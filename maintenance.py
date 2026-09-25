from __future__ import annotations

import asyncio
import keyword
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# 这些文件名一旦被删就不是「清缓存」而是「搞坏对方」：
# onebot11*.json 是 NapCat 的 OneBot 配置（也是本插件自己的 Token 来源），
# napcat.mjs / package.json 是程序本体。即使目录填错也绝不能动。
_PROTECTED_NAMES = frozenset({
    "onebot11.json", "napcat.json", "webui.json", "napcat.mjs",
    "package.json", "package-lock.json", "start.bat", "start.sh",
})


def _is_protected(name: str) -> bool:
    return name in _PROTECTED_NAMES or name.startswith("onebot11_")


class ModuleCacheError(RuntimeError):
    pass


class ModuleCacheManager:
    _PLUGIN_PREFIX = "data.plugins."
    _BUILTIN_PREFIX = "astrbot.builtin_stars."

    def __init__(self, context: Any, current_plugin_id: str) -> None:
        self.context = context
        self.current_plugin_id = str(current_plugin_id)

    @staticmethod
    def _owner(module_name: str) -> tuple[str, str] | None:
        for prefix in (ModuleCacheManager._PLUGIN_PREFIX, ModuleCacheManager._BUILTIN_PREFIX):
            if module_name.startswith(prefix):
                owner = module_name[len(prefix) :].split(".", 1)[0]
                if owner:
                    return prefix, owner
        return None

    def snapshot(self) -> list[dict[str, Any]]:
        by_owner: dict[tuple[str, str], list[str]] = {}
        for module_name in list(sys.modules):
            owner = self._owner(module_name)
            if owner:
                by_owner.setdefault(owner, []).append(module_name)
        return [
            {
                "scope": prefix.rstrip("."),
                "plugin_id": plugin_id,
                "modules": sorted(modules),
            }
            for (prefix, plugin_id), modules in sorted(by_owner.items())
        ]

    @staticmethod
    def _validate_plugin_id(plugin_id: str) -> str:
        value = str(plugin_id or "").strip()
        if not value.isidentifier() or keyword.iskeyword(value):
            raise ModuleCacheError("插件 ID 必须是合法 Python 模块名")
        return value

    def _metadata_for(self, plugin_id: str) -> Any:
        getter = getattr(self.context, "get_all_stars", None)
        if not callable(getter):
            return None
        for metadata in getter() or []:
            names = {
                str(getattr(metadata, "name", "") or ""),
                str(getattr(metadata, "root_dir_name", "") or ""),
            }
            if plugin_id in names:
                return metadata
        return None

    def clean_plugin(self, plugin_id: str) -> list[str]:
        value = self._validate_plugin_id(plugin_id)
        if value == self.current_plugin_id:
            raise ModuleCacheError("不能清理当前正在运行的插件")
        prefixes = (f"{self._PLUGIN_PREFIX}{value}", f"{self._BUILTIN_PREFIX}{value}")
        loaded = [
            name
            for name in list(sys.modules)
            if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)
        ]
        if not loaded:
            raise ModuleCacheError("没有找到该插件已加载的模块")
        metadata = self._metadata_for(value)
        if metadata is not None and bool(getattr(metadata, "activated", False)):
            raise ModuleCacheError("该插件仍处于激活状态，请先停用")
        return self._drop_modules(loaded)

    def is_active(self, plugin_id: str) -> bool:
        value = self._validate_plugin_id(plugin_id)
        metadata = self._metadata_for(value)
        return bool(getattr(metadata, "activated", False))

    def purge_loaded(self, plugin_id: str) -> list[str]:
        """移除一个插件已加载的模块，不校验激活状态。

        停用插件时 AstrBot 的 turn_off_plugin 不会清理 sys.modules，而它是在
        触发 on_plugin_unloaded 钩子之后才把 activated 置为 False，所以钩子里
        无法用激活状态判断该不该清，只能无条件清理已加载的模块。
        """
        value = self._validate_plugin_id(plugin_id)
        if value == self.current_plugin_id:
            raise ModuleCacheError("不能清理当前正在运行的插件")
        prefixes = (f"{self._PLUGIN_PREFIX}{value}", f"{self._BUILTIN_PREFIX}{value}")
        loaded = [
            name
            for name in list(sys.modules)
            if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)
        ]
        return self._drop_modules(loaded)

    def _drop_modules(self, loaded: list[str]) -> list[str]:
        for name in loaded:
            sys.modules.pop(name, None)
        return sorted(loaded)

    def describe(self) -> list[dict[str, Any]]:
        """把模块快照和插件元数据合并，供面板展示。"""
        rows: list[dict[str, Any]] = []
        for item in self.snapshot():
            plugin_id = str(item["plugin_id"])
            metadata = self._metadata_for(plugin_id)
            rows.append(
                {
                    "scope": item["scope"],
                    "plugin_id": plugin_id,
                    "display_name": str(getattr(metadata, "name", "") or plugin_id),
                    "version": str(getattr(metadata, "version", "") or ""),
                    "activated": bool(getattr(metadata, "activated", False)),
                    "module_count": len(item["modules"]),
                    "self": plugin_id == self.current_plugin_id,
                }
            )
        return rows

    def clean_inactive_plugins(self) -> dict[str, list[str] | str]:
        cleaned: dict[str, list[str]] = {}
        skipped: list[str] = []
        for item in self.snapshot():
            plugin_id = str(item["plugin_id"])
            if plugin_id == self.current_plugin_id:
                skipped.append(f"{plugin_id}（当前插件）")
                continue
            metadata = self._metadata_for(plugin_id)
            if metadata is not None and bool(getattr(metadata, "activated", False)):
                skipped.append(f"{plugin_id}（仍激活）")
                continue
            try:
                cleaned[plugin_id] = self.clean_plugin(plugin_id)
            except ModuleCacheError as exc:
                skipped.append(f"{plugin_id}（{exc}）")
        return {"cleaned": cleaned, "skipped": skipped}


@dataclass(frozen=True)
class CacheScan:
    path: str
    exists: bool
    file_count: int
    size_bytes: int
    skipped_symlinks: int
    error: str = ""


class NapCatCacheDirectoryCleaner:
    def __init__(
        self,
        directories: list[str],
        *,
        threshold_mb: int = 100,
        min_age_minutes: int = 10,
        target_ratio: float = 0.85,
    ) -> None:
        self.threshold_bytes = int(threshold_mb) * 1024 * 1024
        self.min_age_seconds = int(min_age_minutes) * 60
        self.target_ratio = float(target_ratio)
        if self.threshold_bytes <= 0:
            raise ValueError("缓存阈值必须大于 0")
        if self.min_age_seconds < 0:
            raise ValueError("缓存文件最小年龄不能小于 0")
        if not 0 < self.target_ratio <= 1:
            raise ValueError("清理目标比例必须大于 0 且不超过 1")
        self.directories = self._validate_directories(directories)

    def _validate_directories(self, directories: list[str]) -> list[Path]:
        home = Path.home().resolve()
        cwd = Path.cwd().resolve()
        data_root = Path(get_astrbot_data_path()).resolve()
        result: list[Path] = []
        seen: set[Path] = set()
        for value in directories:
            text = str(value or "").strip()
            if not text:
                continue
            path = Path(text).expanduser()
            if not path.is_absolute():
                raise ValueError(f"NapCat 缓存目录必须是绝对路径: {text}")
            if path.is_symlink():
                raise ValueError(f"NapCat 缓存目录不能是符号链接: {text}")
            resolved = path.resolve(strict=False)
            if resolved == home or home.is_relative_to(resolved):
                raise ValueError(f"拒绝监控用户目录或其祖先: {resolved}")
            if (
                resolved == cwd
                or resolved.is_relative_to(cwd)
                or cwd.is_relative_to(resolved)
            ):
                raise ValueError(f"NapCat 缓存目录不能与当前工作区重叠: {resolved}")
            if (
                resolved == data_root
                or resolved.is_relative_to(data_root)
                or data_root.is_relative_to(resolved)
            ):
                raise ValueError(f"NapCat 缓存目录不能与 AstrBot 数据目录重叠: {resolved}")
            if resolved not in seen:
                seen.add(resolved)
                result.append(resolved)
        return result

    @staticmethod
    def _walk(root: Path) -> tuple[list[tuple[Path, int, float]], int]:
        files: list[tuple[Path, int, float]] = []
        skipped = 0
        if not root.exists():
            return files, skipped
        for path in root.rglob("*"):
            try:
                if path.is_symlink():
                    skipped += 1
                    continue
                if not path.is_file():
                    continue
                if _is_protected(path.name):
                    skipped += 1
                    continue
                stat = path.stat()
                files.append((path, stat.st_size, stat.st_mtime))
            except OSError:
                skipped += 1
        files.sort(key=lambda item: (item[2], str(item[0])))
        return files, skipped

    def scan_sync(self) -> list[CacheScan]:
        scans: list[CacheScan] = []
        for root in self.directories:
            try:
                files, skipped = self._walk(root)
            except OSError as exc:
                scans.append(CacheScan(str(root), root.exists(), 0, 0, 0, str(exc)))
                continue
            scans.append(
                CacheScan(
                    path=str(root),
                    exists=root.exists(),
                    file_count=len(files),
                    size_bytes=sum(item[1] for item in files),
                    skipped_symlinks=skipped,
                )
            )
        return scans

    def preview_sync(self, *, force: bool = False) -> list[dict[str, Any]]:
        """只算不删：返回超阈值的目录、将被删除的文件数与字节数。

        用来回答「这插件到底会不会真的删东西」——不碰磁盘任何写操作。
        """
        previews: list[dict[str, Any]] = []
        now = time.time()
        for root in self.directories:
            files, _ = self._walk(root)
            total = sum(item[1] for item in files)
            if not force and total <= self.threshold_bytes:
                previews.append({
                    "path": str(root), "size_bytes": total,
                    "would_delete_files": 0, "would_delete_bytes": 0,
                    "samples": [], "over_threshold": False,
                })
                continue
            # force 时目标是 0；否则阈值 × 0.85
            target = 0 if force else int(self.threshold_bytes * self.target_ratio)
            remaining = total
            doomed: list[tuple[Path, int]] = []
            for path, size, mtime in files:
                if remaining <= target:
                    break
                if now - mtime < self.min_age_seconds:
                    continue
                doomed.append((path, size))
                remaining -= size
            previews.append({
                "path": str(root),
                "size_bytes": total,
                "would_delete_files": len(doomed),
                "would_delete_bytes": sum(size for _, size in doomed),
                "samples": [str(path) for path, _ in doomed[:8]],
                "over_threshold": total > self.threshold_bytes,
            })
        return previews

    async def preview(self, *, force: bool = False) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.preview_sync, force=force)

    def clean_sync(self, *, force: bool = False) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        now = time.time()
        for root in self.directories:
            files, skipped = self._walk(root)
            total = sum(item[1] for item in files)
            deleted = 0
            deleted_bytes = 0
            failed = 0
            if force or total > self.threshold_bytes:
                # force 时目标是 0：否则「强制全清」在占用低于阈值时反而一个都不删
                target_bytes = 0 if force else int(self.threshold_bytes * self.target_ratio)
                for path, size, mtime in files:
                    if total <= target_bytes:
                        break
                    if now - mtime < self.min_age_seconds:
                        continue
                    try:
                        path.unlink()
                    except OSError:
                        failed += 1
                        continue
                    deleted += 1
                    deleted_bytes += size
                    total -= size
                for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
                    directory = Path(dirpath)
                    if directory == root or directory.is_symlink():
                        continue
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
            results.append(
                {
                    "path": str(root),
                    "deleted_files": deleted,
                    "deleted_bytes": deleted_bytes,
                    "remaining_bytes": total,
                    "skipped_symlinks": skipped,
                    "failed_files": failed,
                }
            )
        return results

    async def status(self) -> list[CacheScan]:
        return await asyncio.to_thread(self.scan_sync)

    async def clean_once(self, *, force: bool = False) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.clean_sync, force=force)


class AstrBotCacheCleaner:
    def _cleaner(self) -> Any:
        from astrbot.core.utils.storage_cleaner import StorageCleaner

        return StorageCleaner({})

    async def status(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._cleaner().get_status)

    async def size_bytes(self) -> int:
        status = await self.status()
        cache = status.get("cache", {}) if isinstance(status, dict) else {}
        try:
            return int(cache.get("size_bytes", 0) or 0)
        except (TypeError, ValueError):
            return 0

    async def clean(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._cleaner().cleanup, "cache")
