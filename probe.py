"""Orbit Cache · 只读探针（P0）。

回答三件事：
  1. 这台部署支持哪些能力（Windows / Android / Linux 差异）
  2. 磁盘被谁吃了（按目录 / 按类型 / 按插件 / 单文件 / 表）
  3. 在涨吗、多快（采样历史 -> 线性拟合 -> 几天后到阈值）

约束：
  * 全程只读，不写 data 目录里的任何文件
  * 历史采样只写插件自己的数据目录（由调用方传入）
  * 不依赖 PowerShell / Windows API，Android 上能跑
"""

from __future__ import annotations

import heapq
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 类型归因

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".svg"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".ogg", ".silk", ".amr", ".m4a", ".aac"}
ARCHIVE_EXT = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".whl"}
CODE_EXT = {
    ".py", ".pyc", ".js", ".ts", ".json", ".yaml", ".yml", ".toml",
    ".md", ".txt", ".html", ".css",
}

_MAX_FILES = 400_000
_HISTORY_LIMIT = 200


def classify(name: str) -> str:
    low = name.lower()
    if low.endswith((".db-wal", ".db-shm")):
        return "数据库"
    ext = os.path.splitext(low)[1]
    if ext in (".db", ".sqlite", ".sqlite3"):
        return "数据库"
    if ext in IMAGE_EXT:
        return "图片"
    if ext in VIDEO_EXT:
        return "视频"
    if ext in AUDIO_EXT:
        return "音频"
    if ext in ARCHIVE_EXT:
        return "压缩包"
    if ext in CODE_EXT:
        return "代码/文本"
    return "其他"


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


# ---------------------------------------------------------------- 数据根

def _data_root(explicit: str | Path | None = None) -> Path:
    """跨部署定位 AstrBot 数据目录。"""
    if explicit:
        return Path(explicit)
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return Path(get_astrbot_data_path())
    except Exception:
        pass
    env = os.environ.get("ASTRBOT_DATA_DIR")
    if env:
        return Path(env)
    return Path.home() / ".astrbot" / "data"


# ---------------------------------------------------------------- 能力探测

def capabilities(data_dir: Path) -> dict[str, dict[str, Any]]:
    caps: dict[str, dict[str, Any]] = {}

    def put(name: str, ok: bool, reason: str = "") -> None:
        caps[name] = {"supported": ok, "reason": reason}

    try:
        ok = data_dir.is_dir() and os.access(data_dir, os.R_OK)
        put("fs_read", ok, "" if ok else f"读不到 {data_dir}")
    except OSError as exc:
        put("fs_read", False, str(exc))

    try:
        import psutil

        rss = psutil.Process(os.getpid()).memory_info().rss
        put("proc_self", rss >= 0, "")
    except Exception as exc:
        put("proc_self", False, f"psutil 不可用或受限: {type(exc).__name__}")

    try:
        import psutil

        seen = 0
        for _ in psutil.process_iter(["pid"]):
            seen += 1
            if seen > 5:
                break
        put("proc_other", True, "")
    except Exception as exc:
        put("proc_other", False, f"无法枚举其他进程: {type(exc).__name__}")

    try:
        import psutil

        put("ports", True, f"可读 {len(psutil.net_connections(kind='inet'))} 条连接")
    except Exception as exc:
        put("ports", False, f"端口表不可读: {type(exc).__name__}")

    put("sqlite", True, "")
    qq_ok = any(
        p.is_dir()
        for p in (Path.home() / "Documents" / "Tencent Files", Path.home() / ".config" / "QQ")
    )
    put("qq_dirs", qq_ok, "" if qq_ok else "本机没有 QQ 客户端目录（Android/Linux 常见）")
    return caps


# ---------------------------------------------------------------- 目录扫描

def scan_tree(root: Path) -> dict[str, Any]:
    total = 0
    files = 0
    errors = 0
    by_dir: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_plugin: dict[str, int] = {}
    largest: list[tuple[int, str]] = []
    oldest: list[tuple[float, int, str]] = []
    db_files: list[Path] = []
    plugin_root = root / "plugin_data"
    seen_dirs = 0

    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        seen_dirs += 1
        if seen_dirs > _MAX_FILES:
            break
        cur = Path(dirpath)
        try:
            rel = cur.relative_to(root)
        except ValueError:
            rel = Path(".")
        top = rel.parts[0] if rel.parts else "."
        try:
            rel_plugin = cur.relative_to(plugin_root)
            under_plugin = rel_plugin.parts[0] if rel_plugin.parts else None
        except ValueError:
            under_plugin = None

        for name in filenames:
            fp = os.path.join(dirpath, name)
            try:
                st = os.stat(fp)
            except OSError:
                errors += 1
                continue
            size = st.st_size
            total += size
            files += 1
            by_dir[top] = by_dir.get(top, 0) + size
            kind = classify(name)
            by_type[kind] = by_type.get(kind, 0) + size
            if under_plugin:
                by_plugin[under_plugin] = by_plugin.get(under_plugin, 0) + size
            if size > 1024 * 1024:
                heapq.heappush(largest, (size, fp))
                if len(largest) > 10:
                    heapq.heappop(largest)
            heapq.heappush(oldest, (st.st_mtime, size, fp))
            if len(oldest) > 5:
                heapq.heappop(oldest)
            if name.lower().endswith((".db", ".sqlite", ".sqlite3")):
                db_files.append(Path(fp))

    return {
        "root": str(root),
        "total_bytes": total,
        "file_count": files,
        "errors": errors,
        "by_dir": dict(sorted(by_dir.items(), key=lambda kv: kv[1], reverse=True)),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: kv[1], reverse=True)),
        "by_plugin": dict(sorted(by_plugin.items(), key=lambda kv: kv[1], reverse=True)),
        "largest": [{"bytes": s, "path": p} for s, p in sorted(largest, reverse=True)],
        "oldest": [
            {"days": round((time.time() - m) / 86400, 1), "bytes": s, "path": p}
            for m, s, p in sorted(oldest)
        ],
        "db_files": db_files,
    }


# ---------------------------------------------------------------- 数据库只读

def db_stats(db_files: list[Path]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for db in db_files[:5]:
        info: dict[str, Any] = {
            "path": str(db),
            "bytes": db.stat().st_size if db.exists() else 0,
            "wal_bytes": _size_of(str(db) + "-wal"),
            "shm_bytes": _size_of(str(db) + "-shm"),
        }
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3)
            try:
                page_count = conn.execute("PRAGMA page_count").fetchone()[0]
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                info["logical_bytes"] = page_count * page_size
                info["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
                tables = [
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    )
                ]
                rows: list[dict[str, Any]] = []
                for name in tables[:20]:
                    try:
                        n = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                    except Exception:
                        n = None
                    rows.append({"table": name, "rows": n})
                rows.sort(key=lambda r: (r["rows"] or 0), reverse=True)
                info["tables"] = rows
                info["table_count"] = len(tables)
            finally:
                conn.close()
        except Exception as exc:
            info["error"] = f"{type(exc).__name__}: {exc}"
        out.append(info)
    return out


def _size_of(path: str) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


# ---------------------------------------------------------------- 增长率

def _load_history(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    samples = data.get("samples") if isinstance(data, dict) else None
    return [s for s in (samples or []) if isinstance(s, dict) and "ts" in s and "bytes" in s]


def _save_history(path: Path, samples: list[dict[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"samples": samples[-_HISTORY_LIMIT:]}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def growth(samples: list[dict[str, Any]], total: int, threshold_mb: int) -> dict[str, Any]:
    samples = samples + [{"ts": time.time(), "bytes": total}]
    if len(samples) < 2:
        return {"samples": len(samples), "mb_per_day": None, "days_to_threshold": None}
    first, last = samples[0], samples[-1]
    span_h = (last["ts"] - first["ts"]) / 3600.0
    if span_h < 0.5:
        return {
            "samples": len(samples),
            "mb_per_day": None,
            "days_to_threshold": None,
            "note": "样本间隔不足 30 分钟，暂不估算",
        }
    per_day = (last["bytes"] - first["bytes"]) / span_h * 24 / (1024 * 1024)
    result: dict[str, Any] = {
        "samples": len(samples),
        "span_hours": round(span_h, 2),
        "mb_per_day": round(per_day, 2),
        "days_to_threshold": None,
    }
    if per_day > 0:
        remain = threshold_mb - total / (1024 * 1024)
        result["days_to_threshold"] = round(remain / per_day, 1) if remain > 0 else 0.0
    return result


# ---------------------------------------------------------------- 对外入口

def build_probe_report(
    data_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
    threshold_mb: int = 1024,
) -> dict[str, Any]:
    """只读体检报告。state_dir 只用来存采样历史。"""
    root = _data_root(data_dir)
    storage = scan_tree(root)
    report: dict[str, Any] = {
        "root": storage["root"],
        "total_bytes": storage["total_bytes"],
        "total_human": human_bytes(storage["total_bytes"]),
        "file_count": storage["file_count"],
        "errors": storage["errors"],
        "capabilities": capabilities(root),
        "by_dir": {k: {"bytes": v, "human": human_bytes(v)} for k, v in storage["by_dir"].items()},
        "by_type": {k: {"bytes": v, "human": human_bytes(v)} for k, v in storage["by_type"].items()},
        "by_plugin": {
            k: {"bytes": v, "human": human_bytes(v)} for k, v in storage["by_plugin"].items()
        },
        "largest": [
            {**row, "human": human_bytes(row["bytes"])} for row in storage["largest"]
        ],
        "oldest": [
            {**row, "human": human_bytes(row["bytes"])} for row in storage["oldest"]
        ],
        "databases": db_stats(storage["db_files"]),
        "threshold_mb": threshold_mb,
        "readonly": True,
    }

    history_path = Path(state_dir) / "probe_history.json" if state_dir else None
    samples = _load_history(history_path) if history_path else []
    report["growth"] = growth(samples, storage["total_bytes"], threshold_mb)
    if history_path:
        _save_history(history_path, samples + [{"ts": time.time(), "bytes": storage["total_bytes"]}])
    return report
