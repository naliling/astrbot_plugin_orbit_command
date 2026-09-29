from __future__ import annotations

import asyncio
import json
import shutil
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
    _MEDIA_EXTENSIONS,
)
from .discovery import cache_dirs_sync, identity_for, identity_sync, normalize_endpoint
from .napcat import NapCatCacheClient
from .spaces import (
    log_inventory,
    platform_audit,
    reverse_ws_only_async,
    log_inventory_sync,
    log_retention_clean,
    log_retention_plan,
    orphan_plugin_data,
    plugin_junk_sync,
    purge_all_junk,
    temp_cleanup,
    temp_scan,
)

MIB = 1024 * 1024

LAYERS = (
    "modules",
    "astrbot",
    "astrbot_logs",
    "napcat_files",
    "napcat_protocol",
    "media_files",
    "plugin_junk",
    "temp_files",
)

LAYER_LABELS = {
    "modules": "停用插件模块",
    "astrbot": "AstrBot 磁盘缓存",
    "astrbot_logs": "AstrBot 日志",
    "napcat_files": "NapCat 文件缓存",
    "napcat_protocol": "NapCat 协议缓存",
    "media_files": "自选目录媒体文件",
    "plugin_junk": "插件目录垃圾",
    "temp_files": "AstrBot 临时文件",
}

_HISTORY_LIMIT = 200
_AUTO_DIR_TTL = 1800.0

# 「连续失联」计数最多隔这么久才推进一次。面板每 30 秒刷一次，不设闸的话
# 一个只是暂时没启动的 bot 会在半小时内被推成「长期失联」。
_REACH_ADVANCE_INTERVAL = 900.0

# 「下次体检」过期多久就当成没在跑。cron 最短也要几分钟一轮，
# 留半小时容差，免得刚过点就误报。
_SCHEDULE_OVERDUE_SECONDS = 1800

# 「知道了」之后安静多久。12 小时：够把这个点过去的事翻篇，
# 又不至于让一个持续一整周的问题彻底消失。
_ATTENTION_SNOOZE_SECONDS = 12 * 3600

_SETTING_BOUNDS = {
    "astrbot_cache_threshold_mb": (1, 1024 * 1024),
    "astrbot_logs_threshold_mb": (1, 1024 * 1024),
    "napcat_cache_threshold_mb": (1, 1024 * 1024),
    "napcat_cache_min_age_minutes": (0, 10080),
    "napcat_protocol_interval_hours": (1, 24 * 365),
    "media_threshold_mb": (1, 1024 * 1024),
    "media_min_age_days": (1, 3650),
    "log_retention_days": (0, 3650),
    # 「疑似失联」要连上几次才算。以前写死 3 次，配合 15 分钟的推进闸，
    # 一个挂掉的 bot 最快 45 分钟才露面——看起来就像「检测不到」。
    "unreachable_limit": (1, 20),
}

# 外观偏好。存在插件配置里而不是只靠浏览器 localStorage：
# 面板跑在 iframe 里，沙箱一旦没给 allow-same-origin，localStorage 会直接抛
# SecurityError，写入静默失败，自定义主色就“保存不住”。
_UI_THEMES = ("deep", "cyber", "matrix", "light")


def validate_accent(value: Any) -> str:
    """校验 #rgb / #rrggbb。返回空串表示合法。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if not text.startswith("#") or len(text) not in (4, 7):
        return "主色要写成 #rgb 或 #rrggbb 的形式"
    try:
        int(text[1:], 16)
    except ValueError:
        return f"主色「{text}」不是合法的十六进制颜色"
    return ""

_TARGET_RATIO = 0.85


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _disk_free(path: Path) -> int | None:
    """磁盘剩余字节。取不到就返回 None，不猜。"""
    try:
        return shutil.disk_usage(path).free
    except (OSError, ValueError):
        return None


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


# cron 字段的范围：(最小, 最大)。day-of-week 用 0-7，7 与 0 都表示周日。
_CRON_FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_CRON_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}


def _cron_value(token: str, low: int, high: int) -> int | None:
    text = token.strip().lower()
    if text in _CRON_NAMES:
        return _CRON_NAMES[text]
    try:
        value = int(text)
    except ValueError:
        return None
    # day-of-week 允许写 7 表示周日
    return value if low <= value <= (7 if high == 7 else high) else None


def validate_cron(expression: str) -> str:
    """校验五段 crontab。返回空串表示合法，否则返回给用户看的原因。

    之前只数了一下是不是五段，`99 99 99 99 99` 这种也能存进去，
    然后定时任务注册静默失败，面板上却显示已保存。
    """
    text = str(expression or "").strip()
    if not text:
        return "体检频率不能为空"
    parts = text.split()
    if len(parts) != 5:
        return f"体检频率需要是五段标准 crontab，你填了 {len(parts)} 段"
    labels = ("分钟", "小时", "日", "月", "星期")
    for part, (low, high), label in zip(parts, _CRON_FIELDS, labels):
        for chunk in part.split(","):
            step = chunk.split("/")
            if len(step) > 2:
                return f"{label}字段「{chunk}」的 / 写法不对"
            if len(step) == 2:
                try:
                    if int(step[1]) <= 0:
                        return f"{label}字段的步长必须大于 0"
                except ValueError:
                    return f"{label}字段的步长「{step[1]}」不是数字"
            base = step[0]
            if base == "*":
                continue
            bounds = base.split("-")
            for item in bounds:
                if _cron_value(item, low, high) is None:
                    return f"{label}字段「{item}」超出范围（{low}-{high}）"
            if len(bounds) == 2:
                start = _cron_value(bounds[0], low, high)
                end = _cron_value(bounds[1], low, high)
                # 反向区间（5-1）大部分实现会直接报错，别放它过去
                if start is not None and end is not None and start > end:
                    return f"{label}字段的区间「{base}」起点比终点大"
    return ""


def _astrbot_target(status: Any, target: str) -> tuple[int, str]:
    """从 StorageCleaner.get_status() 里取某一块的 (字节数, 展示文案)。

    上游一次就把 logs 与 cache 都返回了，所以两层共用一次扫描结果。
    """
    block = status.get(target, {}) if isinstance(status, dict) else {}
    if not isinstance(block, dict):
        return 0, "0 个文件"
    try:
        size = int(block.get("size_bytes", 0) or 0)
    except (TypeError, ValueError):
        size = 0
    return size, f"{block.get('file_count', 0)} 个文件"


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
        self._restored = False
        self._state = self._load_state()
        self._history = self._load_history()
        # 「热重载后累计清零」这种事，光看面板分不清是自己没跑过还是记录丢了。
        # 记下来：本次是新开张，还是接上了上次的记录。
        self._restored = bool(
            self._state.get("total_freed_bytes") or self._state.get("sweep_count")
        )
        self._schedule: dict[str, Any] = {"cron": "", "next_run": "", "job_id": ""}
        self._auto_dirs: list[dict[str, Any]] = []
        self._auto_dirs_at = 0.0
        self._reach_at = 0.0

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
        self._auto_dirs = [
            row for row in rows if row.get("safe") and not row.get("truncated")
        ]
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
        return self._string_list("napcat_cache_dirs")

    def _theme_pref(self) -> str:
        value = str(self._config.get("ui_theme", "") or "").strip()
        return value if value in _UI_THEMES else ""

    def _accent_pref(self) -> str:
        value = str(self._config.get("ui_accent", "") or "").strip().lower()
        return value if not validate_accent(value) else ""

    def _media_dirs(self) -> list[str]:
        return self._string_list("media_dirs")

    def _log_dirs(self) -> list[str]:
        return self._string_list("log_dirs")

    @staticmethod
    def _string_list_from(raw: Any) -> list[str]:
        if isinstance(raw, str):
            return [line.strip() for line in raw.splitlines() if line.strip()]
        if isinstance(raw, list):
            return [str(item).strip() for item in raw if str(item).strip()]
        return []

    def _string_list(self, key: str) -> list[str]:
        return self._string_list_from(self._config.get(key, []))

    def _validate_log_dirs(self, values: list[str]) -> str:
        """日志目录只校验「能当成目录」。

        不套 _validate_directories 那套重叠检查：日志落在 data 目录之外
        （挂载卷、/var/log）是正常部署，硬拦只会让人没法填。
        """
        for value in values:
            path = Path(value).expanduser()
            if not path.is_absolute():
                return f"日志目录必须是绝对路径: {value}"
            if path.is_symlink():
                return f"日志目录不能是符号链接: {value}"
            if path.exists() and not path.is_dir():
                return f"日志目录指向的不是目录: {value}"
        return ""

    def _dirs(self) -> list[str]:
        return self._string_list("napcat_cache_dirs")

    def _token_dirs(self) -> list[str]:
        """用户显式指定的额外搜索根。"""
        return self._string_list("napcat_config_dirs")

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
            "astrbot_logs_threshold_mb": self._int(
                "astrbot_logs_threshold_mb", 16, *_SETTING_BOUNDS["astrbot_logs_threshold_mb"]
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
            "media_dirs": self._media_dirs(),
            "media_threshold_mb": self._int(
                "media_threshold_mb", 512, *_SETTING_BOUNDS["media_threshold_mb"]
            ),
            "media_min_age_days": self._int(
                "media_min_age_days", 30, *_SETTING_BOUNDS["media_min_age_days"]
            ),
            "log_retention_days": self._int(
                "log_retention_days", 0, *_SETTING_BOUNDS["log_retention_days"]
            ),
            "unreachable_limit": self._int(
                "unreachable_limit", 3, *_SETTING_BOUNDS["unreachable_limit"]
            ),
            "auto_clean_plugin_junk": self._bool("auto_clean_plugin_junk", False),
            "log_dirs": self._log_dirs(),
            "ui_theme": self._theme_pref(),
            "ui_accent": self._accent_pref(),
            "target_ratio": _TARGET_RATIO,
        }

    def update_settings(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """校验并落盘面板提交的可调项。返回 (settings, error)，error 非空表示未生效。"""
        errors: list[str] = []
        if not isinstance(payload, dict):
            return self.settings(), "提交内容不是对象"

        for key in (
            "auto_enabled",
            "auto_clean_plugin_modules",
            "auto_adopt_cache_dirs",
            "auto_clean_plugin_junk",
        ):
            if key in payload:
                self._config[key] = bool(payload[key])

        if "sweep_cron" in payload:
            expression = str(payload["sweep_cron"] or "").strip()
            problem = validate_cron(expression)
            if problem:
                errors.append(problem)
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

        for key in ("napcat_cache_dirs", "media_dirs", "log_dirs"):
            if key not in payload:
                continue
            values = self._string_list_from(payload[key])
            # 先试建一次：不合法就当场报错，而且**不写盘**。
            # 之前是先报错、再把值存进去，用户看到「没保存」但配置其实已经变了。
            if key == "media_dirs":
                try:
                    self._build_media_cleaner(values)
                except ValueError as exc:
                    errors.append(f"自选目录配置有误：{exc}")
                else:
                    self._config[key] = values
            elif key == "log_dirs":
                problem = self._validate_log_dirs(values)
                if problem:
                    errors.append(problem)
                else:
                    self._config[key] = values
            else:
                self._config[key] = values

        if "ui_theme" in payload:
            value = str(payload["ui_theme"] or "").strip()
            if value and value not in _UI_THEMES:
                errors.append(f"未知的配色：{value}")
            else:
                self._config["ui_theme"] = value

        if "ui_accent" in payload:
            value = str(payload["ui_accent"] or "").strip().lower()
            problem = validate_accent(value)
            if problem:
                errors.append(problem)
            else:
                self._config["ui_accent"] = value

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

    # ---------- 自选目录媒体文件清理器 ----------

    def _build_media_cleaner(self, directories: list[str]) -> NapCatCacheDirectoryCleaner:
        settings = self.settings()
        return NapCatCacheDirectoryCleaner(
            directories,
            threshold_mb=settings["media_threshold_mb"],
            min_age_minutes=settings["media_min_age_days"] * 24 * 60,
            target_ratio=_TARGET_RATIO,
            extensions=_MEDIA_EXTENSIONS,
        )

    def media_cleaner(self) -> tuple[NapCatCacheDirectoryCleaner, str]:
        """返回 (清理器, 错误文案)。目录没填就是关着的。"""
        directories = self._media_dirs()
        if not directories:
            return NapCatCacheDirectoryCleaner([]), ""
        try:
            return self._build_media_cleaner(directories), ""
        except ValueError as exc:
            return NapCatCacheDirectoryCleaner([]), str(exc)

    # ---------- 状态与历史 ----------

    def _load_state(self) -> dict[str, Any]:
        default = {
            "layers": {},
            "unreachable": {},
            "last_sweep_at": "",
            "last_freed_bytes": 0,
            "total_freed_bytes": 0,
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

    async def save_state(self) -> None:
        """把当前状态落盘。

        体检流程里的改动由 _sweep_locked 统一落盘；但「标记已读」这类
        发生在体检之外的动作得自己存，否则一重启就丢。
        """
        await self._persist()

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
        try:
            await self.auto_dirs()
        except Exception as exc:
            logger.warning("Orbit Cache 自动接管缓存目录失败：%s", type(exc).__name__)
        cleaner = self.fs_cleaner()
        dirs, dir_source = self.active_dirs()
        remote, astrbot_cache, napcat_fs, log_scan, junk_scan, identities = (
            await asyncio.gather(
                self.napcat_client.status(),
                self.astrbot_cache.status(),
                cleaner.status(),
                log_inventory(self._log_dirs()),
                asyncio.to_thread(plugin_junk_sync),
                self._static_identities(),
                return_exceptions=True,
            )
        )
        # 这几项都单独包一层：任何一项出错只让它自己变成空结果，
        # 绝不能把整个 collect() 抽掉。它们里有**同步求值**的调用
        # （known_ids），放在 gather 前面，它一抛就等于 overview 整个 500，
        # 面板上所有卡片永远停在「正在读取」。
        temp_info: Any = {}
        orphan_info: Any = {}
        audit_info: Any = {}
        reverse_info: Any = {}
        try:
            temp_info = await temp_scan()
        except Exception as exc:
            logger.warning("Orbit Cache 临时目录扫描失败：%s", type(exc).__name__)
        try:
            loaded = self.module_cache.known_ids()
        except Exception as exc:
            logger.warning("Orbit Cache 读插件注册表失败：%s", type(exc).__name__)
            loaded = set()
        try:
            orphan_info = await orphan_plugin_data(loaded)
        except Exception as exc:
            logger.warning("Orbit Cache 插件数据残留扫描失败：%s", type(exc).__name__)
        try:
            audit_info = await platform_audit(self._token_dirs())
        except Exception as exc:
            logger.warning("Orbit Cache 平台配置体检失败：%s", type(exc).__name__)
        try:
            reverse_info = await reverse_ws_only_async(self._token_dirs())
        except Exception as exc:
            logger.warning("Orbit Cache 探测 NapCat 接入方式失败：%s", type(exc).__name__)
        if not isinstance(temp_info, dict):
            temp_info = {}
        if not isinstance(orphan_info, dict):
            orphan_info = {}
        if not isinstance(audit_info, dict):
            audit_info = {}
        if not isinstance(reverse_info, dict):
            reverse_info = {}
        logs_info = log_scan if isinstance(log_scan, dict) else {}
        junk_info = junk_scan if isinstance(junk_scan, dict) else {}
        if not isinstance(identities, dict):
            identities = {"exact": {}, "loose": {}}
        # collect() 本来就探测了，直接拿这份结果推进失联计数，不额外发请求
        self._note_reachability(remote)
        self._note_last_ok(remote)
        fs_bytes = 0
        fs_files = 0
        if not isinstance(napcat_fs, Exception):
            fs_bytes = sum(int(item.size_bytes) for item in napcat_fs)
            fs_files = sum(int(item.file_count) for item in napcat_fs)

        settings = self.settings()
        threshold_fs = settings["napcat_cache_threshold_mb"] * MIB
        threshold_astrbot = settings["astrbot_cache_threshold_mb"] * MIB
        threshold_logs = settings["astrbot_logs_threshold_mb"] * MIB
        media_cleaner, media_error = self.media_cleaner()
        media_scans = await media_cleaner.status()
        media_bytes = sum(int(item.size_bytes) for item in media_scans)
        threshold_media = settings["media_threshold_mb"] * MIB
        has_media = bool(media_cleaner.directories)
        astrbot_bytes, astrbot_detail, logs_bytes, logs_detail = 0, "", 0, ""
        if isinstance(astrbot_cache, Exception):
            astrbot_detail = f"读取失败（{type(astrbot_cache).__name__}）"
            logs_detail = astrbot_detail
        else:
            astrbot_bytes, astrbot_detail = _astrbot_target(astrbot_cache, "cache")
            logs_bytes, logs_detail = _astrbot_target(astrbot_cache, "logs")
        # 上游那个数只当附注。终端里明明满屏日志、面板上却报 0 B，几乎总是
        # 因为 AstrBot 写日志的地方和 data/logs 根本不是同一个——把两个口径
        # 都摆出来（而不是二选一），差异本身就是最有用的线索。
        upstream_bytes = logs_bytes
        if logs_info.get("missing"):
            logs_detail = f"没找到日志目录（试过 {len(logs_info.get('roots') or [])} 个位置）"
        elif logs_info.get("total_bytes"):
            logs_detail = (
                f"自测 {human_bytes(int(logs_info['total_bytes']))} 于 "
                f"{logs_info.get('root') or '未知路径'}；"
                f"上游报 {human_bytes(upstream_bytes)}"
            )
        logs_state = {
            **logs_info,
            "upstream_bytes": upstream_bytes,
            "growth": self._log_growth(int(logs_info.get("total_bytes", 0) or 0)),
        }

        protocol_state = self._layer_state("napcat_protocol")
        instance_state = self._instance_state()
        try:
            enabled = self.napcat_client.enabled_endpoints
        except Exception:
            enabled = []
        protocol_due = self._protocol_due() and bool(enabled)
        # describe() 也在 collect 的关键路径上：它一抛，整个 overview 就 500，
        # 面板上每一张卡同时变空。没读到插件模块不该让整个面板读不出来。
        try:
            modules = self.module_cache.describe()
        except Exception as exc:
            logger.warning("Orbit Cache 读取插件模块失败：%s", type(exc).__name__)
            modules = []
        instance_status = self._instance_status(remote, identities)
        # status 是新字段；旧版或测试替身可能只给 activated，这时按旧语义补一个。
        for item in modules:
            item.setdefault(
                "status", "running" if item.get("activated") else "stopped"
            )
        for item in modules:
            item.setdefault("status", "running" if item.get("activated") else "stopped")
        reclaimable_modules = sum(
            item["module_count"] for item in modules
            if item.get("status") == "stopped" and not item.get("self")
        )
        unknown_modules = sum(1 for item in modules if item.get("status") == "unknown")
        protocol_hours = settings["napcat_protocol_interval_hours"]
        has_dirs = bool(cleaner.directories)
        layer_rows = {
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
                "why": (
                    f"{unknown_modules} 个插件状态未知，已跳过自动清理"
                    if unknown_modules
                    else (
                        f"{reclaimable_modules} 个停用插件的模块可回收"
                        if reclaimable_modules
                        else "没有停用插件仍占用内存"
                    )
                ),
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
                "why": (
                    f"扫描失败（{type(astrbot_cache).__name__}）"
                    if isinstance(astrbot_cache, Exception)
                    else (
                        f"占用 {human_bytes(astrbot_bytes)}，未超阈值 {human_bytes(threshold_astrbot)}"
                        if astrbot_bytes <= threshold_astrbot
                        else f"占用 {human_bytes(astrbot_bytes)} 已超阈值，等待定时体检"
                    )
                ),
                "config_error": "",
                "directories": [],
                "last": self._layer_state("astrbot"),
            },
            "astrbot_logs": {
                "label": LAYER_LABELS["astrbot_logs"],
                "current": logs_bytes,
                "unit": "bytes",
                "amount": f"{human_bytes(logs_bytes)} / 阈值 {human_bytes(threshold_logs)}",
                "threshold": threshold_logs,
                "ratio": _ratio(logs_bytes, threshold_logs),
                "due": logs_bytes > threshold_logs,
                "enabled": True,
                "detail": logs_detail,
                "logs": logs_state,
                "why": (
                    logs_detail
                    if (logs_info.get("missing") or logs_info.get("total_bytes"))
                    else "没扫到日志文件"
                ),
                "config_error": "",
                "directories": [],
                "last": self._layer_state("astrbot_logs"),
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
                "detail": f"{len(cleaner.directories)} 个目录 / {fs_files} 个文件"
                + (
                    "（有目录未测完，占用可能偏低）"
                    if isinstance(napcat_fs, list)
                    and any(getattr(item, "truncated", False) for item in napcat_fs)
                    else ""
                )
                if isinstance(napcat_fs, list)
                else f"扫描失败（{type(napcat_fs).__name__}）",
                "why": (
                    "尚未配置或接管到任何缓存目录"
                    if not has_dirs
                    else (
                        f"占用 {human_bytes(fs_bytes)}，未超阈值 {human_bytes(threshold_fs)}"
                        if fs_bytes <= threshold_fs
                        else f"占用 {human_bytes(fs_bytes)} 已超阈值 {human_bytes(threshold_fs)}，等待定时体检"
                    )
                ),
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
                "why": (
                    "没有启用中的 NapCat 实例"
                    if not enabled
                    else (
                        # 机器人明明在跑，这一层却永远报失败——通常是因为
                        # NapCat 只配了反向连接、根本不往外监听。
                        # 说清楚「这层用不上」，比每 15 分钟报一次连不上有用。
                        "你的 NapCat 只配了反向连接、不对外提供 OneBot 接口，"
                        "这一层在你的部署上用不上"
                        if reverse_info.get("configs") and not reverse_info.get("has_server")
                        else (
                            "已到清理时间"
                            if protocol_due
                            else f"距上次清理未超过 {protocol_hours} 小时"
                        )
                    )
                ),
                "directories": [],
                "last": protocol_state,
            },
            "media_files": {
                "label": LAYER_LABELS["media_files"],
                "current": media_bytes,
                "unit": "bytes",
                "amount": (
                    f"{human_bytes(media_bytes)} / 阈值 {human_bytes(threshold_media)}"
                    if has_media
                    else "未填目录（默认关着）"
                ),
                "threshold": threshold_media if has_media else None,
                "ratio": _ratio(media_bytes, threshold_media) if has_media else None,
                "due": media_bytes > threshold_media and has_media,
                "enabled": has_media,
                "detail": (
                    f"{len(media_cleaner.directories)} 个目录 / "
                    f"只删超过 {settings['media_min_age_days']} 天的图片视频音频"
                ),
                "why": (
                    "没有填自选目录，这层关着"
                    if not has_media
                    else (
                        f"媒体 {human_bytes(media_bytes)}，未超阈值 {human_bytes(threshold_media)}"
                        if media_bytes <= threshold_media
                        else f"媒体 {human_bytes(media_bytes)} 已超阈值，等待定时体检"
                    )
                ),
                "config_error": media_error,
                "directories": [
                    {
                        "path": str(item.path),
                        "exists": item.exists,
                        "file_count": item.file_count,
                        "size_bytes": item.size_bytes,
                        "skipped_symlinks": item.skipped_symlinks,
                        "error": item.error,
                    }
                    for item in media_scans
                ],
                "last": self._layer_state("media_files"),
            },
            "plugin_junk": {
                "label": LAYER_LABELS["plugin_junk"],
                "current": int(junk_info.get("total_bytes", 0) or 0),
                "unit": "bytes",
                "amount": (
                    f"{human_bytes(int(junk_info.get('total_bytes', 0) or 0))} / "
                    f"{(junk_info.get('all') or {}).get('count', 0)} 项"
                    if junk_info.get("root_exists")
                    else "插件目录不存在"
                ),
                "threshold": None,
                "ratio": None,
                "due": bool((junk_info.get("all") or {}).get("count")),
                "enabled": settings["auto_clean_plugin_junk"],
                "detail": (
                    f"没装完 {len(junk_info.get('broken') or [])} · "
                    f"安装包 {len(junk_info.get('zips') or [])} · "
                    f"__pycache__ {len(junk_info.get('pycache') or [])}"
                ),
                "why": (
                    "自动清除已关闭，可在面板上手动清除"
                    if not settings["auto_clean_plugin_junk"]
                    else (
                        f"{(junk_info.get('all') or {}).get('count', 0)} 项可清，"
                        f"其中 {int(junk_info.get('protected_count') or 0)} 项在 7 天年龄锁内"
                    )
                ),
                "config_error": "",
                "directories": [],
                "last": self._layer_state("plugin_junk"),
            },
            "temp_files": {
                "label": LAYER_LABELS["temp_files"],
                "current": int(temp_info.get("total_bytes", 0) or 0),
                "unit": "bytes",
                "amount": (
                    f"{human_bytes(int(temp_info.get('total_bytes', 0) or 0))} / "
                    f"{temp_info.get('scanned_files', 0)} 个临时文件"
                    if temp_info.get("exists")
                    else "没有临时目录"
                ),
                "threshold": None,
                "ratio": None,
                "due": bool(temp_info.get("deleted_files")),
                "enabled": True,
                "detail": (
                    str(temp_info.get("root", ""))
                    + (
                        f"；最旧的文件 {temp_info.get('oldest_days')} 天"
                        if temp_info.get("oldest_days") is not None else ""
                    )
                ),
                "why": (
                    f"{temp_info.get('too_recent', 0)} 个文件在 7 天年龄锁内，"
                    "超过年龄锁的会被清掉"
                    if temp_info.get("exists")
                    else "AstrBot 没有建临时目录，可能是官方路径没拿到"
                ),
                "config_error": "",
                "directories": [],
                "last": self._layer_state("temp_files"),
            },
        }

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
                "reclaimable_bytes": astrbot_bytes + logs_bytes + fs_bytes + media_bytes,
                "reclaimable_modules": reclaimable_modules,
                "last_sweep_at": self._state.get("last_sweep_at", ""),
                "last_freed_bytes": self._state.get("last_freed_bytes", 0),
                "total_freed_bytes": self._state.get("total_freed_bytes", 0),
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
            "layers": layer_rows,
            "instance_status": instance_status,
            "orphan_plugin_data": orphan_info,
            "platform_audit": audit_info,
            "diagnostics": {
                "state_path": str(self._state_path),
                "state_exists": self._state_path.is_file(),
                "history_records": len(self._history),
                "restored": self._restored,
            },
            "reverse_only": reverse_info,
            "attention": self._attention(
                instance_status=instance_status,
                layers=layer_rows,
                modules=modules,
                schedule={
                    **self._schedule,
                    "cron": self.sweep_cron,
                    "cron_effective": self._schedule.get("cron", ""),
                },
                junk=junk_info,
                orphan=orphan_info,
                audit=audit_info,
                reverse_info=reverse_info,
            ),
            "module_rows": modules,
        }

    def _protocol_due(self) -> bool:
        return any(
            self._protocol_due_for(item.url, item.protocol_interval_hours)
            for item in self.napcat_client.enabled_endpoints
        )

    def _log_growth(self, current: int) -> int | None:
        """日志比上一次记录多了多少字节。

        只有跨过一天才有「24 小时增长」那个含义；面板 30 秒刷一次，
        两次之间多半只隔了半分钟，所以这里返回的是「距上次记录」，
        字段也叫 growth 而不是 growth_24h，不骗人。第一次没有上次，返回 None。
        """
        history = self._state.get("log_sizes")
        history = dict(history) if isinstance(history, dict) else {}
        key = datetime.now().astimezone().date().isoformat()
        previous = history.get(key)
        history[key] = current
        self._state["log_sizes"] = {k: history[k] for k in sorted(history)[-8:]}
        if previous is None:
            return None
        return current - int(previous)

    # ---------- 需要处理的事 ----------

    def _instance_status(
        self, remote: Any, identities: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """每个实例现在到底是什么状态。

        以前面板只给一个「连接正常 / 异常」，而「异常」把「你自己停用的」
        「服务没启动」「Token 错了」全糊在一起——这几种要采取的动作完全不同。
        账号取自磁盘上的 onebot11_*.json，所以连不上时也能显示。
        """
        endpoints = self.napcat_client.endpoints
        rows = remote if isinstance(remote, list) else []
        by_url = {str(item.get("url", "")): item for item in rows}
        counters = self._unreachable_counters()
        limit = self.settings()["unreachable_limit"]
        last_ok = self._state.get("last_ok_at")
        last_ok = last_ok if isinstance(last_ok, dict) else {}
        seen_account: dict[str, int] = {}
        status_rows: list[dict[str, Any]] = []
        for index, endpoint in enumerate(endpoints):
            row = by_url.get(endpoint.label, {})
            identity = identity_for(endpoint.url, identities)
            account = str(identity.get("account") or "")
            duplicate_with = ""
            if account and account in seen_account:
                duplicate_with = endpoints[seen_account[account]].label
            elif account:
                seen_account[account] = index
            # state 缺了（老版本后端、或探测结果被裁剪过）就退回 connected，
            # 不能因为字段缺失就一律当成 unknown——那会把好好的实例报成连不上。
            state = str(row.get("state") or ("running" if row.get("connected") else "unknown"))
            note = str(row.get("error") or "")[:120]
            if state not in ("running", "disabled") and int(counters.get(endpoint.url, 0)) >= limit:
                state = "dead"
            # 区分两种「连不上」，因为要采取的动作完全不同：
            #   · 磁盘上有对应的 onebot 配置 → 地址没填错，多半是 NapCat 进程没起
            #   · 磁盘上找不到这个地址       → 地址填错了，或那个 NapCat 根本不往外监听
            if state not in ("running", "disabled") and not duplicate_with:
                if identity.get("file"):
                    hint = (
                        f"磁盘上有 {Path(identity['file']).name} 的配置，"
                        "但连不上——多半是那个 NapCat 进程没在跑"
                    )
                else:
                    hint = (
                        "磁盘上的 onebot11*.json 里找不到这个地址："
                        "要么地址填错了，要么那个 NapCat 压根没开对外端口"
                    )
            else:
                hint = note
            status_rows.append({
                "url": endpoint.label,
                "enable": endpoint.enable,
                "state": state,
                "account": account,
                "note": duplicate_with or note,
                "hint": duplicate_with or hint,
                "duplicate_with": duplicate_with,
                "last_ok_at": str(last_ok.get(endpoint.url, "")),
            })
        return status_rows

    def _schedule_problem(self) -> str:
        """定时体检到底跑没跑。返回空串表示一切正常。

        「上次体检」是过去时；「下次体检」是计划时——计划时已经过去很久而体检
        没发生，说明任务压根没执行（handler 抛异常、进程重启后任务丢了）。
        这种情况面板上看起来一片正常，所以必须单独查。
        """
        schedule = self._schedule
        if not self.settings()["auto_enabled"]:
            return ""   # 用户主动暂停的，不是故障
        if not schedule.get("job_id"):
            return "定时任务没有注册上，自动清理现在是停的"
        next_run = str(schedule.get("next_run") or "").strip()
        if not next_run:
            return "读不到下次执行时间，任务可能没在跑"
        try:
            planned = datetime.fromisoformat(next_run)
        except ValueError:
            return ""
        if planned.tzinfo is None:
            return ""
        overdue = (datetime.now(planned.tzinfo) - planned).total_seconds()
        if overdue > _SCHEDULE_OVERDUE_SECONDS:
            return f"体检计划在 {next_run} 执行，现在已经过期 {int(overdue // 60)} 分钟"
        return ""

    def _attention(
        self,
        *,
        instance_status: list[dict[str, Any]],
        layers: dict[str, Any],
        modules: list[dict[str, Any]],
        schedule: dict[str, Any],
        junk: dict[str, Any],
        orphan: dict[str, Any] | None = None,
        audit: dict[str, Any] | None = None,
        reverse_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把所有「需要人处理」的事汇成一条。

        本插件只在面板里提醒、不推送，所以这一条是它唯一的传达途径，
        必须是打开面板第一眼就能看见的东西，而不是散在各张卡片角落的附注。
        """
        raw: list[dict[str, Any]] = []
        # 这几项都是「有就用、没有就算了」的补充信息，直接调 _attention 的地方
        # 不会传它们，所以先归一成空字典再往下用。
        orphan = orphan or {}
        audit = audit or {}
        reverse_info = reverse_info or {}

        def add(
            key: str, level: str, title: str, detail: str, action: dict | None = None
        ) -> None:
            item = {"id": key, "level": level, "title": title, "detail": detail}
            if action:
                # 待办条目可以直接带处置动作。重复实例那一类以前只能「知道了」，
                # 想删还得自己翻到下面的卡片去找——于是看起来就像「删不掉」。
                item["action"] = action
            raw.append(item)

        for row in instance_status:
            # 重复要排在「状态好不好」前面：一条正在运行的重复配置仍然是多余的，
            # 先因为「它连得上」而跳过，用户就永远看不到「这条是重复的」。
            if row["duplicate_with"]:
                add(
                    f"instance-dup:{row['url']}", "bad", "重复实例",
                    f"{row['url']} 与 {row['duplicate_with']} 指向同一个 QQ 号。",
                    action={"type": "remove_instance", "url": row["url"]},
                )
                continue
            if row["state"] in ("running", "disabled"):
                continue
            if row["state"] == "dead":
                add(
                    f"instance-dead:{row['url']}", "warn", "疑似失联",
                    f"{row['url']} 连续多次连不上。分不清是停用还是下线，不提供删除。",
                )
            else:
                add(
                    f"instance-{row['state']}:{row['url']}", "warn", "实例连不上",
                    f"{row['url']}：{row.get('hint') or row['note'] or row['state']}",
                )
        for key, layer in layers.items():
            if layer.get("config_error"):
                add(
                    f"layer-config:{key}", "bad", f"{layer['label']} 配置有误",
                    str(layer["config_error"]),
                )
            last = layer.get("last") or {}
            # id 不带 last_run_at：带了它等于每 15 分钟换一个新问题，
            # 「未读」永远消不掉，而且同一个故障会在待办表里堆出一串。
            if last.get("last_error"):
                add(
                    f"layer-error:{key}", "bad",
                    f"{layer['label']} 执行失败", str(last["last_error"]),
                )
        unknown = [
            item for item in modules
            if item.get("status") == "unknown"
        ]
        if unknown:
            add(
                "modules-unknown", "warn", "有插件状态未知",
                f"{len(unknown)} 个已加载的插件拿不到元数据，已跳过自动清理："
                + "、".join(str(item["plugin_id"]) for item in unknown[:4]),
            )
        schedule_problem = self._schedule_problem()
        if schedule_problem:
            add("schedule-unregistered", "bad", "定时体检没在跑", schedule_problem)
        protected = int(junk.get("protected_count") or 0)
        if protected:
            add(
                "junk-protected", "info", f"{protected} 项垃圾在年龄锁内",
                "最近 7 天内的目录不动，避免打断正在安装的插件。",
            )
        # 磁盘上的 NapCat 全都只配了反向连接 → 协议层在这套部署上用不上。
        # 这是**环境说明**不是故障，所以用 info 级：不该占着首屏红字不放，
        # 但也得让人知道「这一层为什么一直连不上」。
        if reverse_info.get("configs") and not reverse_info.get("has_server"):
            add(
                "reverse-only", "info", "这套部署用不上协议缓存",
                f"磁盘上 {reverse_info['configs']} 个 NapCat 配置都只有反向连接，"
                "没有对外监听的 OneBot 端口。协议缓存这一层在这里不适用，"
                "不会也不需要去清它。",
            )
        leftovers = (orphan or {}).get("items") or []
        if leftovers:
            # 一律用 .get 拼文案：这条路径上的字段来自扫描结果，
            # 少一个键就把整个 overview 打成 500，面板全废。
            top = "、".join(
                "{}（{}）".format(
                    item.get("label") or Path(str(item.get("path", ""))).name or "未知目录",
                    human_bytes(int(item.get("size_bytes") or 0)),
                )
                for item in leftovers[:3]
            )
            add(
                "orphan-plugin-data", "info",
                f"{len(leftovers)} 个插件数据目录没人用了",
                f"对应的插件已不在 AstrBot 里：{top}。只提示，要删由你在面板上点。",
            )
        for item in (audit or {}).get("conflicts") or []:
            level = "bad" if item.get("kind") == "duplicate" else "warn"
            title = (
                "重复的平台配置" if item.get("kind") == "duplicate" else "端口抢占"
            )
            add(
                f"platform-conflict:{item.get('file','')}:{item.get('names')}",
                level, title,
                str(item.get("detail") or "")
                + " 涉及：" + "、".join(str(n) for n in item.get("names") or []),
            )
        return self._mark_attention(raw)

    def _mark_attention(self, raw: list[dict[str, Any]]) -> dict[str, Any]:
        seen_map = self._state.get("attention_seen")
        seen_map = dict(seen_map) if isinstance(seen_map, dict) else {}
        now = _now_iso()
        items: list[dict[str, Any]] = []
        for item in raw:
            first_seen = seen_map.get(item["id"])
            if not isinstance(first_seen, str) or not first_seen:
                first_seen = now
            seen_map[item["id"]] = first_seen
            items.append({**item, "at": first_seen})
        # 已经不存在的问题要从表里清掉，否则这张表会无限增长
        alive = {item["id"] for item in raw}
        self._state["attention_seen"] = {k: v for k, v in seen_map.items() if k in alive}
        # 点了「知道了」之后，同一批问题要**真的消失**。以前只把 unread 归零，
        # items 原样返回，而面板是按 items 有没有内容决定显不显示的——
        # 结果就是点了没反应，那条红框一直挂在首屏上，比不提示还烦。
        #
        # 但也不能永久消失：12 小时后问题还在就重新提醒（并告诉用户这是第几次）。
        # 「知道」不等于「解决」，这里管的是别反复打扰。
        now_ts = time.time()
        snooze = self._state.get("attention_snooze")
        snooze = dict(snooze) if isinstance(snooze, dict) else {}
        shown: list[dict[str, Any]] = []
        for item in items:
            entry = snooze.get(item["id"])
            if isinstance(entry, dict) and now_ts < float(entry.get("until") or 0):
                continue
            if isinstance(entry, dict) and int(entry.get("times") or 0) > 0:
                item["title"] = f"{item['title']}（又出现了）"
            shown.append(item)
        ack_at = str(self._state.get("attention_ack_at") or "")
        return {
            "level": "bad" if any(i["level"] == "bad" for i in shown) else (
                "warn" if shown else "ok"
            ),
            "items": shown,
            "unread": len(shown),
            "ack_at": ack_at,
        }

    def ack_attention(self) -> dict[str, Any]:
        """标记「看过了」：当前这一批安静 12 小时。"""
        stamp = _now_iso()
        self._state["attention_ack_at"] = stamp
        snooze = self._state.get("attention_snooze")
        snooze = dict(snooze) if isinstance(snooze, dict) else {}
        until = time.time() + _ATTENTION_SNOOZE_SECONDS
        for item_id in self._state.get("attention_seen") or {}:
            entry = snooze.get(item_id)
            times = int(entry.get("times") or 0) + 1 if isinstance(entry, dict) else 1
            snooze[item_id] = {"until": until, "times": times}
        # 只保留还没过期的，否则这张表会随着时间越来越长
        self._state["attention_snooze"] = {
            k: v for k, v in snooze.items()
            if isinstance(v, dict) and float(v.get("until") or 0) > time.time()
        }
        return {
            "ok": True, "ack_at": stamp,
            "snoozed_until": datetime.fromtimestamp(until).astimezone().isoformat(
                timespec="seconds"
            ),
        }

    def _note_last_ok(self, rows: Any) -> None:
        """记住每个实例最后一次连上的时间。

        「上次成功是什么时候」比「上次运行」有用得多：任务天天跑、天天失败
        的时候，只看「上次运行」根本发现不了。
        """
        if not isinstance(rows, list):
            return
        table = self._state.get("last_ok_at")
        table = dict(table) if isinstance(table, dict) else {}
        changed = False
        for row in rows:
            url = str(row.get("url", ""))
            if url and row.get("connected"):
                table[url] = _now_iso()
                changed = True
        if changed:
            self._state["last_ok_at"] = table

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
        # 「释放了多少字节」是插件自己算的，磁盘不一定认：可能有进程还持着句柄，
        # 也可能删的是稀疏文件。真正说了算的是磁盘，所以清理前后各取一次。
        disk_before = _disk_free(self._data_dir)
        for layer in wanted:
            records.append(await self._run_layer(layer, force=force))
        disk_after = _disk_free(self._data_dir)

        freed = sum(int(item.get("freed_bytes", 0)) for item in records)
        self._state["last_sweep_at"] = _now_iso()
        self._state["last_freed_bytes"] = freed
        self._state["total_freed_bytes"] = (
            int(self._state.get("total_freed_bytes", 0) or 0) + freed
        )
        self._state["sweep_count"] = int(self._state.get("sweep_count", 0)) + 1
        await self._persist()
        await self._push_history(records)

        errors = [item["error"] for item in records if item.get("error")]
        return {
            "ok": not errors,
            "forced": force,
            "at": self._state["last_sweep_at"],
            "freed_bytes": freed,
            "disk_before": disk_before,
            "disk_after": disk_after,
            "disk_freed": (
                disk_after - disk_before
                if disk_before is not None and disk_after is not None else None
            ),
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
                "astrbot_logs": self._run_astrbot_logs,
                "napcat_files": self._run_napcat_files,
                "napcat_protocol": self._run_napcat_protocol,
                "media_files": self._run_media_files,
                "plugin_junk": self._run_plugin_junk,
                "temp_files": self._run_temp_files,
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
        await self._run_astrbot_target(record, force, "cache", threshold)

    async def _run_astrbot_logs(self, record: dict[str, Any], force: bool) -> None:
        days = self.settings()["log_retention_days"]
        if days > 0:
            await self._run_log_retention(record, days)
            return
        threshold = self.settings()["astrbot_logs_threshold_mb"] * MIB
        await self._run_astrbot_target(record, force, "logs", threshold)

    async def _run_log_retention(self, record: dict[str, Any], days: int) -> None:
        """按保留天数清日志。

        不能用上游的 cleanup("logs")：它对非活跃日志是直接 unlink，不看新旧，
        一次就会把 logs 目录清光，连昨天的 astrbot.log.1 都没。当前正在写的
        astrbot.log 也一律不碰——删掉被打开的文件不会马上释放空间，
        之后的新日志会写进那个已删除的 inode，表现为「日志不见了」。
        """
        plan = await log_retention_plan(days)
        candidates = plan.get("candidates", [])
        record["detail"] = (
            f"保留 {days} 天，当前日志 {plan.get('kept_current', 0)} 个不删，"
            f"超期 {len(candidates)} 个"
        )
        if not candidates:
            record["skipped"] = f"没有超过 {days} 天的日志"
            return
        result = await log_retention_clean(days)
        record["items"] = int(result.get("deleted_files", 0))
        record["freed_bytes"] = int(result.get("freed_bytes", 0))
        record["acted"] = record["items"] > 0
        record["detail"] = (
            f"删除 {record['items']} 个超期日志，释放 {record['freed_bytes']} 字节，"
            f"失败 {result.get('failed_files', 0)}，当前日志 {result.get('kept_current', 0)} 个未动"
        )
        if not record["acted"]:
            record["skipped"] = "超期日志没能删掉"

    async def _run_astrbot_target(
        self, record: dict[str, Any], force: bool, target: str, threshold: int
    ) -> None:
        """cache 与 logs 共用同一套判定与记账，只有 target 与阈值不同。"""
        size = await self.astrbot_cache.size_bytes(target)
        record["detail"] = f"当前占用 {size} 字节，阈值 {threshold} 字节"
        if size <= threshold and not force:
            record["skipped"] = f"占用 {size} 字节未超过阈值 {threshold} 字节"
            return
        result = await self.astrbot_cache.clean(target)
        record["freed_bytes"] = int(result.get("removed_bytes", 0) or 0)
        record["items"] = int(result.get("processed_files", 0) or 0)
        # acted 必须是「真的动了手」，不是「调过了接口」：目录本来就是干净的话
        # 强制清理会返回 0 字节，谎报已清理只会让人以为插件没用。
        record["acted"] = record["freed_bytes"] > 0 or record["items"] > 0
        record["detail"] = (
            f"处理 {record['items']} 个文件，释放 {record['freed_bytes']} 字节，"
            f"失败 {result.get('failed_files', 0)}"
        )
        if not record["acted"]:
            record["skipped"] = "已执行，但本来就没有可清理的内容"

    async def _run_napcat_files(self, record: dict[str, Any], force: bool) -> None:
        try:
            await self.auto_dirs()
        except Exception as exc:
            logger.warning("Orbit Cache 自动接管缓存目录失败：%s", type(exc).__name__)
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
        record["freed_bytes"] = sum(int(item["deleted_bytes"]) for item in results)
        record["items"] = sum(int(item["deleted_files"]) for item in results)
        record["acted"] = record["freed_bytes"] > 0 or record["items"] > 0
        record["detail"] = "；".join(
            f"{item['path']} 删除 {item['deleted_files']} 个 / {item['deleted_bytes']} 字节"
            for item in results
        )
        if not record["acted"]:
            record["skipped"] = "已执行，但没有符合条件的文件可删"

    def _instance_state(self) -> dict[str, Any]:
        entry = self._state["layers"].get("napcat_protocol", {})
        value = entry.get("instances") if isinstance(entry, dict) else None
        return value if isinstance(value, dict) else {}

    async def _run_media_files(self, record: dict[str, Any], force: bool) -> None:
        cleaner, error = self.media_cleaner()
        if error:
            record["error"] = error
            return
        if not cleaner.directories:
            record["skipped"] = "没有填自选目录，这层默认关着"
            return
        scans = await cleaner.status()
        total = sum(int(item.size_bytes) for item in scans)
        threshold = cleaner.threshold_bytes
        record["detail"] = (
            f"媒体文件合计 {total} 字节，阈值 {threshold} 字节；"
            f"最小保留 {self.settings()['media_min_age_days']} 天"
        )
        if total <= threshold and not force:
            record["skipped"] = f"媒体文件 {total} 字节未超过阈值 {threshold} 字节"
            return
        results = await cleaner.clean_once(force=force)
        record["freed_bytes"] = sum(int(item["deleted_bytes"]) for item in results)
        record["items"] = sum(int(item["deleted_files"]) for item in results)
        record["acted"] = record["freed_bytes"] > 0 or record["items"] > 0
        record["detail"] = "；".join(
            f"{item['path']} 删除 {item['deleted_files']} 个 / {item['deleted_bytes']} 字节"
            for item in results
        )
        if not record["acted"]:
            record["skipped"] = "已执行，但没有超龄的媒体文件可删"

    def _protocol_due_for(self, url: str, interval_hours: int) -> bool:
        last = self._instance_state().get(url, {}).get("last_run_at")
        if not last:
            return True
        try:
            previous = datetime.fromisoformat(str(last)).timestamp()
        except ValueError:
            return True
        return (time.time() - previous) >= interval_hours * 3600

    async def _run_plugin_junk(self, record: dict[str, Any], force: bool) -> None:
        if not self._bool("auto_clean_plugin_junk", False) and not force:
            record["skipped"] = "插件垃圾自动清理已关闭，可在面板上手动清除"
            return
        result = await purge_all_junk()
        if not result.get("ok") and not result.get("deleted"):
            record["error"] = str(result.get("error") or "清除失败")
            return
        record["items"] = int(result.get("deleted", 0))
        record["freed_bytes"] = int(result.get("freed_bytes", 0))
        record["acted"] = record["items"] > 0
        protected = int(result.get("protected", 0))
        record["detail"] = (
            f"清除 {record['items']} 项垃圾，释放 {record['freed_bytes']} 字节"
            + (f"；{protected} 项在 7 天年龄锁内，没动" if protected else "")
        )
        if not record["acted"]:
            record["skipped"] = (
                f"没有可清除的垃圾"
                + (f"（{protected} 项在年龄锁内）" if protected else "")
            )

    async def _run_temp_files(self, record: dict[str, Any], force: bool) -> None:
        """官方临时目录。删了会由 AstrBot 重建，这是最安全的清理对象。"""
        result = await temp_cleanup()
        if not result.get("exists"):
            record["skipped"] = f"没有临时目录：{result.get('root', '')}"
            return
        record["items"] = int(result.get("deleted_files", 0))
        record["freed_bytes"] = int(result.get("freed_bytes", 0))
        record["acted"] = record["items"] > 0
        recent = int(result.get("too_recent", 0))
        record["detail"] = (
            f"扫了 {result.get('scanned_files', 0)} 个临时文件，"
            f"删除 {record['items']} 个 / 释放 {record['freed_bytes']} 字节"
            + (f"；{recent} 个在 7 天年龄锁内" if recent else "")
        )
        if not record["acted"]:
            record["skipped"] = (
                f"临时目录里没有超过 7 天的文件（{recent} 个在年龄锁内）"
                if recent else "临时目录是空的"
            )

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
        # 每个实例的失败必须冒到 record["error"]。以前错误只进了 results 里的局部
        # 变量，最后一律记成「跳过：所有实例都未到清理时间」——一个挂了三个月的
        # bot，体检历史里全是「跳过」，看上去一切正常。
        failures = [
            f"{item['url']}：{item['error']}" for item in results if item["error"]
        ]
        if failures:
            record["error"] = "；".join(failures)[:200]
        record["detail"] = "；".join(
            f"{item['url']}：{item['detail'] or item['error']}" for item in results
        )
        if not record["acted"]:
            if failures:
                record["skipped"] = f"{len(failures)} 个实例调用失败"
            else:
                record["skipped"] = "所有实例都未到清理时间"

    # ---------- 死实例探测：只报告，不自动删 ----------

    def _unreachable_counters(self) -> dict[str, int]:
        value = self._state.get("unreachable")
        return value if isinstance(value, dict) else {}

    def _note_reachability(self, rows: Any) -> None:
        """用 collect() 已经取到的探测结果推进「连续失联」计数，不额外发请求。

        推进带时间闸：面板每 30 秒刷一次，不设闸的话一个只是暂时没启动的 bot
        会在半小时内被推成「长期失联」。也不能放进 sweep()——那会让每次体检
        先干等一轮网络探测（每个实例最长 10 秒超时），点「强制清理」像是没反应。
        """
        if not isinstance(rows, list):
            return
        now = time.monotonic()
        if (now - self._reach_at) < _REACH_ADVANCE_INTERVAL:
            return
        self._reach_at = now
        counters = self._unreachable_counters()
        for row in rows:
            url = str(row.get("url", ""))
            if not url:
                continue
            if row.get("connected"):
                counters.pop(url, None)
            else:
                counters[url] = int(counters.get(url, 0)) + 1
        known = {endpoint.label for endpoint in self.napcat_client.endpoints}
        # 实例被从配置里删掉后，计数不能一直留着
        self._state["unreachable"] = {k: v for k, v in counters.items() if k in known}

    async def _login_account(self, endpoint: Any) -> str:
        """取实例登录的 QQ 号，取不到就返回空串。"""
        try:
            info = await self.napcat_client._call(endpoint, "get_login_info")
        except Exception:
            return ""
        if not isinstance(info, dict):
            return ""
        user_id = info.get("user_id")
        return str(user_id) if user_id not in (None, "") else ""

    async def _static_identities(self) -> dict[str, dict[str, Any]]:
        """读磁盘上的 onebot11*.json，拿到每个地址对应的 QQ 号与配置文件。

        这一步不发请求。它存在的理由是：连不上的 NapCat 拿不到
        get_login_info，而那恰恰是「确实重复且确实没用」的那一类实例
        最常见的状态。
        """
        try:
            return await asyncio.to_thread(identity_sync, self._token_dirs())
        except Exception as exc:
            logger.warning("Orbit Cache 读取实例身份失败：%s", type(exc).__name__)
            return {"exact": {}, "loose": {}}

    async def dead_instances(self) -> list[dict[str, Any]]:
        """找出疑似冗余的 NapCat 实例。

        证据分两组。静态组只读磁盘上的 onebot11*.json（文件名里就是 QQ 号，
        里面的 network 段就是监听端口），**不发任何网络请求**，所以 NapCat
        挂掉时照样能判出重复。动态组要连上才算，是加强而不是前提。

        高置信（可删）：地址归一后相同 / 指向同一份 onebot11*.json /
        两个实例登录后是同一个 QQ 号。
        低置信（只提示）：连续多次探测连不上——停用与暂时下线无法区分，
        不给删除。

        两种情况都只动配置里那一行，绝不碰 NapCat 磁盘上的任何东西。
        每个发现项带 occurrence（这是第几个同地址的条目），因为删除是按
        「地址 + 第几个」定位的。
        """
        endpoints = self.napcat_client.endpoints
        if len(endpoints) < 2:
            return []
        rows = await self.napcat_client.status()
        counters = self._unreachable_counters()
        identities = await self._static_identities()
        findings: list[dict[str, Any]] = []
        seen: set[int] = set()

        def add(index: int, *, reason: str, basis: str) -> None:
            seen.add(index)
            findings.append({
                "url": endpoints[index].label,
                "index": index,
                "occurrence": occurrence_of[index],
                "reason": reason,
                "confidence": "low" if basis == "timeout" else "high",
                "removable": basis != "timeout",
                "basis": basis,
            })

        # 地址先归一再比：http://1.1.1.1:3000 与 http://1.1.1.1:3000/ 指向
        # 同一个 NapCat，字面不同却从没被当成重复。
        occurrence_of: dict[int, int] = {}
        seen_count: dict[str, int] = {}
        first_by_url: dict[str, int] = {}
        url_duplicate: set[int] = set()
        for index, endpoint in enumerate(endpoints):
            raw = endpoint.url
            occurrence_of[index] = seen_count.get(raw, 0)
            seen_count[raw] = occurrence_of[index] + 1
            normalized = normalize_endpoint(raw)
            if normalized in first_by_url:
                url_duplicate.add(index)
            else:
                first_by_url[normalized] = index
        for index in sorted(url_duplicate):
            add(
                index,
                reason=(
                    f"地址与第 {first_by_url[normalize_endpoint(endpoints[index].url)] + 1}"
                    " 行指向同一个服务"
                ),
                basis="static",
            )

        # 剩下的实例里找「指向同一个 QQ / 同一份配置文件」的。已被地址判据
        # 认领的行不参与，避免同一个 index 被判两次。
        file_first: dict[str, int] = {}
        account_first: dict[str, int] = {}
        for index in range(len(endpoints)):
            if index in url_duplicate:
                continue
            identity = identity_for(endpoints[index].url, identities)
            config_file = str(identity.get("file") or "")
            account = str(identity.get("account") or "")
            if config_file and config_file in file_first:
                add(
                    index,
                    reason=(
                        f"与第 {file_first[config_file] + 1} 行指向同一份 "
                        f"{Path(config_file).name}"
                    ),
                    basis="static",
                )
                continue
            if account and account in account_first:
                add(
                    index,
                    reason=f"与第 {account_first[account] + 1} 行指向同一个 QQ 号 {account}",
                    basis="static",
                )
                continue
            if config_file:
                file_first[config_file] = index
            if account:
                account_first[account] = index

        for index in range(len(endpoints)):
            if index in seen:
                continue
            account = await self._login_account(endpoints[index])
            if not account:
                continue
            previous = account_first.get(account)
            if previous is None:
                account_first[account] = index
                continue
            add(
                index,
                reason=f"与第 {previous + 1} 行登录的是同一个 QQ 号 {account}",
                basis="dynamic",
            )

        limit = self.settings()["unreachable_limit"]
        for row in rows:
            url = str(row.get("url", ""))
            count = int(counters.get(url, 0))
            index = next((i for i, e in enumerate(endpoints) if e.label == url), -1)
            if count < limit or index in seen or index < 0:
                continue
            detail = str(row.get("error", ""))[:80]
            suffix = f"（{detail}）" if detail else ""
            add(index, reason=f"连续 {count} 次体检连不上{suffix}", basis="timeout")
        return findings

    async def dry_run(self, *, force: bool = False) -> dict[str, Any]:
        """试运行：只算不删，回答「到底会不会真的删东西」。

        不对磁盘做任何写操作，也不改状态、不记历史。
        """
        try:
            await self.auto_dirs()
        except Exception as exc:
            logger.warning("Orbit Cache 自动接管缓存目录失败：%s", type(exc).__name__)
        cleaner = self.fs_cleaner()
        previews = await cleaner.preview(force=force)
        media_cleaner, _ = self.media_cleaner()
        media_previews = await media_cleaner.preview(force=force)
        retention = await log_retention_plan(self.settings()["log_retention_days"])
        threshold = self.settings()["napcat_cache_threshold_mb"] * MIB
        total_files = sum(item["would_delete_files"] for item in previews)
        total_bytes = sum(item["would_delete_bytes"] for item in previews)
        astrbot_size = 0
        logs_size = 0
        astrbot_error = ""
        try:
            source = await self.astrbot_cache.status()
            astrbot_size, _ = _astrbot_target(source, "cache")
            logs_size, _ = _astrbot_target(source, "logs")
        except Exception as exc:
            astrbot_error = f"{type(exc).__name__}: {exc}"[:120]
        return {
            "threshold_bytes": threshold,
            "directories": previews,
            "would_delete_files": total_files,
            "would_delete_bytes": total_bytes,
            "media_directories": media_previews,
            "media_would_delete_files": sum(
                item["would_delete_files"] for item in media_previews
            ),
            "media_would_delete_bytes": sum(
                item["would_delete_bytes"] for item in media_previews
            ),
            "astrbot_size_bytes": astrbot_size,
            "astrbot_threshold_bytes": self.settings()["astrbot_cache_threshold_mb"] * MIB,
            "astrbot_logs_size_bytes": logs_size,
            "astrbot_logs_threshold_bytes": self.settings()["astrbot_logs_threshold_mb"] * MIB,
            "log_retention": retention,
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
