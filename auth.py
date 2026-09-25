"""NapCat Token 的实时解析与自愈。

为什么需要这个：NapCat 的 OneBot token 可能在每次重启后重新生成。
如果把 token 当成静态配置存下来，它迟早会过期，于是「配好了一次就不管了」这条路走不通。

做法是把 token 变成「可解析的值」：
  1. 每次建立连接前，从多个来源收集候选
  2. 逐个用真实请求验证，留下第一个能通的那个
  3. 运行中一旦收到 401/403，立刻重新解析并重试一次

因为每个候选都要先验证，错的 token 不会造成任何副作用——宁可多试几次。
"""

from __future__ import annotations

import json
import errno as _errno_mod
import os
import socket
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

# 常见 errno（不硬编码平台相关常量，避免在不同系统上取值不一致）
_REFUSED_ERRNOS = frozenset(
    value
    for value in (
        getattr(_errno_mod, "ECONNREFUSED", None),
        10061,  # Windows
    )
    if value is not None
)
_DNS_ERRNOS = frozenset(
    value
    for value in (
        getattr(_errno_mod, "EAI_NONAME", None),
        getattr(_errno_mod, "EAI_AGAIN", None),
        -2, -3, -5,
    )
    if value is not None
)

from astrbot.api import logger

from .discovery import onebot_configs_sync
from .transport import OneBotActionError, OneBotTransport, OneBotTransportError

_AUTH_CODES = {401, 403}
_TTL_SECONDS = 300

_ENV_SOURCES = ("NAPCAT_TOKEN", "ONEBOT_ACCESS_TOKEN", "ONEBOT_TOKEN", "NAPCAT_ACCESS_TOKEN")

# 传输层错误按成因分类，才能告诉用户「到底该改什么」。
# 这些前缀就是分类标签，后面跟的是原始报错文本。
_KIND_REFUSED = "refused:"
_KIND_TIMEOUT = "timeout:"
_KIND_DNS = "dns:"
_KIND_OTHER = "other:"


def _classify_transport(exc: BaseException) -> str:
    """沿着 __cause__ 链找到真正的 socket 层错误。"""
    seen = 0
    current: BaseException | None = exc
    while current is not None and seen < 5:
        seen += 1
        if isinstance(current, TimeoutError) or "timeout" in type(current).__name__.lower():
            return _KIND_TIMEOUT
        if isinstance(current, socket.gaierror):
            return _KIND_DNS
        if isinstance(current, ConnectionRefusedError):
            return _KIND_REFUSED
        if isinstance(current, OSError) and current.errno in _REFUSED_ERRNOS:
            return _KIND_REFUSED
        if isinstance(current, OSError) and current.errno in _DNS_ERRNOS:
            return _KIND_DNS
        current = current.__cause__ or current.__context__
    return _KIND_OTHER


def _explain_failure(why: str, *, has_candidates: bool) -> str:
    """把底层失败原因翻译成人能行动的话。

    关键区分：地址连不上 / 服务没启动 / 服务无响应 / 主机名写错 / 鉴权被拒，
    这几类的解法完全不同，不能一律说成「Token 不对」。
    """
    if why.startswith(_KIND_REFUSED):
        detail = why[len(_KIND_REFUSED) :].strip()
        return f"NapCat 服务没启动（或端口填错了），与 Token 无关：{detail}"
    if why.startswith(_KIND_TIMEOUT):
        detail = why[len(_KIND_TIMEOUT) :].strip()
        return f"连上了但不响应，可能是防火墙拦截或服务卡住（与 Token 无关）：{detail}"
    if why.startswith(_KIND_DNS):
        detail = why[len(_KIND_DNS) :].strip()
        return f"主机名解析失败，请检查地址里写的是服务名还是 IP（与 Token 无关）：{detail}"
    if why.startswith(_KIND_OTHER):
        detail = why[len(_KIND_OTHER) :].strip()
        return f"连接失败（与 Token 无关），请检查网络或代理：{detail}"
    if why.startswith("bad_url"):
        return f"地址不合法：{why.split(': ', 1)[-1]}"
    if why in ("auth", "auth_with_token"):
        if has_candidates:
            return "找到的 Token 都被拒绝了，请在面板手动填写当前 NapCat 实际使用的那个"
        return "NapCat 启用了鉴权，但没找到任何可用的 Token 来源，请手动填写"
    return why[:200]


def _port_of(base_url: str) -> int:
    try:
        return int(urlsplit(base_url).port or 0)
    except ValueError:
        return 0


def _collect_tokens(value: Any, out: list[str], depth: int = 0) -> None:
    """从任意结构里递归捞出 token 字段，兼容 AstrBot 各版本的配置差异。"""
    if depth > 6:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and key.lower() in ("token", "access_token", "secret"):
                if item.strip():
                    out.append(item.strip())
            else:
                _collect_tokens(item, out, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _collect_tokens(item, out, depth + 1)


def _astrbot_platform_tokens(data_path: Path) -> list[str]:
    """从 AstrBot 自己的平台配置里捞 token——它和 NapCat 侧可能是同一个值。"""
    out: list[str] = []
    candidates = [data_path / "cmd_config.json"]
    config_dir = data_path / "config"
    if config_dir.is_dir():
        try:
            candidates.extend(sorted(config_dir.glob("*.json")))
        except OSError:
            pass
    for path in candidates:
        try:
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        _collect_tokens(doc.get("platform"), out)
        _collect_tokens(doc.get("platform_specific"), out)
    return out


class TokenResolver:
    def __init__(
        self,
        *,
        config: Any,
        data_path: Path,
        explicit_dirs: Callable[[], list[str]],
        timeout: float = 8.0,
        verify_tls: bool = True,
    ) -> None:
        self._config = config
        self._data_path = data_path
        self._explicit_dirs = explicit_dirs
        self._timeout = timeout
        self._verify_tls = verify_tls
        self._cache: dict[str, tuple[float, str, str]] = {}
        self.last_note = ""

    # ---------- 候选收集 ----------

    def _napcat_tokens(self, base_url: str) -> list[tuple[str, str]]:
        """按 (来源, token) 返回；端口与目标一致的优先。"""
        port = _port_of(base_url)
        exact: list[tuple[str, str]] = []
        others: list[tuple[str, str]] = []
        try:
            entries = onebot_configs_sync(self._explicit_dirs())
        except Exception as exc:
            logger.warning("Orbit Cache 扫描 NapCat 配置失败：%s", type(exc).__name__)
            return []
        for entry in entries:
            file_label = Path(entry.get("file", "")).name or "onebot11.json"
            for server in entry.get("servers", []):
                token = str(server.get("token") or "")
                if not token:
                    continue
                item = (f"NapCat 配置 {file_label}", token)
                if port and server.get("port") == port:
                    exact.append(item)
                else:
                    others.append(item)
        return exact + others

    def _candidates(self, base_url: str) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        seen: set[str] = set()

        def add(source: str, token: str) -> None:
            token = str(token or "").strip()
            if token and token not in seen:
                seen.add(token)
                found.append((source, token))

        add("手动配置", str(self._config.get("onebot_access_token", "") or ""))
        for source, token in self._napcat_tokens(base_url):
            add(source, token)
        for token in _astrbot_platform_tokens(self._data_path):
            add("AstrBot 平台配置", token)
        for key in _ENV_SOURCES:
            add(f"环境变量 {key}", os.environ.get(key, ""))
        return found

    # ---------- 解析 ----------

    async def resolve(self, base_url: str, *, force: bool = False) -> dict[str, Any]:
        """返回 {token, source, needs_token, reachable, note}。"""
        if not base_url:
            return {
                "token": "", "source": "", "needs_token": False, "reachable": False,
                "note": "尚未配置 NapCat 地址",
            }
        cached = self._cache.get(base_url)
        if cached and not force and time.monotonic() - cached[0] < _TTL_SECONDS:
            return {
                "token": cached[1], "source": cached[2], "needs_token": False,
                "reachable": True, "note": "",
            }

        ok, why = await _try(base_url, "", self._timeout, self._verify_tls)
        if ok:
            self._cache[base_url] = (time.monotonic(), "", "无需 Token")
            self.last_note = "无需 Token"
            return {
                "token": "", "source": "无需 Token", "needs_token": False,
                "reachable": True, "note": "该实例未启用鉴权",
            }

        candidates = self._candidates(base_url)
        transport_failed = why.startswith(
            (_KIND_REFUSED, _KIND_TIMEOUT, _KIND_DNS, _KIND_OTHER)
        )
        if not candidates or transport_failed:
            # 连都连不上时谈 Token 是浪费时间，直说连接问题
            self.last_note = _explain_failure(why, has_candidates=False)
            return {
                "token": "", "source": "",
                "needs_token": False if transport_failed else why == "auth",
                "reachable": not transport_failed,
                "note": self.last_note,
            }
        for source, token in candidates:
            ok, _ = await _try(base_url, token, self._timeout, self._verify_tls)
            if ok:
                self._cache[base_url] = (time.monotonic(), token, source)
                self.last_note = f"Token 来自 {source}"
                logger.info("Orbit Cache NapCat Token 已解析（来源：%s）", source)
                return {
                    "token": token, "source": source, "needs_token": True,
                    "reachable": True, "note": "",
                }
        self._cache.pop(base_url, None)
        self.last_note = "候选 Token 都无效"
        return {
            "token": "", "source": "", "needs_token": why == "auth",
            "reachable": not transport_failed,
            "note": _explain_failure(why, has_candidates=True),
        }

    def invalidate(self, base_url: str) -> None:
        self._cache.pop(base_url, None)

    def forget_manual(self) -> None:
        """手动改了 token 就别再吃缓存。"""
        self._cache.clear()


async def probe_state(
    base_url: str, token: str, *, timeout: float = 8.0, verify_tls: bool = True
) -> dict[str, str]:
    """只判断当前配置能不能连上，不做解析。"""
    if not base_url:
        return {"mode": "no_url", "detail": "尚未配置 NapCat 地址"}
    ok, why = await _try(base_url, token, timeout, verify_tls)
    if ok:
        return {
            "mode": "ok" if token else "ok_no_token",
            "detail": "Token 有效" if token else "该实例未启用鉴权",
        }
    if why in ("auth", "auth_with_token"):
        return {
            "mode": "auth_failed" if token else "auth_required",
            "detail": "Token 无效或缺失，NapCat 拒绝了请求",
        }
    if why.startswith(_KIND_TIMEOUT):
        return {"mode": "timeout", "detail": "连上了但不响应，可能是防火墙或服务卡住"}
    if why.startswith(_KIND_DNS):
        return {"mode": "dns", "detail": "主机名解析失败，请检查地址里的服务名或 IP"}
    if why.startswith(_KIND_OTHER):
        return {"mode": "error", "detail": "连接失败，请检查网络或代理"}
    if why.startswith(_KIND_REFUSED):
        return {"mode": "unreachable", "detail": "NapCat 服务没启动（或端口填错了）"}
    return {"mode": "error", "detail": why[:200]}


async def _try(base_url: str, token: str, timeout: float, verify_tls: bool) -> tuple[bool, str]:
    try:
        transport = OneBotTransport(
            base_url, token, timeout_seconds=timeout, verify_tls=verify_tls
        )
    except ValueError as exc:
        return False, f"bad_url: {exc}"
    await transport.start()
    try:
        await transport._request("get_version_info")
        return True, "ok"
    except OneBotActionError as exc:
        if exc.retcode in _AUTH_CODES:
            return False, "auth_with_token" if token else "auth"
        return False, f"retcode={exc.retcode}"
    except OneBotTransportError as exc:
        return False, _classify_transport(exc) + str(exc)[:160]
    except Exception as exc:
        return False, _classify_transport(exc) + f"{type(exc).__name__}: {exc}"[:160]
    finally:
        await transport.close()


async def probe_auth(
    base_url: str, *, timeout: float = 5.0, verify_tls: bool = True
) -> dict[str, Any]:
    """不带 Token 真实发一次请求，判断这个 NapCat 到底要不要 Token。"""
    ok, why = await _try(base_url, "", timeout, verify_tls)
    if ok:
        return {"mode": "ok_no_token", "detail": "不填 Token 也能连上，说明该实例没启用鉴权"}
    if why == "auth":
        return {"mode": "auth_required", "detail": "该实例启用了鉴权，必须填 Token"}
    if why.startswith("bad_url"):
        return {"mode": "bad_url", "detail": why.split(": ", 1)[-1][:200]}
    return {
        "mode": "unreachable",
        "detail": _explain_failure(why, has_candidates=False)[:220],
    }


__all__ = ["TokenResolver", "probe_state", "probe_auth"]
