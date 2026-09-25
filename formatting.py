from __future__ import annotations

from typing import Any


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


def chunk_text(value: str, limit: int = 3500) -> list[str]:
    text = str(value or "")
    if not text:
        return ["（无内容）"]
    return [text[index : index + limit] for index in range(0, len(text), limit)]


def format_directory_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "尚未配置 NapCat 文件缓存目录，可在 WebUI 面板点「自动探测」。"
    lines = ["NapCat 文件缓存目录"]
    for row in rows:
        lines.append(
            f"- {row['path']} | {human_bytes(row.get('size_bytes', 0))} | "
            f"{row.get('file_count', 0)} 个文件 | 跳过符号链接 {row.get('skipped_symlinks', 0)}"
            + (f" | 错误 {row['error']}" if row.get("error") else "")
        )
    return "\n".join(lines)


def format_module_rows(rows: list[dict[str, Any]]) -> str:
    lines = [f"已加载插件模块组：{len(rows)}"]
    for row in rows:
        if row.get("self"):
            flag = "运行中（本插件）"
        else:
            flag = "运行中" if row.get("activated") else "已停用"
        lines.append(
            f"- {row['scope']} / {row['plugin_id']}：{row.get('module_count', 0)} 个模块（{flag}）"
        )
    return "\n".join(lines)


def format_sweep_records(records: list[dict[str, Any]], *, force: bool = False) -> str:
    lines = ["全量强制清理完成" if force else "体检完成"]
    for record in records:
        if record.get("error"):
            lines.append(f"- {record['label']}：失败（{record['error']}）")
        elif record.get("acted"):
            lines.append(f"- {record['label']}：已清理，{record.get('detail', '')}")
        else:
            lines.append(f"- {record['label']}：未处理，{record.get('skipped', '')}")
    freed = sum(int(item.get("freed_bytes", 0)) for item in records)
    if freed:
        lines.extend(["", f"本次释放 {human_bytes(freed)}"])
    return "\n".join(lines)


def format_overview(overview: dict[str, Any]) -> str:
    schedule = overview.get("schedule", {})
    totals = overview.get("totals", {})
    napcat = overview.get("napcat", {})
    state = "连接正常" if napcat.get("connected") else f"连接异常 {napcat.get('error', '')}"
    lines = [
        f"NapCat：{napcat.get('endpoint', '未知')}（{state}）",
        f"自动清理：{'已开启' if schedule.get('auto_enabled') else '已暂停'}"
        f"，体检频率 {schedule.get('cron_effective') or '未注册'}",
        f"下次体检：{schedule.get('next_run') or '未注册'}",
        f"上次体检：{totals.get('last_sweep_at') or '从未'}"
        f"，释放 {human_bytes(totals.get('last_freed_bytes', 0))}",
        "",
    ]
    for layer in overview.get("layers", {}).values():
        if not layer.get("enabled"):
            marker = "未启用"
        else:
            marker = "待清理" if layer.get("due") else "正常"
        line = f"[{marker}] {layer['label']}"
        if layer.get("amount"):
            line += f"：{layer['amount']}"
        if layer.get("detail"):
            line += f" —— {layer['detail']}"
        lines.append(line)
    return "\n".join(lines)
