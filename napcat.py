from __future__ import annotations

import asyncio
from typing import Any

from .auth import classify_transport
from .transport import OneBotTransport

_AUTH_CODES = {401, 403}

# 传输层报错归到这几种就能直接告诉用户「改什么」：服务没启动要起进程，
# 无响应要看防火墙，解析失败要改主机名。归到 other 只能说「失败」。
_KIND_STATE = {
    "refused:": "refused",
    "timeout:": "timeout",
    "dns:": "dns",
    "other:": "error",
}


def _state_of(exc: BaseException) -> str:
    if getattr(exc, "retcode", None) in _AUTH_CODES:
        return "auth"
    return _KIND_STATE.get(classify_transport(exc), "error")


class NapCatEndpoint:
    """一个 NapCat 实例。多个 Bot 就靠这个对象列表来管理。"""

    def __init__(
        self,
        url: str,
        token: str = "",
        *,
        enable: bool = True,
        protocol_interval_hours: int = 12,
        verify_tls: bool = True,
        timeout: float = 10.0,
        max_concurrency: int = 2,
        interval_ms: int = 100,
    ) -> None:
        self.url = str(url or "").strip().rstrip("/")
        self.token = str(token or "").strip()
        self.enable = bool(enable)
        self.protocol_interval_hours = max(1, int(protocol_interval_hours))
        self.verify_tls = bool(verify_tls)
        self.timeout = float(timeout)
        self.max_concurrency = int(max_concurrency)
        self.interval_ms = int(interval_ms)
        self.transport = OneBotTransport(
            self.url,
            self.token,
            timeout_seconds=self.timeout,
            verify_tls=self.verify_tls,
            max_concurrency=self.max_concurrency,
            request_interval_ms=self.interval_ms,
        )

    @property
    def label(self) -> str:
        return self.url or "(未配置地址)"

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"NapCatEndpoint({self.url!r}, token={'有' if self.token else '无'})"


class NapCatCacheClient:
    def __init__(self, endpoints: list[NapCatEndpoint], reauth: Any = None) -> None:
        self.endpoints = [item for item in endpoints if item.url]
        self._reauth = reauth

    def _endpoint_by_url(self, url: str) -> NapCatEndpoint | None:
        for item in self.endpoints:
            if item.url == url:
                return item
        return None

    def set_token(self, url: str, token: str) -> None:
        endpoint = self._endpoint_by_url(url)
        if endpoint is not None and endpoint.token != token:
            endpoint.token = token
            endpoint.transport = OneBotTransport(
                endpoint.url,
                token,
                timeout_seconds=endpoint.timeout,
                verify_tls=endpoint.verify_tls,
                max_concurrency=endpoint.max_concurrency,
                request_interval_ms=endpoint.interval_ms,
            )

    @property
    def endpoint_label(self) -> str:
        if not self.endpoints:
            return "未配置"
        if len(self.endpoints) == 1:
            return self.endpoints[0].label
        return f"{len(self.endpoints)} 个实例"

    @property
    def enabled_endpoints(self) -> list[NapCatEndpoint]:
        return [item for item in self.endpoints if item.enable]

    async def _call(self, endpoint: NapCatEndpoint, action: str) -> Any:
        """跑一次 action；遇 401/403 先重新解析 Token 再重试一次。"""
        try:
            return await endpoint.transport._request(action)
        except Exception as exc:
            retcode = getattr(exc, "retcode", None)
            if retcode not in _AUTH_CODES or self._reauth is None:
                raise
            self._reauth(endpoint.url)
            refreshed = self._endpoint_by_url(endpoint.url)
            if refreshed is not None:
                return await refreshed.transport._request(action)
            raise

    async def status(self) -> list[dict[str, Any]]:
        """逐实例探测。单个实例失败不影响其他实例。

        state 是给面板看的结论：running / disabled / refused / timeout /
        dns / auth / error。以前只有 connected 与一串原始报错文本，
        「服务没启动」和「Token 错了」长得一样，用户只能自己猜。
        """
        results: list[dict[str, Any]] = []

        async def probe(endpoint: NapCatEndpoint) -> dict[str, Any]:
            row: dict[str, Any] = {
                "url": endpoint.label,
                "enable": endpoint.enable,
                "connected": False,
                "error": "",
                "state": "unknown",
            }
            if not endpoint.enable:
                # 不去探测你自己关掉的实例：探测它只会把「已停用」和
                # 「连不上」混成同一个状态，而这两者要采取的动作完全不同。
                row["state"] = "disabled"
                return row
            try:
                await self._call(endpoint, "get_version_info")
                row["connected"] = True
                row["state"] = "running"
            except Exception as exc:
                row["error"] = str(exc)[:160]
                row["state"] = _state_of(exc)
            return row

        if not self.endpoints:
            return results
        results = await asyncio.gather(*(probe(item) for item in self.endpoints))
        return list(results)

    async def clean_protocol_cache(self) -> list[dict[str, Any]]:
        """逐实例清理协议缓存，返回每个实例的结果。"""
        results: list[dict[str, Any]] = []
        for endpoint in self.enabled_endpoints:
            row: dict[str, Any] = {
                "url": endpoint.label,
                "acted": False,
                "error": "",
                "detail": "",
            }
            try:
                await self._call(endpoint, "clean_cache")
                row["acted"] = True
                row["detail"] = "已调用 NapCat clean_cache"
            except Exception as exc:
                row["error"] = str(exc)[:160]
            results.append(row)
        return results
