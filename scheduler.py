from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .formatting import human_bytes
from .maintenance import (
    AstrBotCacheCleaner,
    ModuleCacheError,
    ModuleCacheManager,
    NapCatCacheDirectoryCleaner,
)
from .napcat import NapCatCacheClient

MIB = 1024 * 1024

LAYERS = ("modules", "astrbot", "napcat_files", "napcat_protocol")

LAYER_LABELS = {
    "modules": "停用插件模块",
    "astrbot": "AstrBot 磁盘缓存",
    "napcat_files": "NapCat 文件缓存",
    "napcat_protocol": "NapCat 协议缓存",
}

_HISTORY_LIMIT = 200
_AUTO_DIR_TTL = 1800.0

_SETTING_BOUNDS = {
    "astrbot_cache_threshold_mb": (1, 1024 * 1024),
    "napcat_cache_threshold_mb": (1, 1024 * 1024),
    "napcat_cache_min_age_minutes": (0, 10080),
    "napcat_protocol_interval_hours": (1, 24 * 365),
}

_TARGET_RATIO = 0.85


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _ratio(current: int, threshold: int) -> int:
    if threshold <= 0:
        return 0
    return min(100, round(current * 100 / threshold))


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    tmp.replace(path)


class SweepEngine:
    """自动清理的唯一入口：判定条件、执行动作、记录状态。

    所有入口（框架 cron、WebUI 面板、聊天命令）都走 sweep()，保证
    「条件判定 -> 执行 -> 记账」只有一份实现，不会出现两条路径行为不一致。
    """

    def __init__(
        self,
        *,
        config: Any,
        napcat_client: NapCatCacheClient,
        astrbot_cache: AstrBotCacheCleaner,
        module_cache: ModuleCacheManager,
        data_dir: Path,
    ) -> None:
        self._config = config
        self.napcat_client = napcat_client
        self.astrbot_cache = astrbot_cache
        self.module_cache = module_cache
        self._data_dir = data_dir
        self._lock = asyncio.Lock()
        self._fs_cleaner: NapCatCacheDirectoryCleaner | None = None
        self._fs_error = ""
        self._state_path = data_dir / "state.json"
        self._history_path = data_dir / "history.json"
        self._state = self._load_state()
        self._history = self._load_history()
        self._schedule: dict[str, Any] = {"cron": "", "next_run": "", "job_id": ""}
        self._auto_dirs: list[dict[str, Any]] = []
        self._auto_dirs_at = 0.0

    # ---------- 缓存目录：手动优先，否则自动接管 ----------

    @property
    def auto_adopt_enabled(self) -> bool:
        return self._bool("auto_adopt_cache_dirs", True)

    def _cached_auto_dirs(self) -> list[dict[str, Any]]:
        if not self._auto_dirs or (time.monotonic() - self._auto_dirs_at) > _AUTO_DIR_TTL:
            return []
        return self._auto_dirs

    async def auto_dirs(self, *, force: bool = False) -> list[dict[str, Any]]:
        """扫描并判定哪些缓存目录可以「不问就接管」。

        只在用户没有手填目录时生效；手填优先，永远不会被自动接管覆盖。
        """
        if not self.auto_adopt_enabled or self._dirs():
            return []
        if (
            not force
            and self._auto_dirs
            and (time.monotonic() - self._auto_dirs_at) <= _AUTO_DIR_TTL
        ):
            return self._auto_dirs
        from .discovery import cache_dirs_sync

        try:
            rows = await asyncio.to_thread(cache_dirs_sync, self._token_dirs())
        except Exception as exc:
            logger.warning(
                "Orbit Cache 自动接管缓存目录扫描失败：%s", type(exc).__name__
            )
            return self._cached_auto_dirs()
        self._auto_dirs = [row for row in rows if row.get("safe")]
        self._auto_dirs_at = time.monotonic()
        if self._auto_dirs:
            logger.info(
                "Orbit Cache 已自动接管 %d 个缓存目录：%s",
                len(self._auto_dirs),
                "、".join(str(row["path"]) for row in self._auto_dirs[:4]),
            )
        return self._auto_dirs

    def active_dirs(self) -> tuple[list[str], str]:
        """返回 (生效目录, 来源说明)。"""
        manual = self._dirs()
        if manual:
            return manual, "手动配置"
        if not self.auto_adopt_enabled:
            return [], "自动接管已关闭"
        auto = self._cached_auto_dirs()
        if not auto:
            return [], "未发现可自动接管的缓存目录"
        return [str(row["path"]) for row in auto], "自动接管"

    # ---------- 配置 ----------

    def _get(self, key: str, default: Any) -> Any:
        value = self._config.get(key, default)
        return default if value is None else value

    def _bool(self, key: str, default: bool) -> bool:
        value = self._config.get(key, default)
        return value if isinstance(value, bool) else default

    def _int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(self._config.get(key, default))
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    def _dirs(self) -> list[str]:
        value = self._config.get("napcat_cache_dirs", [])
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    def _token_dirs(self) -> list[str]:
        """用户显式指定的额外搜索根。"""
        value = self._config.get("napcat_config_dirs", [])
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    @property
    def auto_enabled(self) -> bool:
        return self._bool("auto_enabled", True)

    @property
    def sweep_cron(self) -> str:
        value = str(self._get("sweep_cron", "*/15 * * * *")).strip()
        return value or "*/15 * * * *"

    def settings(self) -> dict[str, Any]:
        return {
            "auto_enabled": self.auto_enabled,
            "sweep_cron": self.sweep_cron,
            "auto_clean_plugin_modules": self._bool("auto_clean_plugin_modules", True),
            "auto_adopt_cache_dirs": self.auto_adopt_enabled,
            "astrbot_cache_threshold_mb": self._int(
                "astrbot_cache_threshold_mb", 16, *_SETTING_BOUNDS["astrbot_cache_threshold_mb"]
            ),
            "napcat_cache_threshold_mb": self._int(
                "napcat_cache_threshold_mb", 1, *_SETTING_BOUNDS["napcat_cache_threshold_mb"]
            ),
            "napcat_cache_min_age_minutes": self._int(
                "napcat_cache_min_age_minutes", 10, *_SETTING_BOUNDS["napcat_cache_min_age_minutes"]
            ),
            "napcat_protocol_interval_hours": self._int(
                "napcat_protocol_interval_hours", 12, *_SETTING_BOUNDS["napcat_protocol_interval_hours"]
            ),
            "napcat_cache_dirs": self._dirs(),
            "target_ratio": _TARGET_RATIO,
        }

    def update_settings(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """校验并落盘面板提交的可调项。返回 (settings, error)，error 非空表示未生效。"""
        errors: list[str] = []
        if not isinstance(payload, dict):
            return self.settings(), "提交内容不是对象"

        for key in ("auto_enabled", "auto_clean_plugin_modules", "auto_adopt_cache_dirs"):
            if key in payload:
                self._config[key] = bool(payload[key])

        if "sweep_cron" in payload:
            expression = str(payload["sweep_cron"] or "").strip()
            if not expression:
                errors.append("体检频率不能为空")
            elif len(expression.split()) != 5:
                errors.append("体检频率需要是五段标准 crontab 表达式")
            else:
                self._config["sweep_cron"] = expression

        for key, (minimum, maximum) in _SETTING_BOUNDS.items():
            if key not in payload:
                continue
            try:
                parsed = int(payload[key])
            except (TypeError, ValueError):
                errors.append(f"{key} 必须是整数")
                continue
            if not minimum <= parsed <= maximum:
                errors.append(f"{key} 需要在 {minimum} 到 {maximum} 之间")
                continue
            self._config[key] = parsed

        if "napcat_cache_dirs" in payload:
            raw = payload["napcat_cache_dirs"]
            if isinstance(raw, str):
                values = [line.strip() for line in raw.splitlines() if line.strip()]
            elif isinstance(raw, list):
                values = [str(item).strip() for item in raw if str(item).strip()]
            else:
                values = []
            self._config["napcat_cache_dirs"] = values

        self._config.save_config()
        self._invalidate_fs_cleaner()
        return self.settings(), "；".join(errors)

    # ---------- NapCat 文件缓存清理器 ----------

    def _invalidate_fs_cleaner(self) -> None:
        self._fs_cleaner = None
        self._fs_error = ""
        self._auto_dirs = []
        self._auto_dirs_at = 0.0

    def fs_cleaner(self) -> NapCatCacheDirectoryCleaner:
        if self._fs_cleaner is not None:
            return self._fs_cleaner
        settings = self.settings()
        dirs, _ = self.active_dirs()
        try:
            self._fs_cleaner = NapCatCacheDirectoryCleaner(
                dirs,
                threshold_mb=settings["napcat_cache_threshold_mb"],
                min_age_minutes=settings["napcat_cache_min_age_minutes"],
                target_ratio=_TARGET_RATIO,
            )
            self._fs_error = ""
        except ValueError as exc:
            self._fs_cleaner = NapCatCacheDirectoryCleaner(
                [],
                threshold_mb=settings["napcat_cache_threshold_mb"],
                min_age_minutes=settings["napcat_cache_min_age_minutes"],
            )
            self._fs_error = str(exc)
        return self._fs_cleaner

    @property
    def fs_config_error(self) -> str:
        self.fs_cleaner()
        return self._fs_error

    # ---------- 状态与历史 ----------

    def _load_state(self) -> dict[str, Any]:
        default = {
            "layers": {},
            "last_sweep_at": "",
            "last_freed_bytes": 0,
            "sweep_count": 0,
        }
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
        if not isinstance(raw, dict):
            return default
        layers = raw.get("layers")
        raw["layers"] = layers if isinstance(layers, dict) else {}
        return {**default, **raw}

    def _load_history(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self._history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)][:_HISTORY_LIMIT]

    def _layer_state(self, layer: str) -> dict[str, Any]:
        entry = self._state["layers"].get(layer)
        return entry if isinstance(entry, dict) else {}

    def _record_layer(self, layer: str, record: dict[str, Any]) -> None:
        # 必须是合并而不是整块覆盖：napcat_protocol 里还挂着逐实例的 instances
        entry = self._state["layers"].get(layer)
        merged = dict(entry) if isinstance(entry, dict) else {}
        merged.update(
            {
                "last_run_at": record["at"],
                "last_freed_bytes": record.get("freed_bytes", 0),
                "last_acted": bool(record.get("acted")),
                "last_error": record.get("error", ""),
            }
        )
        self._state["layers"][layer] = merged

    async def _persist(self) -> None:
        try:
            await asyncio.to_thread(_write_json_atomic, self._state_path, self._state)
        except OSError as exc:
            logger.warning("Orbit Cache 无法写入状态文件: %s", exc)

    async def _push_history(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        self._history = (records + self._history)[:_HISTORY_LIMIT]
        try:
            await asyncio.to_thread(_write_json_atomic, self._history_path, self._history)
        except OSError as exc:
            logger.warning("Orbit Cache 无法写入历史文件: %s", exc)

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._history[: max(1, min(int(limit), _HISTORY_LIMIT))]

    def last_error(self) -> str:
        """最近一次真实错误（history 是新在前）。"""
        for record in self._history:
            if record.get("error"):
                return f"{record.get('at', '')} [{record.get('label', '')}] {record['error']}"
        return ""

    def set_schedule(self, *, cron: str, next_run: str, job_id: str) -> None:
        self._schedule = {"cron": cron, "next_run": next_run, "job_id": job_id}

    # ---------- 状态采集 ----------

    async def collect(self) -> dict[str, Any]:
        await self.auto_dirs()
        cleaner = self.fs_cleaner()
        dirs, dir_source = self.active_dirs()
        remote, astrbot_cache, napcat_fs = await asyncio.gather(
            self.napcat_client.status(),
            self.astrbot_cache.status(),
            cleaner.status(),
            return_exceptions=True,
        )
        fs_bytes = 0
        fs_files = 0
        if isinstance(napcat_fs, Exception):
            fs_detail = f"扫描失败（{type(napcat_fs).__name__}）"
        else:
            fs_bytes = sum(int(item.size_bytes) for item in napcat_fs)
            fs_files = sum(int(item.file_count) for item in napcat_fs)
            fs_detail = f"{len(cleaner.directories)} 个目录 / {fs_files} 个文件"

        settings = self.settings()
        threshold_fs = settings["napcat_cache_threshold_mb"] * MIB
        threshold_astrbot = settings["astrbot_cache_threshold_mb"] * MIB
        if isinstance(astrbot_cache, Exception):
            astrbot_bytes = 0
            astrbot_detail = f"读取失败（{type(astrbot_cache).__name__}）"
        else:
            cache = astrbot_cache.get("cache", {}) if isinstance(astrbot_cache, dict) else {}
            astrbot_bytes = int(cache.get("size_bytes", 0) or 0)
            astrbot_detail = f"{cache.get('file_count', 0)} 个文件"

        protocol_state = self._layer_state("napcat_protocol")
        instance_state = self._instance_state()
        enabled = self.napcat_client.enabled_endpoints
        protocol_due = self._protocol_due() and bool(enabled)
        modules = self.module_cache.describe()
        reclaimable_modules = sum(
            item["module_count"] for item in modules if not item["activated"] and not item["self"]
        )
        protocol_hours = settings["napcat_protocol_interval_hours"]
        has_dirs = bool(cleaner.directories)
        return {
            "collected_at": _now_iso(),
            "last_error": self.last_error(),
            "schedule": {
                **self._schedule,
                "auto_enabled": settings["auto_enabled"],
                "cron": self.sweep_cron,
                "cron_effective": self._schedule.get("cron", ""),
            },
            "totals": {
                "reclaimable_bytes": astrbot_bytes + fs_bytes,
                "reclaimable_modules": reclaimable_modules,
                "last_sweep_at": self._state.get("last_sweep_at", ""),
                "last_freed_bytes": self._state.get("last_freed_bytes", 0),
                "sweep_count": self._state.get("sweep_count", 0),
            },
            "napcat": {
                "endpoint": self.napcat_client.endpoint_label,
                "instances": [
                    {
                        **row,
                        "token_set": bool(
                            getattr(self.napcat_client._endpoint_by_url(row["url"]), "token", "")
                        ),
                        "last_run_at": instance_state.get(row["url"], {}).get("last_run_at", ""),
                    }
                    for row in (remote if isinstance(remote, list) else [])
                ],
                "connected": bool(
                    isinstance(remote, list) and any(r.get("connected") for r in remote)
                ),
                "error": "" if isinstance(remote, list) else type(remote).__name__,
            },
            "layers": {
                "modules": {
                    "label": LAYER_LABELS["modules"],
                    "current": reclaimable_modules,
                    "unit": "模块",
                    "amount": f"{reclaimable_modules} 个模块可回收",
                    "threshold": None,
                    "ratio": None,
                    "due": reclaimable_modules > 0
                    and settings["auto_clean_plugin_modules"],
                    "enabled": settings["auto_clean_plugin_modules"],
                    "detail": f"已加载插件共 {len(modules)} 组，当前全部启用则无需处理",
                    "config_error": "",
                    "directories": [],
                    "last": self._layer_state("modules"),
                },
                "astrbot": {
                    "label": LAYER_LABELS["astrbot"],
                    "current": astrbot_bytes,
                    "unit": "bytes",
                    "amount": f"{human_bytes(astrbot_bytes)} / 阈值 {human_bytes(threshold_astrbot)}",
                    "threshold": threshold_astrbot,
                    "ratio": _ratio(astrbot_bytes, threshold_astrbot),
                    "due": astrbot_bytes > threshold_astrbot,
                    "enabled": True,
                    "detail": astrbot_detail,
                    "config_error": "",
                    "directories": [],
                    "last": self._layer_state("astrbot"),
                },
                "napcat_files": {
                    "label": LAYER_LABELS["napcat_files"],
                    "current": fs_bytes,
                    "unit": "bytes",
                    "amount": (
                        f"{human_bytes(fs_bytes)} / 阈值 {human_bytes(threshold_fs)}"
                        if has_dirs
                        else "未配置目录"
                    ),
                    "threshold": threshold_fs if has_dirs else None,
                    "ratio": _ratio(fs_bytes, threshold_fs) if has_dirs else None,
                    "due": fs_bytes > threshold_fs and has_dirs,
                    "enabled": has_dirs,
                    "detail": fs_detail,
                    "dir_source": dir_source,
                    "config_error": self._fs_error,
                    "directories": [
                        {
                            "path": str(item.path),
                            "exists": item.exists,
                            "file_count": item.file_count,
                            "size_bytes": item.size_bytes,
                            "skipped_symlinks": item.skipped_symlinks,
                            "error": item.error,
                        }
                        for item in (napcat_fs if isinstance(napcat_fs, list) else [])
                    ],
                    "last": self._layer_state("napcat_files"),
                },
                "napcat_protocol": {
                    "label": LAYER_LABELS["napcat_protocol"],
                    "current": 0,
                    "unit": "count",
                    "amount": f"{len(enabled)} 个启用实例 / 每 {protocol_hours} 小时一次",
                    "threshold": None,
                    "ratio": None,
                    "due": protocol_due,
                    "enabled": bool(enabled),
                    "detail": (
                        f"上次 {protocol_state.get('last_run_at')}"
                        if protocol_state.get("last_run_at")
                        else "尚未清理过"
                    ),
                    "config_error": "" if enabled else "没有启用中的 NapCat 实例",
                    "directories": [],
                    "last": protocol_state,
                },
            },
            "module_rows": modules,
        }

    def _protocol_due(self) -> bool:
        return any(
            self._protocol_due_for(item.url, item.protocol_interval_hours)
            for item in self.napcat_client.enabled_endpoints
        )

    # ---------- 执行 ----------

    async def sweep(
        self, *, force: bool = False, layers: list[str] | None = None
    ) -> dict[str, Any]:
        if self._lock.locked():
            return {"ok": False, "error": "上一次清理还没结束", "records": []}
        async with self._lock:
            return await self._sweep_locked(force=force, layers=layers)

    async def _sweep_locked(
        self, *, force: bool, layers: list[str] | None = None
    ) -> dict[str, Any]:
        wanted = [item for item in (layers or LAYERS) if item in LAYERS]
        records: list[dict[str, Any]] = []
        for layer in wanted:
            records.append(await self._run_layer(layer, force=force))

        freed = sum(int(item.get("freed_bytes", 0)) for item in records)
        self._state["last_sweep_at"] = _now_iso()
        self._state["last_freed_bytes"] = freed
        self._state["sweep_count"] = int(self._state.get("sweep_count", 0)) + 1
        await self._persist()
        await self._push_history(records)

        errors = [item["error"] for item in records if item.get("error")]
        return {
            "ok": not errors,
            "forced": force,
            "at": self._state["last_sweep_at"],
            "freed_bytes": freed,
            "acted": [item["layer"] for item in records if item.get("acted")],
            "records": records,
            "error": "；".join(errors),
        }

    async def _run_layer(self, layer: str, *, force: bool) -> dict[str, Any]:
        started = time.monotonic()
        record: dict[str, Any] = {
            "layer": layer,
            "label": LAYER_LABELS[layer],
            "at": _now_iso(),
            "acted": False,
            "freed_bytes": 0,
            "items": 0,
            "skipped": "",
            "error": "",
            "detail": "",
        }
        try:
            handler = {
                "modules": self._run_modules,
                "astrbot": self._run_astrbot,
                "napcat_files": self._run_napcat_files,
                "napcat_protocol": self._run_napcat_protocol,
            }[layer]
            await handler(record, force)
        except Exception as exc:  # 单层失败不能中断其他层
            record["error"] = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("Orbit Cache %s 清理失败: %s", layer, record["error"])
        record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        self._record_layer(layer, record)
        if record["acted"] or record["error"]:
            logger.info(
                "Orbit Cache layer=%s acted=%s freed=%s items=%s detail=%s",
                layer,
                record["acted"],
                record["freed_bytes"],
                record["items"],
                record["detail"] or record["skipped"],
            )
        return record

    async def _run_modules(self, record: dict[str, Any], force: bool) -> None:
        if not self._bool("auto_clean_plugin_modules", True) and not force:
            record["skipped"] = "插件模块自动清理已关闭"
            return
        result = self.module_cache.clean_inactive_plugins()
        cleaned = result.get("cleaned", {})
        items = sum(len(modules) for modules in cleaned.values())
        record["items"] = items
        if items:
            record["acted"] = True
            record["detail"] = "、".join(
                f"{plugin_id} 移除 {len(modules)} 个模块" for plugin_id, modules in cleaned.items()
            )
        else:
            record["skipped"] = "没有停用插件仍占用内存"
        skipped = result.get("skipped", [])
        if skipped:
            record["detail"] = (record["detail"] + "；" if record["detail"] else "") + "跳过 " + "、".join(
                skipped
            )

    async def _run_astrbot(self, record: dict[str, Any], force: bool) -> None:
        threshold = self.settings()["astrbot_cache_threshold_mb"] * MIB
        size = await self.astrbot_cache.size_bytes()
        record["detail"] = f"当前占用 {size} 字节，阈值 {threshold} 字节"
        if size <= threshold and not force:
            record["skipped"] = f"占用 {size} 字节未超过阈值 {threshold} 字节"
            return
        result = await self.astrbot_cache.clean()
        record["acted"] = True
        record["freed_bytes"] = int(result.get("removed_bytes", 0) or 0)
        record["items"] = int(result.get("processed_files", 0) or 0)
        record["detail"] = (
            f"处理 {record['items']} 个文件，释放 {record['freed_bytes']} 字节，"
            f"失败 {result.get('failed_files', 0)}"
        )

    async def _run_napcat_files(self, record: dict[str, Any], force: bool) -> None:
        await self.auto_dirs()
        cleaner = self.fs_cleaner()
        if self._fs_error:
            record["error"] = self._fs_error
            return
        if not cleaner.directories:
            record["skipped"] = "尚未配置 NapCat 文件缓存目录，可在面板点自动探测"
            return
        scans = await cleaner.status()
        total = sum(int(item.size_bytes) for item in scans)
        threshold = cleaner.threshold_bytes
        record["detail"] = f"当前占用 {total} 字节，阈值 {threshold} 字节"
        if total <= threshold and not force:
            record["skipped"] = f"占用 {total} 字节未超过阈值 {threshold} 字节"
            return
        results = await cleaner.clean_once(force=force)
        record["acted"] = True
        record["freed_bytes"] = sum(int(item["deleted_bytes"]) for item in results)
        record["items"] = sum(int(item["deleted_files"]) for item in results)
        record["detail"] = "；".join(
            f"{item['path']} 删除 {item['deleted_files']} 个 / {item['deleted_bytes']} 字节"
            for item in results
        )

    def _instance_state(self) -> dict[str, Any]:
        entry = self._state["layers"].get("napcat_protocol", {})
        value = entry.get("instances") if isinstance(entry, dict) else None
        return value if isinstance(value, dict) else {}

    def _protocol_due_for(self, url: str, interval_hours: int) -> bool:
        last = self._instance_state().get(url, {}).get("last_run_at")
        if not last:
            return True
        try:
            previous = datetime.fromisoformat(str(last)).timestamp()
        except ValueError:
            return True
        return (time.time() - previous) >= interval_hours * 3600

    async def _run_napcat_protocol(self, record: dict[str, Any], force: bool) -> None:
        endpoints = self.napcat_client.enabled_endpoints
        if not endpoints:
            record["skipped"] = "没有启用中的 NapCat 实例"
            return
        results: list[dict[str, Any]] = []
        per_instance = self._instance_state()
        for endpoint in endpoints:
            due = force or self._protocol_due_for(endpoint.url, endpoint.protocol_interval_hours)
            if not due:
                results.append({
                    "url": endpoint.label, "acted": False, "error": "",
                    "detail": "距上次清理未超过间隔",
                })
                continue
            try:
                await self.napcat_client._call(endpoint, "clean_cache")
                results.append({
                    "url": endpoint.label, "acted": True, "error": "",
                    "detail": "已调用 clean_cache",
                })
                per_instance[endpoint.url] = {"last_run_at": _now_iso()}
            except Exception as exc:
                results.append({
                    "url": endpoint.label, "acted": False,
                    "error": f"{type(exc).__name__}: {exc}"[:160], "detail": "",
                })
        self._state["layers"].setdefault("napcat_protocol", {})
        self._state["layers"]["napcat_protocol"]["instances"] = per_instance
        record["items"] = sum(1 for item in results if item["acted"])
        record["acted"] = record["items"] > 0
        record["detail"] = "；".join(
            f"{item['url']}：{item['detail'] or item['error']}" for item in results
        )
        if not record["acted"]:
            record["skipped"] = "所有实例都未到清理时间"

    async def dry_run(self, *, force: bool = False) -> dict[str, Any]:
        """试运行：只算不删，回答「到底会不会真的删东西」。

        不对磁盘做任何写操作，也不改状态、不记历史。
        """
        await self.auto_dirs()
        cleaner = self.fs_cleaner()
        previews = await cleaner.preview(force=force)
        threshold = self.settings()["napcat_cache_threshold_mb"] * MIB
        total_files = sum(item["would_delete_files"] for item in previews)
        total_bytes = sum(item["would_delete_bytes"] for item in previews)
        astrbot_size = 0
        astrbot_error = ""
        try:
            astrbot_size = await self.astrbot_cache.size_bytes()
        except Exception as exc:
            astrbot_error = f"{type(exc).__name__}: {exc}"[:120]
        return {
            "threshold_bytes": threshold,
            "directories": previews,
            "would_delete_files": total_files,
            "would_delete_bytes": total_bytes,
            "astrbot_size_bytes": astrbot_size,
            "astrbot_threshold_bytes": self.settings()["astrbot_cache_threshold_mb"] * MIB,
            "astrbot_error": astrbot_error,
            "dir_source": self.active_dirs()[1],
            "modules_pending": sum(
                item["module_count"]
                for item in self.module_cache.describe()
                if not item["activated"] and not item["self"]
            ),
        }

    # ---------- 供面板直接触发 ----------

    async def purge_plugin_modules(self, plugin_id: str) -> dict[str, Any]:
        try:
            if self.module_cache.is_active(plugin_id):
                return {"ok": False, "error": "该插件仍在运行，请先在 WebUI 停用它再清模块"}
            removed = self.module_cache.purge_loaded(plugin_id)
        except ModuleCacheError as exc:
            return {"ok": False, "error": str(exc)}
        record = {
            "layer": "modules",
            "label": LAYER_LABELS["modules"],
            "at": _now_iso(),
            "acted": bool(removed),
            "freed_bytes": 0,
            "items": len(removed),
            "skipped": "" if removed else "该插件没有已加载的模块",
            "error": "",
            "detail": f"{plugin_id} 移除 {len(removed)} 个模块" if removed else "",
            "elapsed_ms": 0,
        }
        self._record_layer("modules", record)
        await self._push_history([record])
        return {"ok": True, "error": "", "record": record}
