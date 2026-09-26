"""空间盘点与客观垃圾识别。

这一模块只做「量出来是什么」和「删掉不需要判断的东西」两件事：

* 空间地图：纯只读。把 AstrBot 数据目录、QQ 客户端数据目录、用户已配置的
  缓存/媒体目录按体积排行出来，用来回答「空间到底被谁吃了」。
  这个插件的各层都只认自己那几个位置，看不到大头在哪。
* 插件垃圾：只认三个「不满足条件就是垃圾」的目标——没装完的插件目录、
  遗留安装包、__pycache__。**不推断**「装了但没加载」这类状态：
  停用、没加载、残留三者无法可靠区分，猜错就是删掉别人在跑的插件。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# 扫描必须有硬上限：用户目录树上动辄几十万条，不设限会把面板卡死
_MAX_ENTRIES = 60000
_MAX_DEPTH = 5

# 当前正在写的日志文件：即使超过保留期也绝不删。
# 删掉被进程打开的文件不会立刻释放空间（inode 还被占着），
# 而之后的新日志会写进那个已删除的 inode，表现为「日志不见了」。
_CURRENT_LOG_NAMES = frozenset({"astrbot.log", "astrbot.trace.log"})

# 插件目录的入口文件。data/plugins/<dir>/ 下没有它的，AstrBot 根本不会加载
_PLUGIN_MARKER = "metadata.yaml"


class _Budget:
    def __init__(self, limit: int = _MAX_ENTRIES) -> None:
        self.remaining = limit
        self.exhausted = False

    def take(self, count: int = 1) -> bool:
        if self.remaining <= 0:
            self.exhausted = True
            return False
        self.remaining -= count
        return True


def human_bytes(value: Any) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "0 B"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TiB"


def _measure(root: Path, budget: _Budget) -> dict[str, Any]:
    """量一个目录。遇到符号链接一律跳过，绝不跟随到目录外面去。"""
    result = {
        "path": str(root),
        "exists": root.is_dir(),
        "size_bytes": 0,
        "file_count": 0,
        "skipped_symlinks": 0,
        "truncated": False,
        "error": "",
    }
    if not root.is_dir():
        return result
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > _MAX_DEPTH:
            continue
        if not budget.take():
            result["truncated"] = True
            break
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            result["error"] = type(exc).__name__
            continue
        for entry in entries:
            if not budget.take():
                result["truncated"] = True
                break
            try:
                if entry.is_symlink():
                    result["skipped_symlinks"] += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), depth + 1))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                result["file_count"] += 1
                result["size_bytes"] += entry.stat(follow_symlinks=False).st_size
            except OSError:
                result["skipped_symlinks"] += 1
    return result


# ---------- 空间地图 ----------


def _data_root() -> Path:
    return Path(get_astrbot_data_path())


def _rank(rows: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    rows = [r for r in rows if r.get("exists")]
    rows.sort(key=lambda r: r["size_bytes"], reverse=True)
    return rows[:limit]


def _astrbot_group(budget: _Budget) -> dict[str, Any]:
    root = _data_root()
    rows: list[dict[str, Any]] = []
    try:
        children = sorted(
            (c for c in root.iterdir() if c.is_dir() and not c.is_symlink()),
            key=lambda p: p.name,
        )
    except OSError as exc:
        return {
            "title": "AstrBot 数据目录",
            "hint": f"读取失败：{type(exc).__name__}",
            "rows": [],
        }
    for child in children:
        row = _measure(child, budget)
        row["label"] = child.name
        rows.append(row)
    return {
        "title": "AstrBot 数据目录",
        "hint": str(root),
        "rows": _rank(rows),
    }


def _qq_group(budget: _Budget, cache_dirs: list[str]) -> dict[str, Any]:
    """找出 QQ 客户端的数据目录。

    这些目录才是图片视频的真正去处，而 NapCat 自己的 temp 只是个中转，
    处理完就清空——所以「NapCat 只占几 KB」和「QQ 收了几个 G 媒体」
    可以同时成立。
    """
    found: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            return
        if resolved in seen or not resolved.is_dir():
            return
        seen.add(resolved)
        found.append(resolved)

    # 用户已经配过的目录附近最可能有，顺着这些找最省事
    hints = [Path(p) for p in cache_dirs]
    for hint in hints:
        for parent in (hint, *hint.parents):
            if parent.name == "nt_data":
                add(parent)
                break
            # .config/QQ/<uin>/nt_data 这一层
            if parent.parent.name == "nt_data":
                add(parent.parent)
                break
    if not found:
        from .discovery import search_roots

        budget2 = _Budget(20000)
        for root in search_roots([]):
            if not root.is_dir() or not budget2.take():
                continue
            stack: list[tuple[Path, int]] = [(root, 0)]
            while stack:
                current, depth = stack.pop()
                if depth > 5:
                    continue
                try:
                    entries = list(os.scandir(current))
                except OSError:
                    continue
                for entry in entries:
                    try:
                        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                            continue
                        name = entry.name
                        if name == "nt_data":
                            add(Path(entry.path))
                            continue
                        if depth < 4 and name not in {".git", "__pycache__", "node_modules"}:
                            stack.append((Path(entry.path), depth + 1))
                    except OSError:
                        continue

    rows: list[dict[str, Any]] = []
    for data_dir in found:
        try:
            children = sorted(
                (c for c in data_dir.iterdir() if c.is_dir() and not c.is_symlink()),
                key=lambda p: p.name,
            )
        except OSError:
            children = []
        for child in children:
            row = _measure(child, budget)
            row["label"] = f"{data_dir.name}/{child.name}"
            rows.append(row)
    return {
        "title": "QQ 客户端数据目录",
        "hint": "图片视频的真正去处。挑大的填进「自选媒体目录」即可清理。",
        "rows": _rank(rows),
    }


def _configured_group(
    title: str, hint: str, dirs: list[str], budget: _Budget
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for value in dirs:
        text = str(value or "").strip()
        if not text:
            continue
        path = Path(text).expanduser()
        row = _measure(path, budget)
        row["label"] = path.name or text
        rows.append(row)
    return {"title": title, "hint": hint, "rows": _rank(rows)}


def usage_map_sync(
    *, media_dirs: list[str], cache_dirs: list[str]
) -> dict[str, Any]:
    started = time.monotonic()
    budget = _Budget()
    groups = [
        _astrbot_group(budget),
        _qq_group(budget, cache_dirs),
        _configured_group(
            "你配置的缓存目录", "插件会自动清理这些", cache_dirs, budget
        ),
        _configured_group(
            "你配置的自选媒体目录", "只删超龄的图片/视频/音频", media_dirs, budget
        ),
    ]
    total = 0
    for group in groups:
        for row in group["rows"]:
            total += int(row["size_bytes"])
    return {
        "groups": [g for g in groups if g["rows"]],
        "total_bytes": total,
        "truncated": budget.exhausted,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


async def usage_map(
    *, media_dirs: list[str], cache_dirs: list[str]
) -> dict[str, Any]:
    return await asyncio.to_thread(
        usage_map_sync, media_dirs=media_dirs, cache_dirs=cache_dirs
    )


# ---------- 客观垃圾 ----------


def _plugins_root() -> Path:
    return _data_root() / "plugins"


def _is_broken_install(path: Path) -> bool:
    """没有 metadata.yaml 的目录：装到一半失败，AstrBot 不会加载它。"""
    return path.is_dir() and not (path / _PLUGIN_MARKER).is_file()


def _classify_junk(target: Path) -> str:
    """只认三种不满足条件就是垃圾的目标。"""
    name = target.name
    if name == "__pycache__":
        return "pycache"
    if target.suffix.lower() == ".zip" and target.is_file():
        return "zip"
    if _is_broken_install(target):
        return "broken"
    return ""


def plugin_junk_sync() -> dict[str, Any]:
    root = _plugins_root()
    broken: list[dict[str, Any]] = []
    zips: list[dict[str, Any]] = []
    pycache: list[dict[str, Any]] = []
    total_bytes = 0
    if not root.is_dir():
        return {
            "root": str(root),
            "root_exists": False,
            "broken": [],
            "zips": [],
            "pycache": [],
            "total_bytes": 0,
        }
    budget = _Budget()
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        path = Path(current)
        if path.is_symlink():
            dirnames[:] = []
            continue
        if path.name == "__pycache__":
            row = _measure(path, budget)
            row["kind"] = "pycache"
            pycache.append(row)
            total_bytes += int(row["size_bytes"])
            dirnames[:] = []
            continue
        if path.parent == root and _is_broken_install(path):
            # plugins 目录的直接子目录却没有 metadata.yaml = 装到一半失败
            row = _measure(path, budget)
            row["kind"] = "broken"
            broken.append(row)
            total_bytes += int(row["size_bytes"])
            dirnames[:] = []  # 整棵已经计过了，别再重复统计
            continue
        for filename in filenames:
            item = path / filename
            if item.suffix.lower() != ".zip" or item.is_symlink():
                continue
            try:
                size = item.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            zips.append({
                "path": str(item),
                "size_bytes": size,
                "file_count": 1,
                "skipped_symlinks": 0,
                "truncated": False,
                "error": "",
                "kind": "zip",
            })
            total_bytes += size
    return {
        "root": str(root),
        "root_exists": True,
        "broken": broken,
        "zips": zips,
        "pycache": pycache,
        "total_bytes": total_bytes,
    }


async def plugin_junk() -> dict[str, Any]:
    return await asyncio.to_thread(plugin_junk_sync)


def remove_junk_sync(kind: str, target: str) -> dict[str, Any]:
    """删一个垃圾项。路径必须仍在 plugins 目录内，且必须确实满足该类条件。

    校验不通过一律拒绝：宁可删不掉，也不能凭一个前端传来的路径删到别处。
    """
    root = _plugins_root()
    try:
        resolved_root = root.resolve(strict=False)
        path = Path(str(target or "")).expanduser().resolve(strict=False)
    except OSError as exc:
        return {"ok": False, "error": f"路径无法解析：{type(exc).__name__}"}
    if path == resolved_root or resolved_root not in path.parents:
        return {"ok": False, "error": "只能删除 data/plugins 目录内的东西"}
    if path.is_symlink() or not path.exists():
        return {"ok": False, "error": "目标不存在或已是符号链接"}
    if _classify_junk(path) != kind:
        return {"ok": False, "error": "该项已不符合清理条件，请刷新后重试"}

    if path.is_dir():
        freed = 0
        failed = 0
        for current, _dirnames, filenames in os.walk(path, topdown=False):
            here = Path(current)
            for filename in filenames:
                item = here / filename
                try:
                    size = item.stat(follow_symlinks=False).st_size
                    item.unlink()
                    freed += size
                except OSError:
                    failed += 1
        # 文件删完后再自底向上清空目录（os.walk 会把 path 自己也吐出来，
        # 所以最后那次 rmdir 留给下面单独做，否则会 FileNotFoundError）
        for current, _dirnames, _filenames in os.walk(path, topdown=False):
            if Path(current) == path:
                continue
            try:
                Path(current).rmdir()
            except OSError:
                pass
        if not path.is_dir():
            return {"ok": True, "removed": str(path), "freed_bytes": freed, "failed": failed}
        try:
            path.rmdir()
        except OSError as exc:
            return {"ok": False, "error": f"删除目录失败：{type(exc).__name__}"}
        return {"ok": True, "removed": str(path), "freed_bytes": freed, "failed": failed}
    try:
        size = path.stat(follow_symlinks=False).st_size
        path.unlink()
    except OSError as exc:
        return {"ok": False, "error": f"删除失败：{type(exc).__name__}"}
    return {"ok": True, "removed": str(path), "freed_bytes": size, "failed": 0}


async def remove_junk(kind: str, target: str) -> dict[str, Any]:
    return await asyncio.to_thread(remove_junk_sync, kind, target)


# ---------- 日志保留 ----------


def _logs_root() -> Path:
    return _data_root() / "logs"


def _is_current_log(path: Path) -> bool:
    return path.name in _CURRENT_LOG_NAMES


def log_retention_plan_sync(retention_days: int) -> dict[str, Any]:
    """算出「按保留天数」会删哪些日志。只算不删。"""
    root = _logs_root()
    if retention_days <= 0:
        return {"mode": "threshold", "root": str(root), "candidates": [],
                "total_bytes": 0, "would_free": 0, "kept_current": 0}
    if not root.is_dir():
        return {"mode": "retention", "root": str(root), "candidates": [],
                "total_bytes": 0, "would_free": 0, "kept_current": 0, "missing": True}
    cutoff = time.time() - retention_days * 86400
    candidates: list[dict[str, Any]] = []
    total = 0
    would_free = 0
    kept_current = 0
    budget = _Budget()
    for path in sorted(root.rglob("*")):
        if budget.take() is False:
            break
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        total += stat.st_size
        if _is_current_log(path):
            # 当前日志永不删：它正被进程打开着
            kept_current += 1
            continue
        if stat.st_mtime >= cutoff:
            continue
        candidates.append({
            "path": str(path),
            "size_bytes": stat.st_size,
            "mtime": stat.st_mtime,
        })
        would_free += stat.st_size
    return {
        "mode": "retention",
        "root": str(root),
        "retention_days": retention_days,
        "candidates": candidates,
        "total_bytes": total,
        "would_free": would_free,
        "kept_current": kept_current,
    }


async def log_retention_plan(retention_days: int) -> dict[str, Any]:
    return await asyncio.to_thread(log_retention_plan_sync, retention_days)


def log_retention_clean_sync(retention_days: int) -> dict[str, Any]:
    plan = log_retention_plan_sync(retention_days)
    deleted = 0
    freed = 0
    failed = 0
    for item in plan["candidates"]:
        path = Path(item["path"])
        try:
            if path.is_symlink() or not path.is_file() or _is_current_log(path):
                continue
            path.unlink()
            deleted += 1
            freed += int(item["size_bytes"])
        except OSError:
            failed += 1
    return {
        "mode": plan["mode"],
        "deleted_files": deleted,
        "freed_bytes": freed,
        "failed_files": failed,
        "kept_current": plan.get("kept_current", 0),
    }


async def log_retention_clean(retention_days: int) -> dict[str, Any]:
    return await asyncio.to_thread(log_retention_clean_sync, retention_days)
