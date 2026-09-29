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
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .discovery import scan_sync

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
    *, media_dirs: list[str], cache_dirs: list[str],
    loaded: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    started = time.monotonic()
    budget = _Budget()
    groups = [
        _astrbot_group(budget),
        _plugin_data_group(budget, loaded),
        _official_group(budget),
        _database_group(),
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
        "paths": official_paths(),
    }


async def usage_map(
    *, media_dirs: list[str], cache_dirs: list[str],
    loaded: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    return await asyncio.to_thread(
        usage_map_sync, media_dirs=media_dirs, cache_dirs=cache_dirs, loaded=loaded
    )


# ---------- 客观垃圾 ----------


def _plugins_root() -> Path:
    return _data_root() / "plugins"


def _plugin_data_root() -> Path:
    """官方明文规定的插件大文件存放地，见 AstrBot 插件存储文档。"""
    return _data_root() / "plugin_data"


def _temp_root() -> Path:
    """AstrBot 自己的临时目录。

    它在 main.py 启动时就建好，下载的图片语音等临时文件都落在这里。
    官方定义里它就是临时的，删了会重新生成——这是所有清理对象里安全性最高的一个。
    """
    return Path(official_paths().get("temp") or (_data_root() / "temp"))


def _config_files() -> list[Path]:
    """AstrBot 的配置文件们。

    cmd_config.json 是默认配置；v4 之后在 WebUI 新建的会以 abconf_ 前缀
    存在 data/config/ 下。不扫后者就会漏掉用户真正在用的那份。
    """
    root = _data_root()
    found: list[Path] = []
    default = root / "cmd_config.json"
    if default.is_file():
        found.append(default)
    config_dir = root / "config"
    if config_dir.is_dir():
        try:
            found.extend(sorted(p for p in config_dir.glob("abconf_*.json") if p.is_file()))
        except OSError:
            pass
    return found


# 接入方式不同，字段名不一样；不同版本也会变。所以一律列候选去猜，
# 拿不准就报「未识别」，不猜错。比写死一个字段名可靠。
_PORT_KEYS = ("ws_reverse_port", "port", "ws_port", "http_port", "api_port")
_TOKEN_KEYS = ("ws_reverse_token", "token", "access_token", "secret")
_NAME_KEYS = ("id", "name", "platform_id", "platform")
_ADAPTER_KEYS = ("type", "platform_type", "adapter", "mode")


def _first_present(entry: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in entry and entry[key] not in (None, ""):
            return entry[key]
    return None


def _token_fingerprint(value: Any) -> str:
    """只留指纹，绝不把凭据送到面板上或日志里。

    同一条 NapCat 的 token 在两份配置里是一样的——这正是判断「完全重复」
    的依据，而这个指纹足够判等，又不会泄露任何东西。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def platform_audit_sync(config_dirs: list[str] | None = None) -> dict[str, Any]:
    """体检 AstrBot 自己的平台配置，找出重复与冲突。

    **之前完全没读过 cmd_config.json**，所以 AstrBot 侧配了几条反向 WS、
    哪两条撞了同一个端口，Orbit 一概不知道——用户看到的就是「它根本找不出
    哪些是重复配置」。

    这里只读，只报，不改：平台配置是核心配置，删错一个可能直接让某个
    机器人掉线。真要删得走面板上的单独入口，并且先备份。
    """
    result: dict[str, Any] = {
        "files": [], "conflicts": [], "total": 0, "error": "",
    }
    entries: list[dict[str, Any]] = []
    for path in _config_files():
        try:
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            result["files"].append({
                "path": str(path), "name": path.stem, "entries": [],
                "error": type(exc).__name__,
            })
            continue
        if not isinstance(doc, dict):
            continue
        raw = doc.get("platform")
        if not isinstance(raw, list):
            continue
        shown: list[dict[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            name = _first_present(item, _NAME_KEYS)
            port = _first_present(item, _PORT_KEYS)
            fingerprint = _token_fingerprint(_first_present(item, _TOKEN_KEYS))
            try:
                port = int(port) if port not in (None, "") else 0
            except (TypeError, ValueError):
                port = 0
            entry = {
                "index": index,
                "name": str(name or f"第 {index + 1} 条"),
                "enable": item.get("enable", True) is not False,
                "port": port,
                "token_fp": fingerprint,
                "adapter": str(_first_present(item, _ADAPTER_KEYS) or ""),
                "file": str(path),
                "file_name": path.stem,
            }
            shown.append(entry)
            entries.append(entry)
        result["files"].append({
            "path": str(path), "name": path.stem, "entries": shown, "error": "",
        })
    result["total"] = len(entries)

    # 端口抢占：多个配置监听同一个端口，谁后起谁绑不上，前面的静默失败。
    by_port: dict[int, list[dict[str, Any]]] = {}
    for entry in entries:
        if entry["port"]:
            by_port.setdefault(entry["port"], []).append(entry)
    for port, group in sorted(by_port.items()):
        if len(group) < 2:
            continue
        names = [f"{g['file_name']} / {g['name']}" for g in group]
        identical = len({g["token_fp"] for g in group if g["token_fp"]}) == 1
        result["conflicts"].append({
            "kind": "duplicate" if identical else "port",
            "port": port,
            "names": names,
            "detail": (
                f"第 {port} 端口上有 {len(group)} 条配置，"
                + ("凭据也一模一样，是同一条被改了个名字。" if identical
                   else "凭据不同，是真的抢同一个端口。")
                + "同一个端口只能绑一次，后启动的那个会静默失败。"
            ),
        })

    # 反向 WS 的端口要能在 NapCat 配置里找到对应，否则那条配置根本没人连。
    # **只在用户显式配了目录时去找**：不指定就跑全盘搜索，面板会被拖到超时。
    known_ports: set[int] = set()
    for item in config_dirs or []:
        try:
            for row in scan_sync([item]).get("configs", []):
                for server in row.get("servers", []):
                    if server.get("can_connect") and server.get("port"):
                        known_ports.add(int(server["port"]))
        except Exception:
            continue
    for entry in entries:
        port = entry.get("port") or 0
        if known_ports and port and port not in known_ports and entry.get("enable"):
            entry["orphan_port"] = True
    return result


def reverse_ws_only(explicit: list[str] | None = None) -> dict[str, Any]:
    """磁盘上的 NapCat 是不是全都只配了反向连接。

    这种情况很常见：机器人正常跑着，但 OneBot 那边不往外监听任何端口，
    于是「协议缓存」这层永远连不上，一直报失败——而它**本来就不适用**。
    与其每 15 分钟报一次红字，不如说清楚「你这套部署用不上这一层」。
    """
    result = {"configs": 0, "has_server": False, "has_client": False}
    try:
        entries = scan_sync(list(explicit or [])).get("configs", [])
    except Exception:
        return result
    for entry in entries:
        result["configs"] += 1
        for server in entry.get("servers", []):
            if server.get("can_connect"):
                result["has_server"] = True
            else:
                result["has_client"] = True
    return result


async def reverse_ws_only_async(explicit: list[str] | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(reverse_ws_only, explicit)


async def platform_audit(config_dirs: list[str] | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(platform_audit_sync, config_dirs)


def _database_files() -> list[Path]:
    """在数据目录里找数据库文件。

    不写死文件名：AstrBot 的库名跟着版本变过，配置里也可能换路径。
    这里用搜索代替硬编码——这正是跨部署最容易失效的地方。
    只读文件名与大小，**绝不打开**。
    """
    root = _data_root()
    found: list[Path] = []
    try:
        for path in root.rglob("*.db*"):
            try:
                if path.is_file() and not path.is_symlink():
                    found.append(path)
            except OSError:
                continue
    except OSError:
        return []
    return sorted(found, key=lambda p: p.name)


# 临时目录的年龄锁。删得太激进会误伤正在传的图片；
# 保守一点没关系，这个目录本来就是靠 AstrBot 自己重建的。
_TEMP_MIN_AGE_DAYS = 7


def temp_cleanup_sync(
    min_age_days: int = _TEMP_MIN_AGE_DAYS, *, dry_run: bool = False
) -> dict[str, Any]:
    """清官方临时目录里超过 N 天的文件。

    这是所有清理对象里安全性最高的一个：temp 目录由 AstrBot 自己在启动时创建，
    官方定义里它就是临时的，删掉会重新生成。**只删文件，不删目录**——
    删掉目录会让正在往里写文件的进程报错。

    dry_run=True 时只统计不删，面板总览靠它显示占用。
    """
    root = _temp_root()
    result = {
        "ok": True, "root": str(root), "exists": root.is_dir(), "dry_run": dry_run,
        "scanned_files": 0, "total_bytes": 0, "deleted_files": 0, "freed_bytes": 0,
        "failed": 0, "too_recent": 0, "oldest_days": None, "error": "",
    }
    if not root.is_dir():
        result["exists"] = False
        return result
    cutoff = time.time() - max(0, min_age_days) * 86400
    now = time.time()
    for path in root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            result["scanned_files"] += 1
            stat = path.stat()
            result["total_bytes"] += stat.st_size
            age_days = round((now - stat.st_mtime) / 86400, 1)
            if result["oldest_days"] is None or age_days > result["oldest_days"]:
                result["oldest_days"] = age_days
            if stat.st_mtime >= cutoff:
                result["too_recent"] += 1
                continue
            if dry_run:
                result["deleted_files"] += 1
                result["freed_bytes"] += stat.st_size
                continue
            path.unlink()
            result["deleted_files"] += 1
            result["freed_bytes"] += stat.st_size
        except OSError:
            result["failed"] += 1
    result["ok"] = result["failed"] == 0
    return result


async def temp_scan() -> dict[str, Any]:
    return await asyncio.to_thread(temp_cleanup_sync, dry_run=True)


async def temp_cleanup(min_age_days: int = _TEMP_MIN_AGE_DAYS) -> dict[str, Any]:
    return await asyncio.to_thread(temp_cleanup_sync, min_age_days)


def _norm_plugin_name(value: str) -> str:
    """把插件名字归一成可比的形式。

    这里曾经出过真错：拿 plugin_data 的目录名去和注册表里的名字**全等**比对，
    而前者是 root_dir_name（目录名）、后者是 metadata.name（可以带
    astrbot_plugin_ 前缀，也可以不带）。两者结构上就可能对不上，
    于是**正在用的插件被误报成残留**——对「不误删」的插件来说这是信任基础。

    所以不假设任何一种命名，只归一化前缀再比。
    """
    name = str(value or "").strip().lower().replace("-", "_")
    for prefix in ("astrbot_plugin_", "astrbot_"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _installed_plugin_names() -> tuple[set[str], bool]:
    """data/plugins 下真正装着（且带 metadata.yaml）的目录名。

    这是比注册表更硬的依据：注册表会随启停变化，磁盘不会。
    一个 plugin_data 目录在磁盘上找不到对应插件，那它就真的没人用了。

    第二个返回值是「读到了没有」。**读不到必须和「读到了、里面是空的」
    区分开**：前者是异常，拿它当「一个插件都没装」会把一整片正常数据
    误报成垃圾——宁可漏报，绝不误报。
    """
    root = _plugins_root()
    names: set[str] = set()
    if not root.is_dir():
        return names, False
    try:
        children = list(root.iterdir())
    except OSError:
        return names, False
    for child in children:
        try:
            if child.is_dir() and not child.is_symlink() and (
                child / _PLUGIN_MARKER
            ).is_file():
                names.add(child.name)
        except OSError:
            continue
    return names, True


def orphan_plugin_data_sync(loaded: set[str] | None = None) -> dict[str, Any]:
    """插件卸载了，`data/plugin_data/<名字>/` 还留着。

    判据是**事实对事实**，而且只信磁盘：
    先列出 `data/plugins` 下真正装着（带 metadata.yaml）的目录，
    再把 plugin_data 里的目录名归一化前缀后比对。
    注册表只作补充——它会随启停变化，拿它当主依据会把「已停用」误报成「已卸载」。

    **只报告不删除。** 那些数据可能是用户特意留的（比如某个插件的向量库、
    历史统计）。要删由用户点。
    """
    root = _plugin_data_root()
    result: dict[str, Any] = {
        "root": str(root), "exists": root.is_dir(),
        "items": [], "total_bytes": 0,
    }
    if not root.is_dir():
        return result
    budget = _Budget()
    try:
        children = sorted(
            (c for c in root.iterdir() if c.is_dir() and not c.is_symlink()),
            key=lambda p: p.name,
        )
    except OSError as exc:
        result["error"] = type(exc).__name__
        return result
    installed, readable = _installed_plugin_names()
    if not readable:
        # 磁盘读不到（目录不存在 / 没权限）——**一个都不报**。
        # 继续往下走的话 alive 是空集，等于断言「一个插件都没装」，
        # 于是整片正常数据都会被说成垃圾。读不到 ≠ 都没装。
        result["unreadable"] = True
        return result
    alive = {_norm_plugin_name(n) for n in installed}
    alive |= {_norm_plugin_name(n) for n in (loaded or set())}
    for child in children:
        if _norm_plugin_name(child.name) in alive:
            continue
        row = _measure(child, budget)
        # label 必须有：面板与待办文案都按它显示。之前漏了，导致
        # 只要存在残留目录，overview 就因 KeyError 整个 500。
        row["label"] = child.name
        try:
            row["age_days"] = round(
                (time.time() - child.stat().st_mtime) / 86400, 1
            )
        except OSError:
            row["age_days"] = None
        row["loaded"] = False
        result["items"].append(row)
        result["total_bytes"] += int(row["size_bytes"])
    result["items"].sort(key=lambda r: r["size_bytes"], reverse=True)
    return result


async def orphan_plugin_data(loaded: set[str] | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(orphan_plugin_data_sync, loaded)


def _is_broken_install(path: Path) -> bool:
    """没有 metadata.yaml 的目录：装到一半失败，AstrBot 不会加载它。"""
    return path.is_dir() and not (path / _PLUGIN_MARKER).is_file()


def _plugin_data_group(
    budget: _Budget, loaded: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """逐个插件的数据目录。

    现在空间地图只看 `data/` 的直接子目录，于是 `plugin_data` 只是一个名字，
    看不出是哪个插件在吃磁盘。而这恰好是「哪个插件可以卸载了」的唯一依据。

    loaded 是 AstrBot 里仍然注册着的插件名；不在里面的标成 orphan（没人用了）。
    """
    root = _plugin_data_root()
    rows: list[dict[str, Any]] = []
    if not root.is_dir():
        return {
            "title": "插件数据目录",
            "hint": f"没找到 {root}（装了插件之后才会有）",
            "rows": [],
        }
    try:
        children = sorted(
            (c for c in root.iterdir() if c.is_dir() and not c.is_symlink()),
            key=lambda p: p.name,
        )
    except OSError as exc:
        return {
            "title": "插件数据目录",
            "hint": f"读取失败：{type(exc).__name__}",
            "rows": [],
        }
    # 与 orphan_plugin_data_sync 用同一套判据：磁盘为准 + 前缀归一化。
    # 两处算法不一致的话，会出现「待办说有残留、空间地图说没有」的矛盾。
    installed, readable = _installed_plugin_names()
    alive = {_norm_plugin_name(n) for n in loaded}
    if readable:
        alive |= {_norm_plugin_name(n) for n in installed}
    orphans = 0
    for child in children:
        row = _measure(child, budget)
        row["label"] = child.name
        # 磁盘读不到时不标任何一条：读不到 ≠ 都没装
        row["orphan"] = readable and _norm_plugin_name(child.name) not in alive
        if row["orphan"]:
            orphans += 1
        rows.append(row)
    # 没人用的排最前：它们既占地方，又是最可能被误当成「还在用」的那批
    rows.sort(key=lambda r: (not r["orphan"], -int(r["size_bytes"])))
    return {
        "title": "插件数据目录",
        "hint": (
            f"每个插件一块；其中 {orphans} 个对应的插件已不在 AstrBot 里（没人用了）"
            if orphans else "每个插件一块，暂无可疑的残留"
        ),
        "rows": _rank(rows, limit=20),
    }


def _official_group(budget: _Budget) -> dict[str, Any]:
    """官方建过的、但不在 data 目录下的那几个：临时目录与知识库。"""
    paths = official_paths()
    rows: list[dict[str, Any]] = []
    for key, label, hint in (
        ("temp", "临时目录", "下载的图片语音都在这，删了会重新生成"),
        ("knowledge", "知识库", "向量库；删了知识库就没了，所以只看不删"),
    ):
        value = paths.get(key, "")
        if not value:
            continue
        path = Path(value)
        row = _measure(path, budget)
        row["label"] = label
        row["hint"] = hint
        row["path"] = str(path)
        rows.append(row)
    return {
        "title": "官方临时目录与知识库",
        "hint": str(paths.get("root") or "根目录未知"),
        "rows": rows,
    }


def _database_group() -> dict[str, Any]:
    """数据库与它的 WAL 文件。

    AstrBot 用 SQLite 且开了 WAL，消息历史与附件只增不减——这是容器里
    最容易膨胀、而又完全看不见的一块。**只报大小，一个字节都不碰。**
    """
    rows: list[dict[str, Any]] = []
    for path in _database_files():
        try:
            size = path.stat().st_size
            mtime = path.stat().st_mtime
        except OSError:
            continue
        name = path.name
        if name.endswith("-wal"):
            kind = "WAL 暂存"
        elif name.endswith("-shm"):
            kind = "共享内存"
        else:
            kind = "数据库"
        rows.append({
            "label": name,
            "path": str(path),
            "size_bytes": size,
            "file_count": 1,
            "kind": kind,
            "mtime": mtime,
            "truncated": False,
        })
    total = sum(int(row["size_bytes"]) for row in rows)
    wal = sum(
        int(row["size_bytes"]) for row in rows
        if row["kind"] == "WAL 暂存"
    )
    return {
        "title": "数据库",
        "hint": (
            f"合计 {human_bytes(total)}"
            + (f"，其中 WAL 暂存 {human_bytes(wal)}" if wal else "")
            + "。WAL 长期不收缩说明上次没正常关闭。只读，插件不会碰它的内容。"
        ),
        "rows": sorted(rows, key=lambda r: r["size_bytes"], reverse=True),
        "total_bytes": total,
        "wal_bytes": wal,
    }


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


# 垃圾自动清理的年龄锁。判据「没有 metadata.yaml」会把「正在安装中的插件」
# 也包含进去——它同样是刚拷进去、还没写元数据。不加这道锁，定时体检会在
# 插件装完之前把它连目录一起删掉。
_JUNK_MIN_AGE_DAYS = 7


def _too_recent(path: Path, min_age_days: int) -> bool:
    try:
        age_days = (time.time() - path.stat().st_mtime) / 86400
    except OSError:
        return True
    return age_days < min_age_days


def _junk_summary(sets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    counts = {kind: len(sets.get(kind, [])) for kind in ("broken", "zip", "pycache")}
    return {
        "count": sum(counts.values()),
        "bytes": sum(
            int(item.get("size_bytes", 0) or 0)
            for items in sets.values()
            for item in items
        ),
        **counts,
    }


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
            "all": _junk_summary({}),
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
            row["protected"] = _too_recent(path, _JUNK_MIN_AGE_DAYS)
            pycache.append(row)
            total_bytes += int(row["size_bytes"])
            dirnames[:] = []
            continue
        if path.parent == root and _is_broken_install(path):
            # plugins 目录的直接子目录却没有 metadata.yaml = 装到一半失败
            row = _measure(path, budget)
            row["kind"] = "broken"
            row["protected"] = _too_recent(path, _JUNK_MIN_AGE_DAYS)
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
                "protected": _too_recent(item, _JUNK_MIN_AGE_DAYS),
            })
            total_bytes += size
    sets = {"broken": broken, "zip": zips, "pycache": pycache}
    return {
        "root": str(root),
        "root_exists": True,
        "broken": broken,
        "zips": zips,
        "pycache": pycache,
        "total_bytes": total_bytes,
        "all": _junk_summary(sets),
        "protected_count": sum(
            1 for items in sets.values() for item in items if item.get("protected")
        ),
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


def purge_all_junk_sync(*, min_age_days: int = _JUNK_MIN_AGE_DAYS) -> dict[str, Any]:
    """一键清除：判据与逐项删除完全相同，只是少点几十次。

    仍然受年龄锁约束。「没有 metadata.yaml」既可能是装到一半失败，也可能是
    一个正在安装中的插件——后者还没装完，不该被扫掉。宁可留着让人看，
    也不能在它装完之前删掉。

    跑之前重新扫一遍并逐项重新校验，不信任上一次扫描的结论：那次扫描到
    现在之间，目录可能已经被装完或者移走了。
    """
    report = plugin_junk_sync()
    if not report.get("root_exists"):
        return {"ok": False, "error": "插件目录不存在", "deleted": 0, "freed_bytes": 0}
    deleted = 0
    freed = 0
    failed = 0
    protected = 0
    detail: list[str] = []
    # 键名必须跟 plugin_junk_sync 返回的对上：zip 的键是 zips（复数）。
    # 写成 "zip" 的话安装包这一类永远扫不到，一键清除看着就是「没反应」。
    for kind, key in (("broken", "broken"), ("zip", "zips"), ("pycache", "pycache")):
        for item in report.get(key) or []:
            path = Path(str(item.get("path", "")))
            # 年龄锁只对「可能是正在安装的目录 / 刚放进去的安装包」有意义。
            # __pycache__ 是编译缓存：每次导入 Python 都会刷新它的 mtime，
            # 拿年龄锁去卡它，等于永远卡住——这就是「一键清除什么都没删」的原因。
            if kind != "pycache" and _too_recent(path, min_age_days):
                protected += 1
                detail.append(f"{kind} 跳过（太新）：{path.name}")
                continue
            outcome = remove_junk_sync(kind, str(path))
            if outcome.get("ok"):
                deleted += 1
                freed += int(outcome.get("freed_bytes", 0) or 0)
                detail.append(f"{kind} 已删：{path.name}")
            else:
                failed += 1
                detail.append(f"{kind} 没删掉：{path.name}")
    return {
        "ok": failed == 0,
        "deleted": deleted,
        "freed_bytes": freed,
        "failed": failed,
        "protected": protected,
        "detail": detail[:30],
        "error": "" if failed == 0 else f"{failed} 项没能删掉",
    }


async def purge_all_junk(min_age_days: int = _JUNK_MIN_AGE_DAYS) -> dict[str, Any]:
    return await asyncio.to_thread(purge_all_junk_sync, min_age_days=min_age_days)


# ---------- 日志保留 ----------


# ---------- 官方路径：两种部署形态的唯一真相来源 ----------


def official_paths() -> dict[str, str]:
    """解析 AstrBot 自己的目录，缺哪个就少哪个，绝不抛异常。

    两种部署形态的根目录不一样：源码 / Docker 是 `ASTRBOT_ROOT` 或启动目录下的
    `data/`，桌面版是 `~/.astrbot/`。写死任何一个都会在另一种部署上静默失效——
    所以一律向官方要，并且**每个函数单独 try**。AstrBot 改版加了新函数、或者某个
    老版本没有，表现为那一项拿不到，而不是整个插件起不来。
    """
    names = {
        "root": "get_astrbot_root",
        "data": "get_astrbot_data_path",
        "config": "get_astrbot_config_path",
        "plugin": "get_astrbot_plugin_path",
        "knowledge": "get_astrbot_knowledge_base_path",
        "temp": "get_astrbot_temp_path",
        "system_tmp": "get_astrbot_system_tmp_path",
    }
    result: dict[str, str] = {}
    for key, func_name in names.items():
        try:
            from astrbot.core.utils import astrbot_path

            func = getattr(astrbot_path, func_name, None)
            if not callable(func):
                continue
            value = str(func() or "").strip()
            if value:
                result[key] = value
        except Exception:
            continue
    if "data" not in result:
        result["data"] = str(_data_root())
    return result


def _log_roots_ordered() -> list[Path]:
    """日志目录的候选，按「最可能命中」排序。

    桌面版（`~/.astrbot/logs/`）与源码版（`data/logs`）的 logs 位置不同，
    两个都试；这也是以前桌面部署上「日志永远是 0」的原因——那时只认 data/logs。
    """
    paths = official_paths()
    candidates: list[str] = []
    # 本地数据目录排最前：它就是测试与实际运行时的约定基准。
    # 桌面版的 logs 不在 data/logs 下，接在后面兼底。
    candidates.append(str(Path(str(_data_root())) / "logs"))
    root = paths.get("root", "")
    data = paths.get("data", "")
    if root:
        candidates.append(str(Path(root) / "logs"))
    if data:
        candidates.append(str(Path(data) / "logs"))
        candidates.append(str(Path(data).parent / "logs"))
    candidates.append(str(Path.cwd() / "logs"))
    seen: set[Path] = set()
    ordered: list[Path] = []
    for item in candidates:
        try:
            resolved = Path(item).resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(resolved)
    return ordered


def _logs_root() -> Path:
    """实际在用的日志目录：取第一个存在的；都不存在时沿用 data/logs。"""
    for candidate in _log_roots_ordered():
        if candidate.is_dir():
            return candidate
    paths = official_paths()
    return Path(paths.get("data", str(_data_root()))) / "logs"


def _is_current_log(path: Path) -> bool:
    return path.name in _CURRENT_LOG_NAMES


# 终端里看着满屏输出、面板上却报「0 B」——这几乎总是因为 AstrBot 写日志的
# 地方和 data/logs 不是同一个。所以盘点时不假定任何固定位置，把能想到的
# 都列出来，并且逐个标明存在与否、为什么。日志路径可配，容器里挂到别处时能填。
_LOG_ENV_HINTS = ("ASTRBOT_LOG_DIR", "ASTRBOT_LOGS_DIR", "ASTRBOT_LOG_PATH")


def _log_roots(explicit: list[str]) -> list[dict[str, Any]]:
    """候选日志目录，按「最可能命中」排序。返回值保留来源说明。"""
    found: list[dict[str, Any]] = []
    seen: set[Path] = set()

    def push(source: str, value: str | Path) -> None:
        text = str(value or "").strip()
        if not text:
            return
        try:
            path = Path(text).expanduser()
            if not path.is_absolute():
                return
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError):
            return
        if resolved in seen:
            return
        seen.add(resolved)
        found.append({"path": str(resolved), "source": source})

    for value in explicit:
        push("手动配置", value)
    for key in _LOG_ENV_HINTS:
        push(f"环境变量 {key}", os.environ.get(key, ""))
    for candidate in _log_roots_ordered():
        push("AstrBot 官方路径", candidate)
    return found


def _scan_log_dir(root: Path, budget: _Budget) -> dict[str, Any]:
    """一遍扫完：体积、最大文件、轮转份数、是否还在写、最旧的多旧。

    共享预算：日志目录被挂到很大的盘上时，不设上限能把面板卡死。
    """
    result: dict[str, Any] = {
        "exists": False, "file_count": 0, "size_bytes": 0, "largest": "",
        "largest_bytes": 0, "rotated": 0, "oldest_days": None,
        "written_24h": 0, "truncated": False,
    }
    if not root.is_dir():
        return result
    result["exists"] = True
    cutoff = time.time() - 86400
    now = time.time()
    for path in root.rglob("*"):
        if not budget.take():
            result["truncated"] = True
            break
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        size = stat.st_size
        result["file_count"] += 1
        result["size_bytes"] += size
        if size > result["largest_bytes"]:
            result["largest_bytes"] = size
            result["largest"] = path.name
        if not _is_current_log(path):
            result["rotated"] += 1
        if stat.st_mtime >= cutoff:
            result["written_24h"] += size
        age_days = round((now - stat.st_mtime) / 86400, 1)
        if result["oldest_days"] is None or age_days > result["oldest_days"]:
            result["oldest_days"] = age_days
    return result


def log_inventory_sync(explicit: list[str] | None = None) -> dict[str, Any]:
    """盘一遍日志到底在哪、多大。

    纯只读。一个目录都没找到时 missing=True——这和「找到了但确实是 0 字节」
    是两回事，面板必须分开说，否则用户会以为插件没在工作。
    """
    budget = _Budget()
    total = {
        "file_count": 0, "total_bytes": 0, "largest": "", "largest_bytes": 0,
        "rotated": 0, "oldest_days": None, "written_24h": 0,
    }
    roots = _log_roots(list(explicit or []))
    primary = ""
    for item in roots:
        scan = _scan_log_dir(Path(item["path"]), budget)
        item.update(scan)
        if not scan["exists"]:
            continue
        if not primary:
            primary = item["path"]
        total["file_count"] += scan["file_count"]
        total["total_bytes"] += scan["size_bytes"]
        if scan["largest_bytes"] > total["largest_bytes"]:
            total["largest"] = scan["largest"]
            total["largest_bytes"] = scan["largest_bytes"]
        total["rotated"] += scan["rotated"]
        total["written_24h"] += scan["written_24h"]
        if scan["oldest_days"] is not None and (
            total["oldest_days"] is None or scan["oldest_days"] > total["oldest_days"]
        ):
            total["oldest_days"] = scan["oldest_days"]
    return {
        "roots": roots,
        "root": primary or str(_logs_root()),
        "missing": not primary,
        "truncated": budget.exhausted,
        **total,
    }


async def log_inventory(directories: list[str] | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(log_inventory_sync, directories)


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
