"""Orbit Cache：自动维护停用插件模块、AstrBot 与 NapCat 缓存。

默认全自动工作，不需要管理员定期手动操作：

  1. 插件一被停用或卸载，立刻清掉它残留在 sys.modules 里的模块
  2. 框架 cron 按 crontab 周期体检，各层超过设定条件才真正动手
  3. WebUI 面板是唯一的查看与调整入口

聊天里只留一条 /orbit 用于快速瞄一眼，旧指令保留为别名。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request

from .auth import TokenResolver, probe_state
from .discovery import onebot_configs, redact_configs, scan
from .formatting import (
    chunk_text,
    format_directory_rows,
    format_module_rows,
    format_overview,
    format_sweep_records,
)
from .maintenance import (
    AstrBotCacheCleaner,
    ModuleCacheError,
    ModuleCacheManager,
)
from .napcat import NapCatCacheClient, NapCatEndpoint
from .scheduler import LAYERS, SweepEngine
from .transport import validate_base_url

PLUGIN_ID = "astrbot_plugin_orbit_command"
JOB_NAME = "orbit-sweep"
DEPRECATION_HINT = "本插件已改为自动模式，建议改用 /orbit 或 WebUI 面板。"

_HELP = (
    "/orbit 看四层缓存与调度状态\n"
    "/orbit clean 立即强制清理全部\n"
    "/orbit on 开启自动 / off 暂停自动\n"
    "/orbit purge <插件ID> 立即清某个插件的内存模块"
)

def admin_command(name: str, **kwargs: Any):
    def decorate(func):
        registered = filter.command(name, **kwargs)(func)
        return filter.permission_type(filter.PermissionType.ADMIN)(registered)

    return decorate


class OrbitCachePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.napcat = NapCatCacheClient(
            self._build_endpoints(), reauth=self._on_auth_failure
        )
        self.resolver = TokenResolver(
            config=self.config,
            data_path=self._data_path(),
            explicit_dirs=self._token_dirs,
            timeout=self._config_int("request_timeout_seconds", 10, 1, 120),
            verify_tls=self._config_bool("verify_tls", True),
        )
        self.auth_state: dict[str, Any] = {
            "mode": "unknown", "detail": "", "source": "", "resolved": False,
        }
        self.astrbot_cache = AstrBotCacheCleaner()
        self.module_cache = ModuleCacheManager(context, self.name)
        self.engine = SweepEngine(
            config=self.config,
            napcat_client=self.napcat,
            astrbot_cache=self.astrbot_cache,
            module_cache=self.module_cache,
            data_dir=StarTools.get_data_dir(PLUGIN_ID),
        )
        self._job_id = ""
        self._register_web_api()

    # ---------- 配置读取 ----------

    def _config_str(self, key: str, default: str) -> str:
        value = self.config.get(key, default)
        return str(value) if isinstance(value, str) else default

    def _config_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        return value if isinstance(value, bool) else default

    def _config_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(self.config.get(key, default))
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    # ---------- 生命周期 ----------

    async def initialize(self) -> None:
        await self._start_endpoints()
        # 必须挂在 initialize 而不是 on_astrbot_loaded：后者只在 AstrBot 启动完成时
        # 触发一次，而热重载走 PluginManager.reload()，不会重新触发它。
        # terminate() 已经把定时任务删了，不在这里重建的话，插件一重载自动清理就静默失效。
        await self.refresh_auth()
        await self.sync_schedule()

    def _data_path(self) -> Any:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path())
        except Exception:
            return Path.cwd() / "data"

    def _on_auth_failure(self) -> None:
        """收到 401/403：作废缓存并在后台重新解析，下一次重试就会用上新 token。"""
        base_url = self._config_str("onebot_base_url", "")
        self.resolver.invalidate(base_url)
        logger.warning("Orbit Cache 收到鉴权失败，正在重新解析 NapCat Token")
        asyncio.create_task(self.refresh_auth())

    async def refresh_auth(self, *, force: bool = False) -> dict[str, Any]:
        """逐实例解析可用 token，并汇总成整体状态。"""
        results: list[dict[str, Any]] = []
        failed: dict[str, Any] | None = None
        with_token = 0
        changed = False
        rows = self._instance_rows()
        for row in rows:
            url = row["url"]
            result = await self.resolver.resolve(url, force=force)
            token = str(result.get("token") or "")
            if token and token != row["token"]:
                row["token"] = token
                self.napcat.set_token(url, token)
                changed = True
            results.append(result)
            if token:
                with_token += 1
            elif result.get("source") == "无需 Token":
                pass
            elif failed is None:
                failed = result

        if not results:
            self.auth_state = {
                "mode": "no_url", "detail": "尚未配置 NapCat 地址",
                "source": "", "resolved": False,
            }
        elif failed is not None:
            self.auth_state = {
                "mode": "auth_required" if failed.get("needs_token") else "unreachable",
                "detail": str(failed.get("note") or ""),
                "source": "",
                "resolved": False,
            }
        else:
            sources = "、".join(
                str(r.get("source") or "") for r in results if r.get("source")
            )
            self.auth_state = {
                "mode": "ok" if with_token else "ok_no_token",
                "detail": f"{len(results)} 个实例已连通",
                "source": sources,
                "resolved": True,
            }
        if changed:
            self._write_instance_rows(rows)
        return results[0] if len(results) == 1 else {"instances": results, **self.auth_state}

    async def terminate(self) -> None:
        await self._remove_cron_job()
        for endpoint in self.napcat.endpoints:
            try:
                await endpoint.transport.close()
            except Exception as exc:
                logger.debug("Orbit Cache 关闭连接失败：%s", exc)

    @filter.on_plugin_unloaded()
    async def on_plugin_unloaded(self, metadata: Any) -> None:
        """停用或卸载插件时立刻清掉它的模块。

        AstrBot 的 turn_off_plugin 只调 terminate()，从不清理 sys.modules，
        停用插件的代码会一直驻留内存直到重启。
        """
        if not self._config_bool("auto_clean_plugin_modules", True):
            return
        plugin_id = str(
            getattr(metadata, "root_dir_name", "") or getattr(metadata, "name", "")
        )
        try:
            removed = self.module_cache.purge_loaded(plugin_id)
        except ModuleCacheError as exc:
            logger.info("Orbit Cache 跳过 %s 的模块清理：%s", plugin_id, exc)
            return
        if removed:
            logger.info(
                "Orbit Cache 插件卸载时清理 %s 的 %d 个模块", plugin_id, len(removed)
            )

    # ---------- 调度 ----------

    def _cron_manager(self) -> Any:
        return getattr(self.context, "cron_manager", None)

    async def _remove_cron_job(self) -> None:
        cron_manager = self._cron_manager()
        if cron_manager is None or not self._job_id:
            return
        try:
            await cron_manager.delete_job(self._job_id)
        except Exception as exc:
            logger.warning("Orbit Cache 移除定时任务失败：%s", exc)
        self._job_id = ""

    async def sync_schedule(self) -> dict[str, Any]:
        """按当前配置重建 cron 任务。热重载和面板改配置后都会走到这里。"""
        cron_manager = self._cron_manager()
        await self._remove_cron_job()
        if cron_manager is None:
            self.engine.set_schedule(cron=self.engine.sweep_cron, next_run="框架不支持", job_id="")
            return self.engine.settings()
        try:
            stale = [
                job
                for job in await cron_manager.list_jobs("basic")
                if job.name == JOB_NAME
            ]
            for job in stale:
                await cron_manager.delete_job(job.job_id)
        except Exception as exc:
            logger.warning("Orbit Cache 清理残留定时任务失败：%s", exc)

        if not self.engine.auto_enabled:
            self.engine.set_schedule(cron="已暂停", next_run="", job_id="")
            return self.engine.settings()

        expression = self.engine.sweep_cron
        try:
            job = await cron_manager.add_basic_job(
                name=JOB_NAME,
                cron_expression=expression,
                handler=self._cron_sweep,
                description="Orbit Cache 自动体检与条件清理",
                persistent=False,
            )
        except Exception as exc:
            logger.error("Orbit Cache 注册定时任务失败（表达式 %s）：%s", expression, exc)
            self.engine.set_schedule(cron=expression, next_run="注册失败", job_id="")
            return self.engine.settings()

        self._job_id = job.job_id
        next_run = ""
        try:
            upcoming = cron_manager.get_next_run_time(job.job_id)
            if upcoming is not None:
                next_run = upcoming.astimezone().isoformat(timespec="seconds")
        except Exception as exc:
            logger.warning("Orbit Cache 读取下次执行时间失败：%s", exc)
        self.engine.set_schedule(cron=expression, next_run=next_run, job_id=job.job_id)
        logger.info("Orbit Cache 已注册自动体检任务：%s，下次 %s", expression, next_run or "未知")
        return self.engine.settings()

    async def _cron_sweep(self) -> None:
        if not self.engine.auto_enabled:
            return
        result = await self.engine.sweep()
        if result.get("acted") or result.get("error"):
            logger.info(
                "Orbit Cache 自动体检：释放 %s 字节，%s",
                result.get("freed_bytes", 0),
                result.get("error") or "全部正常",
            )
        if self._job_id:
            cron_manager = self._cron_manager()
            if cron_manager is not None:
                try:
                    upcoming = cron_manager.get_next_run_time(self._job_id)
                    self.engine.set_schedule(
                        cron=self.engine.sweep_cron,
                        next_run=upcoming.astimezone().isoformat(timespec="seconds")
                        if upcoming
                        else "",
                        job_id=self._job_id,
                    )
                except Exception:
                    pass

    # ---------- Web API ----------

    def _register_web_api(self) -> None:
        reg = self.context.register_web_api
        reg(f"/{PLUGIN_ID}/overview", self.api_overview, ["GET"], "四层缓存与调度总览")
        reg(f"/{PLUGIN_ID}/plugins", self.api_plugins, ["GET"], "插件模块占用")
        reg(f"/{PLUGIN_ID}/history", self.api_history, ["GET"], "清理历史")
        reg(f"/{PLUGIN_ID}/discover", self.api_discover, ["GET"], "自动探测 NapCat 缓存目录")
        reg(f"/{PLUGIN_ID}/token", self.api_token_get, ["GET"], "探测 NapCat 鉴权与配置")
        reg(f"/{PLUGIN_ID}/token", self.api_token_apply, ["POST"], "实例增删改与 Token 解析")
        reg(f"/{PLUGIN_ID}/dryrun", self.api_dryrun, ["POST"], "试运行：只算不删")
        reg(f"/{PLUGIN_ID}/sweep", self.api_sweep, ["POST"], "立即体检或强制清理")
        reg(f"/{PLUGIN_ID}/settings", self.api_settings, ["POST"], "保存可调项")
        reg(f"/{PLUGIN_ID}/purge", self.api_purge, ["POST"], "立即清某个插件的模块")

    async def api_overview(self):
        try:
            overview = await self.engine.collect()
        except Exception as exc:
            return error_response(f"读取总览失败：{type(exc).__name__}", status_code=500)
        return json_response(
            {
                **overview,
                "settings": self.engine.settings(),
                "connection": {
                    "auth": self.auth_state,
                    "instances": [
                        {**row, "token": "***" if row["token"] else "",
                         "token_set": bool(row["token"])}
                        for row in self._instance_rows()
                    ],
                },
                "last_error": overview.get("last_error", ""),
            }
        )

    async def api_plugins(self):
        return json_response({"rows": self.module_cache.describe()})

    async def api_history(self):
        limit = request.query.get("limit", 50, type=int)
        return json_response({"history": self.engine.history(limit)})

    async def api_discover(self):
        try:
            found = await scan(self._token_dirs())
        except Exception as exc:
            return error_response(f"探测失败：{type(exc).__name__}", status_code=500)
        return json_response(
            {
                "candidates": found["cache_dirs"],
                "configs": found["configs"],
                "searched_roots": found["searched_roots"],
                "truncated": found["truncated"],
                "configured": self.engine.settings()["napcat_cache_dirs"],
            }
        )

    def _protocol_hint(self, configs: list[dict[str, Any]]) -> str:
        """根据读到的 NapCat 配置，给出可执行的建议。

        关键：区分「地址填错/对方没开端口」和「Token 不对」。
        前者是部署问题，改 Token 永远没用。
        """
        if not configs:
            return (
                "没读到 NapCat 的配置文件。若它与 AstrBot 不共享文件系统（常见于分容器部署），"
                "插件无法自动获取 Token，需要手动填写。"
            )
        connectable = [
            server
            for entry in configs
            for server in entry.get("servers", [])
            if server.get("can_connect") and server.get("enable")
        ]
        if connectable:
            addrs = "、".join(
                server.get("address", "") for server in connectable[:3]
            )
            return (
                f"NapCat 对外监听了这些端点，可直接填进地址栏：{addrs}"
                "（插件 HTTP 与 WebSocket 都支持，地址带 ws:// 开头即可）"
            )
        kinds = {
            server.get("kind")
            for entry in configs
            for server in entry.get("servers", [])
        }
        if kinds and kinds <= {"ws_client", "http_client"}:
            return (
                "该 NapCat 只配了反向连接（websocketClients / httpClients），"
                "它主动连出去、自己不对外监听，所以本插件连不上它。"
                "请到 NapCat「网络配置」里给它加一个 HTTP 服务端（端口如 3000，"
                "Token 可与现有的一致或留空），然后把地址填到这里。"
            )
        return "读到了 NapCat 配置，但没找到已启用的对外监听端点，请检查它的网络配置。"

    async def api_token_get(self):
        rows = self._instance_rows()
        configs = await onebot_configs(self._token_dirs())
        states = []
        for row in rows:
            state = await probe_state(
                row["url"],
                row["token"],
                timeout=self._config_int("request_timeout_seconds", 10, 1, 120),
                verify_tls=self._config_bool("verify_tls", True),
            )
            states.append({**row, "token": "", "state": state})
        return json_response(
            {
                "instances": states,
                "auth": self.auth_state,
                "hint": self._protocol_hint(configs),
                "configs": redact_configs(configs),
            }
        )

    async def api_token_apply(self):
        payload = await request.json(default={})
        action = str(payload.get("action") or "")

        if action == "save_instances":
            raw = payload.get("instances")
            if not isinstance(raw, list):
                return error_response("instances 必须是数组", status_code=400)
            rows = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip().rstrip("/")
                if not url:
                    continue
                try:
                    validate_base_url(url)
                except ValueError as exc:
                    return error_response(str(exc), status_code=400)
                try:
                    interval = max(1, int(item.get("protocol_interval_hours") or 12))
                except (TypeError, ValueError):
                    interval = 12
                rows.append({
                    "url": url,
                    "token": str(item.get("token") or "").strip(),
                    "enable": item.get("enable", True) is not False,
                    "protocol_interval_hours": interval,
                })
            if not rows:
                return error_response("至少需要一个实例", status_code=400)
            self.config["onebot_instances"] = rows
            # 旧字段清掉，避免面板改了实例却仍被 legacy 分支读到
            self.config["onebot_base_url"] = ""
            self.config["onebot_access_token"] = ""
            self.resolver.forget_manual()
            await self._restart_transport()
            await self._start_endpoints()
            result = await self.refresh_auth(force=True)
            self.config.save_config()
            return json_response(
                {"ok": True, "count": len(rows), "auth": self.auth_state,
                 "note": str(result.get("note") or "")}
            )

        if action == "resolve":
            self.resolver.forget_manual()
            result = await self.refresh_auth(force=True)
            self.config.save_config()
            return json_response({"ok": True, "auth": self.auth_state,
                                  "note": str(result.get("note") or "")})

        if action == "apply_found":
            file_path = str(payload.get("file") or "")
            address = str(payload.get("address") or "").rstrip("/")
            if not file_path or not address:
                return error_response("缺少 file 或 address", status_code=400)
            matched = None
            for entry in await onebot_configs(self._token_dirs()):
                if entry.get("file") != file_path:
                    continue
                for server in entry.get("servers", []):
                    if server.get("address") == address:
                        matched = server
                        break
            if matched is None:
                return error_response("该配置项已变化，请重新探测", status_code=409)
            rows = self._instance_rows()
            token = str(matched.get("token") or "")
            for row in rows:
                if row["url"] == address:
                    row["token"] = token
                    matched = True
                    break
            if not any(row["url"] == address for row in rows):
                rows.append({
                    "url": address, "token": token, "enable": True,
                    "protocol_interval_hours": 12,
                })
            self.config["onebot_instances"] = rows
            self.config["onebot_base_url"] = ""
            self.config["onebot_access_token"] = ""
            self.resolver.forget_manual()
            await self._restart_transport()
            await self._start_endpoints()
            await self.refresh_auth(force=True)
            self.config.save_config()
            return json_response(
                {"ok": True, "count": len(rows),
                 "detail": "已添加该实例" + ("并填入 Token" if token else "（该实例无 Token）")}
            )

        return error_response(f"未知 action：{action or '(空)'}", status_code=400)

    def _token_dirs(self) -> list[str]:
        value = self.config.get("napcat_config_dirs", [])
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    def _instance_rows(self) -> list[dict[str, Any]]:
        """读出实例列表。

        新版用 onebot_instances（可多行）；为不弄坏已有配置，
        没有它时回落到旧的单个 onebot_base_url / onebot_access_token。
        """
        raw = self.config.get("onebot_instances", [])
        rows: list[dict[str, Any]] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip().rstrip("/")
                if not url:
                    continue
                try:
                    interval = max(1, int(item.get("protocol_interval_hours") or 12))
                except (TypeError, ValueError):
                    interval = 12
                rows.append({
                    "url": url,
                    "token": str(item.get("token") or "").strip(),
                    "enable": item.get("enable", True) is not False,
                    "protocol_interval_hours": interval,
                })
        if rows:
            return rows
        legacy_url = self._config_str("onebot_base_url", "").strip()
        if legacy_url:
            return [{
                "url": legacy_url.rstrip("/"),
                "token": self._config_str("onebot_access_token", ""),
                "enable": True,
                "protocol_interval_hours": self._config_int(
                    "napcat_protocol_interval_hours", 12, 1, 24 * 365),
            }]
        return []

    def _write_instance_rows(self, rows: list[dict[str, Any]]) -> None:
        """把解析到的 token 写回配置，否则重启后又得从头找一遍。"""
        if isinstance(self.config.get("onebot_instances"), list) and self.config.get(
            "onebot_instances"
        ):
            self.config["onebot_instances"] = rows
        elif len(rows) == 1:
            self.config["onebot_base_url"] = rows[0]["url"]
            self.config["onebot_access_token"] = rows[0]["token"]
        self.config.save_config()

    def _build_endpoints(self) -> list[NapCatEndpoint]:
        return [
            NapCatEndpoint(
                row["url"],
                row["token"],
                enable=row["enable"],
                protocol_interval_hours=row["protocol_interval_hours"],
                verify_tls=self._config_bool("verify_tls", True),
                timeout=self._config_int("request_timeout_seconds", 10, 1, 120),
                max_concurrency=self._config_int("max_concurrency", 2, 1, 8),
                interval_ms=self._config_int("request_interval_ms", 100, 0, 10000),
            )
            for row in self._instance_rows()
        ]

    async def _restart_transport(self) -> None:
        """实例列表变了必须重建连接池，否则继续用旧会话。"""
        for endpoint in self.napcat.endpoints:
            try:
                await endpoint.transport.close()
            except Exception as exc:
                logger.debug("Orbit Cache 关闭旧连接失败：%s", exc)
        self.napcat = NapCatCacheClient(
            self._build_endpoints(), reauth=self._on_auth_failure)
        self.engine.napcat_client = self.napcat

    async def _start_endpoints(self) -> None:
        for endpoint in self.napcat.endpoints:
            try:
                await endpoint.transport.start()
            except Exception as exc:
                logger.debug("Orbit Cache 连接建立失败：%s", exc)

    async def api_sweep(self):
        payload = await request.json(default={})
        force = bool(payload.get("force"))
        raw_layers = payload.get("layers")
        layers = [str(item) for item in raw_layers] if isinstance(raw_layers, list) else None
        if layers:
            unknown = [item for item in layers if item not in LAYERS]
            if unknown:
                return error_response(f"未知层：{'、'.join(unknown)}", status_code=400)
        result = await self.engine.sweep(force=force, layers=layers)
        if not result.get("ok") and not result.get("records"):
            return error_response(str(result.get("error") or "清理未执行"), status_code=409)
        return json_response(result)

    async def api_dryrun(self):
        payload = await request.json(default={})
        return json_response(await self.engine.dry_run(force=bool(payload.get("force"))))

    async def api_settings(self):
        payload = await request.json(default={})
        settings, error = self.engine.update_settings(payload)
        await self.sync_schedule()
        return json_response({"ok": not error, "error": error, "settings": settings})

    async def api_purge(self):
        payload = await request.json(default={})
        plugin_id = str(payload.get("plugin_id") or "").strip()
        if not plugin_id:
            return error_response("缺少 plugin_id", status_code=400)
        result = await self.engine.purge_plugin_modules(plugin_id)
        if not result.get("ok"):
            return error_response(str(result.get("error") or "清理失败"), status_code=400)
        return json_response(result)

    # ---------- 聊天入口 ----------

    def _results(self, event: AstrMessageEvent, text: str) -> list[Any]:
        return [event.plain_result(chunk) for chunk in chunk_text(text)]

    @staticmethod
    def _private_only(event: AstrMessageEvent) -> None:
        if not event.is_private_chat():
            raise ValueError("缓存信息包含本机路径，请在私聊中执行")

    @staticmethod
    def _confirm(value: str) -> None:
        if str(value or "").strip().lower() != "confirm":
            raise ValueError("请在末尾输入 confirm 确认清理")

    async def _overview_text(self) -> str:
        return format_overview(await self.engine.collect())

    @admin_command("orbit")
    async def orbit(self, event: AstrMessageEvent, action: str = "", plugin_id: str = ""):
        """四层缓存速览。/orbit clean 立即全清，/orbit on|off 开关自动，/orbit purge <插件ID> 清模块。"""
        verb = str(action or "").strip().lower()
        try:
            self._private_only(event)
            if verb in ("", "status"):
                text = await self._overview_text() + "\n\n" + _HELP
            elif verb == "clean":
                result = await self.engine.sweep(force=True)
                text = format_sweep_records(result["records"], force=True)
            elif verb in ("on", "off"):
                _, error = self.engine.update_settings({"auto_enabled": verb == "on"})
                await self.sync_schedule()
                state = "已开启" if self.engine.auto_enabled else "已暂停"
                text = f"自动清理{state}。{error or ''}".strip()
            elif verb == "purge":
                if not plugin_id:
                    raise ValueError("请给出插件 ID，例如 /orbit purge astrbot_plugin_xxx")
                result = await self.engine.purge_plugin_modules(plugin_id)
                if not result.get("ok"):
                    raise ValueError(str(result.get("error") or "清理失败"))
                text = result["record"]["detail"] or result["record"]["skipped"]
            else:
                text = "可用子命令：status / clean / on / off / purge\n\n" + _HELP
        except ValueError as exc:
            for item in self._results(event, str(exc)):
                yield item
            return
        for item in self._results(event, text):
            yield item

    # ---------- 旧指令，保留为别名 ----------

    async def _legacy(
        self,
        event: AstrMessageEvent,
        body: Any,
    ) -> list[Any]:
        results: list[Any] = []
        for chunk in chunk_text(str(body)):
            results.append(event.plain_result(chunk))
        results.append(event.plain_result(DEPRECATION_HINT))
        return results

    @admin_command("orbit_status")
    async def orbit_status(self, event: AstrMessageEvent):
        """查看四层缓存状态（已并入 /orbit status）。"""
        try:
            self._private_only(event)
            text = await self._overview_text()
        except ValueError as exc:
            text = f"无法读取缓存状态：{exc}"
        for item in await self._legacy(event, text):
            yield item

    @admin_command("orbit_astrbot_cache_clean")
    async def orbit_astrbot_cache_clean(self, event: AstrMessageEvent, confirm: str):
        """强制清理 AstrBot 磁盘缓存（已并入 /orbit clean）。"""
        result = await self._legacy_clean(event, confirm, ["astrbot"], force=True)
        for item in result:
            yield item

    @admin_command("orbit_napcat_cache_clean")
    async def orbit_napcat_cache_clean(self, event: AstrMessageEvent, confirm: str):
        """强制调用 NapCat clean_cache（已并入 /orbit clean）。"""
        result = await self._legacy_clean(event, confirm, ["napcat_protocol"], force=True)
        for item in result:
            yield item

    @admin_command("orbit_napcat_fs_status")
    async def orbit_napcat_fs_status(self, event: AstrMessageEvent):
        """查看 NapCat 文件缓存目录（已并入 /orbit status）。"""
        try:
            self._private_only(event)
            overview = await self.engine.collect()
            text = format_directory_rows(overview["layers"]["napcat_files"]["directories"])
        except ValueError as exc:
            text = f"无法读取 NapCat 文件缓存：{exc}"
        for item in await self._legacy(event, text):
            yield item

    @admin_command("orbit_napcat_fs_clean")
    async def orbit_napcat_fs_clean(self, event: AstrMessageEvent, confirm: str):
        """强制清理 NapCat 文件缓存（已并入 /orbit clean）。"""
        result = await self._legacy_clean(event, confirm, ["napcat_files"], force=True)
        for item in result:
            yield item

    @admin_command("orbit_plugin_cache_status")
    async def orbit_plugin_cache_status(self, event: AstrMessageEvent):
        """查看插件模块占用（已并入 /orbit status）。"""
        try:
            self._private_only(event)
            text = format_module_rows(self.module_cache.describe())
        except ValueError as exc:
            text = str(exc)
        for item in await self._legacy(event, text):
            yield item

    @admin_command("orbit_plugin_cache_clean")
    async def orbit_plugin_cache_clean(
        self, event: AstrMessageEvent, plugin_id: str, confirm: str
    ):
        """清理一个插件的内存模块（已并入 /orbit purge）。"""
        try:
            self._private_only(event)
            self._confirm(confirm)
            outcome = await self.engine.purge_plugin_modules(plugin_id)
            if not outcome.get("ok"):
                raise ValueError(str(outcome.get("error") or "清理失败"))
            text = outcome["record"]["detail"] or outcome["record"]["skipped"]
        except (ValueError, ModuleCacheError) as exc:
            text = f"插件模块缓存未清理：{exc}"
        for item in await self._legacy(event, text):
            yield item

    @admin_command("orbit_clean_all")
    async def orbit_clean_all(self, event: AstrMessageEvent, confirm: str):
        """强制清理全部四层（已并入 /orbit clean）。"""
        result = await self._legacy_clean(event, confirm, None, force=True)
        for item in result:
            yield item

    async def _legacy_clean(
        self,
        event: AstrMessageEvent,
        confirm: str,
        layers: list[str] | None,
        *,
        force: bool,
    ) -> list[Any]:
        try:
            self._private_only(event)
            self._confirm(confirm)
            outcome = await self.engine.sweep(force=force, layers=layers)
            text = format_sweep_records(outcome["records"], force=force)
        except ValueError as exc:
            text = str(exc)
        return await self._legacy(event, text)
