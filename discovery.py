from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from .maintenance import _is_protected

# 搜索是兜底手段，必须有硬上限，否则在超大目录树上会把面板卡死
_MAX_SCAN_ENTRIES = 40000
_MAX_DEPTH = 4
_MAX_TREE_ROOTS_DEPTH = 2

# 这些目录里不会出现 NapCat，扫到纯属浪费
_SKIP_DIRS = frozenset({
    "proc", "sys", "dev", "run", "boot", "etc", "lib", "lib64", "bin", "sbin",
    "node_modules", ".git", "__pycache__", ".cache", "lost+found", "snap",
    "usr", "var", "tmp", "home", "root", "media", "mnt", "srv",
})

_ENV_CACHE_HINTS = ("NAPCAT_TEMP_DIR", "NAPCAT_CACHE_DIR", "NAPCAT_TEMP", "NAPCAT_CACHE")
_ENV_CONFIG_HINTS = ("NAPCAT_CONFIG_DIR", "NAPCAT_CONFIG", "NAPCAT_HOME")

# 容器与常见部署下的根，逐个点名比全盘扫描便宜
_WELL_KNOWN_ROOTS = ("/AstrBot", "/app", "/opt", "/data", "/AstrBot/data", "/root")

_ONEBOT_CONFIG_NAMES = ("onebot11.json",)
_ONEBOT_CONFIG_PREFIX = "onebot11_"
_CACHE_SUFFIXES = ("temp", "cache", "cache_temp", "temp_cache")


def _is_onebot_config(name: str) -> bool:
    return name.endswith(".json") and (
        name in _ONEBOT_CONFIG_NAMES or name.startswith(_ONEBOT_CONFIG_PREFIX)
    )


def _looks_like_napcat(name: str) -> bool:
    return "napcat" in name.lower()


def _top_level_files(root: Path) -> list[str]:
    try:
        return [item.name for item in os.scandir(root) if item.is_file(follow_symlinks=False)]
    except OSError:
        return []


def _has_protected_file(root: Path) -> bool:
    """目录里直接放着 NapCat/AstrBot 的关键文件就绝不能当缓存清。"""
    return any(_is_protected(name) for name in _top_level_files(root))


def _is_safe_to_adopt(root: Path) -> bool:
    """能否在「不通知用户」的前提下自动接管这个目录。

    判据故意保守，宁可漏收也不能误删：
      1. 目录名本身要像缓存（temp / cache / cache_temp / temp_cache），
         这样实例根目录（含 config/ 与程序本体）天然被排除
      2. 里面不能直接放着关键文件
    """
    if root.name.lower() not in _CACHE_SUFFIXES:
        return False
    return not _has_protected_file(root)


def _reject_reason(root: Path) -> str:
    if root.name.lower() not in _CACHE_SUFFIXES:
        return "目录名不像缓存目录，可能包含程序本体与配置，不自动接管"
    if _has_protected_file(root):
        return "内含关键配置文件，不自动接管"
    return ""


def _safe_home() -> Path | None:
    try:
        return Path.home()
    except (OSError, RuntimeError):
        return None


def _ancestors(start: Path, levels: int = _MAX_TREE_ROOTS_DEPTH) -> list[Path]:
    out: list[Path] = []
    current = start
    for _ in range(levels):
        out.append(current)
        if current.parent == current:
            break
        current = current.parent
    return out


def _astrbot_roots() -> list[Path]:
    """AstrBot 自身的位置与它的各级父目录——NapCat 通常就在旁边。"""
    roots: list[Path] = []
    env = os.environ.get("ASTRBOT_ROOT", "").strip()
    if env:
        roots.append(Path(env))
    roots.append(Path.cwd())
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        roots.append(Path(get_astrbot_data_path()))
    except Exception:
        pass
    seen: set[Path] = set()
    expanded: list[Path] = []
    for root in roots:
        try:
            resolved = root.resolve(strict=False)
        except OSError:
            continue
        for item in _ancestors(resolved):
            if item not in seen:
                seen.add(item)
                expanded.append(item)
    return expanded


def search_roots(explicit: list[str]) -> list[Path]:
    """要搜索的根，按「最可能命中」排序。"""
    roots: list[Path] = []
    seen: set[Path] = set()

    def push(value: str | Path) -> None:
        try:
            path = Path(value).expanduser()
        except (OSError, RuntimeError):
            return
        if not path.is_absolute():
            return
        resolved = path.resolve(strict=False)
        if resolved not in seen:
            seen.add(resolved)
            roots.append(resolved)

    for value in explicit:
        push(value)
    for key in _ENV_CONFIG_HINTS:
        push(os.environ.get(key, ""))
    roots.extend(_astrbot_roots())
    home = _safe_home()
    if home is not None:
        push(home)
    for value in _WELL_KNOWN_ROOTS:
        push(value)

    existing = [root for root in roots if root.is_dir()]
    missing = [root for root in roots if not root.is_dir()]
    return existing + missing


class _Budget:
    def __init__(self, limit: int = _MAX_SCAN_ENTRIES) -> None:
        self.remaining = limit
        self.exhausted = False

    def take(self, count: int = 1) -> bool:
        if self.remaining <= 0:
            self.exhausted = True
            return False
        self.remaining -= count
        return True


def _walk(root: Path, budget: _Budget) -> tuple[list[Path], list[Path]]:
    """返回 (onebot 配置文件, 疑似 NapCat 目录)。"""
    configs: list[Path] = []
    napcat_dirs: list[Path] = []
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue:
        current, depth = queue.pop(0)
        if depth > _MAX_DEPTH or not budget.take():
            continue
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if not budget.take():
                break
            name = entry.name
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if name in _SKIP_DIRS and depth > 0:
                        continue
                    if _looks_like_napcat(name):
                        napcat_dirs.append(Path(entry.path))
                    if name != "data" or depth == 0:
                        queue.append((Path(entry.path), depth + 1))
                elif entry.is_file(follow_symlinks=False) and _is_onebot_config(name):
                    configs.append(Path(entry.path))
            except OSError:
                continue
    return configs, napcat_dirs


def _measure(root: Path, budget: _Budget) -> dict[str, Any]:
    file_count = 0
    size_bytes = 0
    skipped_symlinks = 0
    truncated = False
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > _MAX_DEPTH:
            continue
        if not budget.take():
            truncated = True
            break
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            return {
                "path": str(root), "exists": True, "file_count": file_count,
                "size_bytes": size_bytes, "skipped_symlinks": skipped_symlinks,
                "truncated": truncated, "error": type(exc).__name__,
            }
        for entry in entries:
            if not budget.take():
                truncated = True
                break
            try:
                if entry.is_symlink():
                    skipped_symlinks += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), depth + 1))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                file_count += 1
                size_bytes += entry.stat(follow_symlinks=False).st_size
            except OSError:
                skipped_symlinks += 1
    return {
        "path": str(root), "exists": True, "file_count": file_count,
        "size_bytes": size_bytes, "skipped_symlinks": skipped_symlinks,
        "truncated": truncated, "error": "",
    }


def _cache_candidates(napcat_dirs: list[Path]) -> list[Path]:
    """从 NapCat 目录里挑出可能的缓存目录。"""
    result: list[Path] = []
    seen: set[Path] = set()
    for parent in napcat_dirs:
        candidates: list[Path] = []
        if parent.name.lower() in _CACHE_SUFFIXES:
            candidates.append(parent)
        try:
            children = sorted(parent.iterdir(), key=lambda item: item.name)
        except OSError:
            children = []
        for child in children:
            if child.is_dir() and not child.is_symlink():
                if child.name.lower() in _CACHE_SUFFIXES or _looks_like_napcat(child.name):
                    candidates.append(child)
        for item in candidates:
            try:
                resolved = item.resolve(strict=False)
            except OSError:
                continue
            if resolved not in seen:
                seen.add(resolved)
                result.append(resolved)
    return result


def scan_sync(explicit: list[str]) -> dict[str, Any]:
    budget = _Budget()
    configs: list[Path] = []
    napcat_dirs: list[Path] = []
    seen_configs: set[Path] = set()
    seen_dirs: set[Path] = set()
    searched: list[str] = []
    for root in search_roots(explicit):
        if not root.is_dir():
            continue
        searched.append(str(root))
        found_configs, found_dirs = _walk(root, budget)
        for item in found_configs:
            if item not in seen_configs:
                seen_configs.add(item)
                configs.append(item)
        for item in found_dirs:
            if item not in seen_dirs:
                seen_dirs.add(item)
                napcat_dirs.append(item)
        if budget.exhausted:
            break

    cache_dirs: list[Path] = []
    seen_cache: set[Path] = set()
    for key in _ENV_CACHE_HINTS:
        value = os.environ.get(key, "").strip()
        if not value:
            continue
        try:
            path = Path(value).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if path.is_dir() and path not in seen_cache:
            seen_cache.add(path)
            cache_dirs.append(path)
    for item in _cache_candidates(napcat_dirs):
        if item not in seen_cache:
            seen_cache.add(item)
            cache_dirs.append(item)

    measured = []
    for item in cache_dirs:
        if budget.take():
            row = _measure(item, budget)
            row["safe"] = _is_safe_to_adopt(item)
            row["reason"] = "" if row["safe"] else _reject_reason(item)
            measured.append(row)
    return {
        "configs": [_read_onebot_config(path) for path in configs],
        "cache_dirs": measured,
        "searched_roots": searched[:20],
        "truncated": budget.exhausted,
    }


async def scan(explicit: list[str]) -> dict[str, Any]:
    return await asyncio.to_thread(scan_sync, explicit)


# ---------- NapCat OneBot 配置解析（用于取地址与 Token）----------


# network 下四类适配器。can_connect 标记它是不是一个「可供客户端连接」的监听端点：
# *Server 是对外监听；*Client 是主动向外连，没有可连的端口。
# 但四类里的 token 都是候选——用户常把同一份 token 配在多处，只是最终能不能用
# 要靠真实请求验证，所以全收。
_NETWORK_SECTIONS = (
    ("httpServers", "http_server", True),
    ("websocketServers", "ws_server", True),
    ("httpClients", "http_client", False),
    ("websocketClients", "ws_client", False),
)


def _servers_of(doc: Any) -> list[dict[str, Any]]:
    if not isinstance(doc, dict):
        return []
    network = doc.get("network")
    if not isinstance(network, dict):
        return []
    result: list[dict[str, Any]] = []
    for key, kind, can_connect in _NETWORK_SECTIONS:
        items = network.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            host = str(item.get("host") or "").strip()
            try:
                port = int(item.get("port"))
            except (TypeError, ValueError):
                port = 0
            token = str(item.get("token") or "")
            # 空 host 与 0.0.0.0 / :: 都表示「监听所有网卡」，不是合法的访问地址
            if host in ("", "0.0.0.0", "::", "[::]"):
                host = "127.0.0.1"
            scheme = "ws" if kind == "ws_server" else "http"
            result.append(
                {
                    "name": str(item.get("name") or kind),
                    "kind": kind,
                    "can_connect": bool(can_connect and port),
                    "enable": bool(item.get("enable", True)),
                    "host": host,
                    "port": port,
                    "address": f"{scheme}://{host}:{port}" if can_connect and port else "",
                    "url": str(item.get("url") or ""),
                    "has_token": bool(token),
                    "token": token,
                }
            )
    return result


def _read_onebot_config(path: Path) -> dict[str, Any]:
    account = path.stem[len(_ONEBOT_CONFIG_PREFIX) :] if path.name.startswith(
        _ONEBOT_CONFIG_PREFIX
    ) else ""
    entry: dict[str, Any] = {
        "file": str(path), "account": account, "servers": [], "error": "",
    }
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"[:120]
        return entry
    servers = _servers_of(doc)
    if not servers:
        entry["error"] = "network 下没有任何网络适配器配置"
    else:
        entry["servers"] = servers
    return entry


def onebot_configs_sync(explicit: list[str]) -> list[dict[str, Any]]:
    """找出 NapCat 的 OneBot 配置文件，返回里含 token 明文，仅供后端内部使用。"""
    return scan_sync(explicit)["configs"]


async def onebot_configs(explicit: list[str]) -> list[dict[str, Any]]:
    return await asyncio.to_thread(onebot_configs_sync, explicit)


def cache_dirs_sync(explicit: list[str]) -> list[dict[str, Any]]:
    return scan_sync(explicit)["cache_dirs"]


def redact_configs(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给前端看的版本：只告知有没有 token，不把明文送到浏览器。"""
    return [
        {
            "file": entry.get("file", ""),
            "account": entry.get("account", ""),
            "error": entry.get("error", ""),
            "servers": [{**server, "token": ""} for server in entry.get("servers", [])],
        }
        for entry in entries
    ]


def redact_dirs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(rows)
