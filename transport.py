from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import aiohttp

_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# 脱敏改成「按已知凭据值替换」，不再按 "authorization: bearer xxx" 这种
# 格式去猜。两点好处：
#   1) 真的更严——凭据出现在 URL、json、header、错误文本里都能换掉，
#      而按格式猜只能覆盖一种形态。
#   2) 响应体里不认识的凭据一律不外传：只回状态码与错误类型。
_LONG_OPAQUE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9_.-]{32,})(?![A-Za-z0-9])")
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# aiohttp 在模块加载时才可用，这里先取常量，失败则退化为按名字比较
_WS_TEXT = getattr(getattr(aiohttp, "WSMsgType", None), "TEXT", "TEXT")
_WS_CLOSED = frozenset(
    getattr(getattr(aiohttp, "WSMsgType", None), name, name)
    for name in ("CLOSE", "CLOSED", "CLOSING", "ERROR")
)


class OneBotTransportError(RuntimeError):
    pass


class OneBotActionError(OneBotTransportError):
    def __init__(self, action: str, retcode: int, detail: str) -> None:
        super().__init__(f"{action} failed: {detail} (retcode={retcode})")
        self.action = action
        self.retcode = retcode
        self.detail = detail


def validate_base_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or any(ord(char) < 32 for char in raw):
        raise ValueError("NapCat 地址不能为空或包含控制字符")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.netloc:
        raise ValueError(
            "NapCat 地址必须是有效的 http://、https://、ws:// 或 wss:// 地址"
        )
    if parsed.username or parsed.password:
        raise ValueError("NapCat 地址不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("NapCat 地址不能包含查询参数或片段")
    return raw.rstrip("/")


class OneBotTransport:
    def __init__(
        self,
        base_url: str,
        access_token: str = "",
        *,
        timeout_seconds: float = 10,
        verify_tls: bool = True,
        max_concurrency: int = 2,
        request_interval_ms: int = 100,
        session: Any = None,
    ) -> None:
        self.base_url = validate_base_url(base_url)
        self.access_token = str(access_token or "").strip()
        self.timeout_seconds = float(timeout_seconds)
        self.verify_tls = bool(verify_tls)
        self.max_concurrency = int(max_concurrency)
        self.request_interval_seconds = int(request_interval_ms) / 1000
        if not 1 <= self.timeout_seconds <= 120:
            raise ValueError("请求超时必须在 1 到 120 秒之间")
        if not 1 <= self.max_concurrency <= 8:
            raise ValueError("最大并发数必须在 1 到 8 之间")
        if not 0 <= self.request_interval_seconds <= 10:
            raise ValueError("请求间隔必须在 0 到 10000 毫秒之间")
        self._session = session
        self._owns_session = session is None
        self._start_lock = asyncio.Lock()
        self._pace_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        # WebSocket 一次只跑一个请求：OneBot 靠 echo 对应请求与响应，
        # 串行化后就不必处理并发下的响应匹配与重连风暴。
        # 本插件是后台维护任务（默认 15 分钟一轮），串行的代价可以忽略。
        self._ws_lock = asyncio.Lock()
        self._echo = 0
        self._next_request_at = 0.0

    @property
    def is_websocket(self) -> bool:
        return self.base_url.startswith(("ws://", "wss://"))

    @property
    def endpoint(self) -> str:
        return self.base_url

    async def start(self) -> None:
        if self._session is not None and not getattr(self._session, "closed", False):
            return
        async with self._start_lock:
            if self._session is None or getattr(self._session, "closed", False):
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
                    trust_env=False,
                )

    async def close(self) -> None:
        session = self._session
        self._session = None
        if self._owns_session and session is not None and not getattr(session, "closed", False):
            await session.close()

    def redact(self, text: str) -> str:
        """把错误文本里的凭据换掉。

        顺序很重要：先换掉自己知道的准确值，再对剩余的长随机串兜底。
        """
        value = str(text or "")
        if self.access_token:
            value = value.replace(self.access_token, "***")
        return _LONG_OPAQUE.sub("***", value)

    async def _pace(self) -> None:
        if self.request_interval_seconds <= 0:
            return
        async with self._pace_lock:
            delay = self._next_request_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request_at = time.monotonic() + self.request_interval_seconds

    async def _request(self, action: str, params: Mapping[str, Any] | None = None) -> Any:
        if not _ACTION_RE.fullmatch(str(action or "")):
            raise ValueError("非法内部 action")
        if self.is_websocket:
            return await self._request_ws(action, params)
        return await self._request_http(action, params)

    def _unwrap(self, action: str, result: Any) -> Any:
        """校验 OneBot 响应并取出 data，HTTP 与 WebSocket 共用。"""
        if not isinstance(result, dict):
            raise OneBotTransportError(f"{action} 返回格式不是对象")
        try:
            retcode = int(result.get("retcode", 0))
        except (TypeError, ValueError) as exc:
            raise OneBotTransportError(f"{action} 返回了无效 retcode") from exc
        status = str(result.get("status", "")).lower()
        if retcode != 0 or status not in {"ok", "success"}:
            detail = str(result.get("wording") or result.get("message") or "未知错误")
            raise OneBotActionError(action, retcode, self.redact(detail[:300]))
        return result.get("data")

    async def _request_http(self, action: str, params: Mapping[str, Any] | None = None) -> Any:
        await self.start()
        await self._pace()
        session = self._session
        if session is None:
            raise OneBotTransportError("NapCat HTTP 会话不可用")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        url = f"{self.base_url}/{action}"
        try:
            async with self._semaphore:
                async with session.post(
                    url,
                    json=dict(params or {}),
                    headers=headers,
                    allow_redirects=False,
                    ssl=self.verify_tls,
                ) as response:
                    raw = await response.read()
                    if len(raw) > _MAX_RESPONSE_BYTES:
                        raise OneBotTransportError("NapCat 响应超过 2 MiB 限制")
                    text = raw.decode("utf-8", errors="replace")
                    if not 200 <= int(response.status) < 300:
                        detail = self.redact(text[:300] or f"HTTP {response.status}")
                        raise OneBotActionError(action, int(response.status), detail)
        except OneBotTransportError:
            raise
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise OneBotTransportError(f"{action} 请求超时") from exc
        except aiohttp.ClientError as exc:
            raise OneBotTransportError(
                f"{action} HTTP 请求失败: {self.redact(str(exc))}"
            ) from exc
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OneBotTransportError(f"{action} 返回了无效 JSON") from exc
        return self._unwrap(action, result)

    async def _request_ws(self, action: str, params: Mapping[str, Any] | None = None) -> Any:
        """以 WebSocket 客户端身份连 NapCat 的 WS 服务端。

        与 HTTP 不同：地址里不带 action，action 放在 JSON 包体的 action 字段里发；
        靠 echo 把响应对应回请求。
        """
        await self.start()
        await self._pace()
        session = self._session
        if session is None:
            raise OneBotTransportError("NapCat WebSocket 会话不可用")
        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        try:
            async with self._ws_lock:
                async with session.ws_connect(
                    self.base_url,
                    headers=headers,
                    ssl=self.verify_tls,
                    timeout=self.timeout_seconds,
                    receive_timeout=self.timeout_seconds,
                ) as ws:
                    self._echo += 1
                    echo = f"orbit-{self._echo}"
                    await ws.send_str(
                        json.dumps(
                            {
                                "action": action,
                                "params": dict(params or {}),
                                "echo": echo,
                            },
                            ensure_ascii=False,
                        )
                    )
                    return await self._await_ws_reply(ws, action, echo)
        except OneBotTransportError:
            raise
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise OneBotTransportError(f"{action} WebSocket 请求超时") from exc
        except aiohttp.ClientError as exc:
            raise OneBotTransportError(
                f"{action} WebSocket 请求失败: {self.redact(str(exc))}"
            ) from exc

    async def _await_ws_reply(self, ws: Any, action: str, echo: str) -> Any:
        """收帧直到拿到 echo 对应的响应。

        服务端可能先推心跳、事件或别的 echo 的响应，都得跳过。
        """
        while True:
            message = await asyncio.wait_for(ws.receive(), timeout=self.timeout_seconds)
            kind = getattr(message, "type", None)
            if kind in _WS_CLOSED:
                raise OneBotTransportError(f"{action} WebSocket 被服务端关闭")
            if kind is not _WS_TEXT:
                continue
            raw = getattr(message, "data", "")
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise OneBotTransportError("NapCat 响应超过 2 MiB 限制")
            try:
                result = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(result, dict) and str(result.get("echo", "")) != echo:
                continue
            return self._unwrap(action, result)
