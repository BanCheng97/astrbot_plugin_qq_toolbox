"""大肥鱼合集插件入口。

把原来的四个独立插件（告状 / 用量 / 语音 / QQ 工具箱）合并成一个插件：

- 只有一个入口类 ``DafeiyuSuitePlugin``，业务分别放在四个 Mixin 里；
- 公共能力（配置查找、数据目录、JSON、OneBot 客户端）收在 ``modules/base.py``；
- 各模块的方法名都加了前缀，避免多继承时静默覆盖；
- 配置按 ``complain / usage / voice / toolbox`` 四个命名空间隔离；
- 自带 Plugin Views 页面（``views/suite/index.html``）和对应的 Web API。

AstrBot 只给「main 模块路径」下的 Handler 绑定实例、并参与事件分发，
所以这里额外做了两件事：
1. ``_rehome_handlers`` 把子模块里的 Handler 重新挂到 main 模块路径；
2. ``_migrate_legacy_config`` 把旧四个插件的配置迁移进新命名空间。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import platform
import re
import sys
import tempfile
import time
from typing import Any

from astrbot import logger
from astrbot.api.star import Context, Star

try:  # AstrBot 4.x 推荐写法
    from astrbot.api.web import error_response, json_response, request
except Exception:  # pragma: no cover - 仅用于老版本 / 本地单测
    def json_response(data: Any, status_code: int = 200) -> Any:
        return data

    def error_response(message: str, status_code: int = 400) -> Any:
        return {"status": "error", "message": message, "code": status_code}

    class _FallbackRequest:
        async def json(self, default: Any = None) -> Any:
            return {} if default is None else default

        async def files(self) -> dict:
            return {}

        async def form(self) -> dict:
            return {}

        class _Query:
            def get(self, key: str, default: Any = None, type: Any = None) -> Any:
                return default

        query = _Query()

    request = _FallbackRequest()  # type: ignore[assignment]

from .modules import ComplainMixin, MemoryMixin, ToolboxMixin, UsageMixin, VoiceMixin
from .modules.base import MISSING, flatten_dict, nested_get, nested_set, to_bool, to_float, to_int

PLUGIN_NAME = "astrbot_plugin_dafeiyu_toolbox"
ALIAS_NAMES = ("astrbot_plugin_dafeiyu_suite", "astrbot_plugin_qq_toolbox")
SUITE_VERSION = "2.2.3"

PLUGIN_ROOT = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(PLUGIN_ROOT, "_conf_schema.json")

# 旧插件目录名 -> 新配置命名空间
LEGACY_MODULES = {
    "complain": "astrbot_plugin_dafeiyu_complain",
    "usage": "astrbot_plugin_dafeiyu_usage",
    "voice": "astrbot_plugin_dafeiyu_voice",
    "toolbox": "astrbot_plugin_qq_toolbox",
}

_SCHEMA_CACHE: dict[str, Any] | None = None


def _has_module(name: str) -> bool:
    """模块是否可导入（不真的 import，省得触发二进制加载）。"""
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _load_schema() -> dict[str, Any]:
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        try:
            with open(SCHEMA_PATH, "r", encoding="utf-8") as file:
                _SCHEMA_CACHE = json.load(file)
        except Exception as exc:
            logger.warning(f"[suite] 读取配置 schema 失败: {exc}")
            _SCHEMA_CACHE = {}
    return _SCHEMA_CACHE


def _items_of(node: Any) -> dict[str, Any]:
    """取 schema 节点的子项。

    配置 schema 的顶层直接就是模块名（没有 "items"），往下每一层才是
    ``{"type": "object", "items": {...}}``，这里统一抹平两种形态。
    """
    if not isinstance(node, dict):
        return {}
    items = node.get("items")
    if isinstance(items, dict):
        return items
    return {key: value for key, value in node.items() if isinstance(value, dict)}


def _iter_leaves(node: Any, path: tuple[str, ...] = ()):
    """遍历 schema，产出（路径, 叶子配置）二元组。"""
    for key, spec in _items_of(node).items():
        next_path = path + (key,)
        if spec.get("type") == "object":
            yield from _iter_leaves(spec, next_path)
        else:
            yield next_path, spec


def _defaults_of(node: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, spec in _items_of(node).items():
        if spec.get("type") == "object":
            out[key] = _defaults_of(spec)
        else:
            out[key] = spec.get("default")
    return out


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _spec_at(path: list[str]) -> dict[str, Any] | None:
    node: Any = _load_schema()
    for part in path:
        items = _items_of(node)
        if part not in items:
            return None
        node = items[part]
    if isinstance(node, dict) and node.get("type") != "object":
        return node
    return None


def _coerce(value: Any, kind: str | None, default: Any = None) -> Any:
    if kind == "bool":
        return to_bool(value, bool(default))
    if kind == "int":
        return to_int(value, to_int(default, 0))
    if kind == "float":
        return to_float(value, to_float(default, 0.0))
    if kind == "list":
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]
        return [] if value is None else [value]
    if value is None:
        return "" if default is None else default
    return str(value)


def _secret_paths() -> list[tuple[str, ...]]:
    return [path for path, spec in _iter_leaves(_load_schema()) if spec.get("secret")]


class ViewEvent:
    """给 Web UI 的「伪事件」：让内部方法以为自己在处理一条管理员消息。"""

    def __init__(self, bot: Any = None, self_id: str = "", sender_id: str = "",
                 sender_name: str = "主人", group_id: str = "") -> None:
        self.bot = bot
        self.self_id = self_id
        self.sender_id = sender_id
        self.sender_name = sender_name
        self.group_id = group_id
        self.unified_msg_origin = ""
        self.message_str = ""
        self.message_obj = None

    def get_self_id(self) -> str:
        return self.self_id

    def get_sender_id(self) -> str:
        return self.sender_id

    def get_sender_name(self) -> str:
        return self.sender_name

    def get_group_id(self) -> str:
        return self.group_id

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def is_admin(self) -> bool:
        return True

    def get_messages(self) -> list:
        return []

    def get_result(self) -> Any:
        return None

    def plain_result(self, text: str) -> Any:
        return text


class ViewBot:
    """把平台客户端包一层，同时兼容 call_action 和 send_xxx_msg 两种调用。"""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.api = getattr(client, "api", None) or client
        self.self_id = str(getattr(client, "self_id", "") or "")

    async def call_action(self, action: str, **params: Any) -> Any:
        call = getattr(self._client, "call_action", None) or getattr(self.api, "call_action", None)
        if call is None:
            raise RuntimeError("当前适配器不支持 call_action")
        return await call(action, **params)

    async def send_group_msg(self, **params: Any) -> Any:
        return await self.call_action("send_group_msg", **params)

    async def send_private_msg(self, **params: Any) -> Any:
        return await self.call_action("send_private_msg", **params)


class DafeiyuSuitePlugin(
    ComplainMixin,
    UsageMixin,
    VoiceMixin,
    ToolboxMixin,
    MemoryMixin,
    Star,
):
    def __init__(self, context: Context | None = None, config: Any = None):
        try:
            super().__init__(context)  # type: ignore[arg-type]
        except TypeError:
            Star.__init__(self, context)  # type: ignore[arg-type]
        self.context = context
        if config is None:
            config = getattr(self, "config", None)
        self.config = config if isinstance(config, dict) else {}

        self.setup_complain()
        self.setup_usage()
        self.setup_voice()
        self.setup_toolbox()
        self.setup_memory()

        self._migrate_legacy_config()
        self._normalize_percent_config()
        self._register_web_apis()
        self._rehome_handlers()

    # ==================== 生命周期 ====================

    async def initialize(self) -> None:
        await self._usage_start()

    async def terminate(self) -> None:
        try:
            await self._usage_stop()
        except Exception as exc:
            logger.warning(f"[suite] 停止用量定时任务失败: {exc}")
        try:
            await self._toolbox_terminate()
        except Exception as exc:
            logger.warning(f"[suite] 释放空间 HTTP 连接失败: {exc}")

    # ==================== Handler 归位 ====================

    def _rehome_handlers(self) -> None:
        """把子模块里的 Handler / LLM 工具挂到 main 模块路径。

        AstrBot 只按 ``metadata.module_path``（main.py）绑定实例、过滤事件；
        子模块注册出来的 handler 不归位就不会生效。
        """
        main_path = __name__
        # 子模块与 main.py 是同级目录，模块路径形如 <pkg>.modules.complain；
        # 如果 AstrBot 直接用顶层名加载 main.py，则子模块路径形如 modules.complain。
        prefix = (
            __name__.rsplit(".", 1)[0] + ".modules"
            if "." in __name__
            else "modules"
        )
        try:
            from astrbot.core.star.star_handler import star_handlers_registry
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[suite] 无法导入 Handler 注册表: {exc}")
            star_handlers_registry = None

        if star_handlers_registry is not None:
            rehomed = 0
            for handler in list(star_handlers_registry):
                path = getattr(handler, "handler_module_path", "") or ""
                if path == main_path or not (path == prefix or path.startswith(prefix + ".")):
                    continue
                try:
                    star_handlers_registry.remove(handler)
                except Exception:
                    pass
                handler.handler_module_path = main_path
                handler.handler_full_name = f"{main_path}_{handler.handler_name}"
                func = getattr(handler, "handler", None)
                if callable(func):
                    try:
                        func.__module__ = main_path
                    except Exception:
                        pass
                try:
                    star_handlers_registry.append(handler)
                except Exception as exc:
                    logger.warning(f"[suite] 重新注册 Handler {handler.handler_name} 失败: {exc}")
                    continue
                rehomed += 1
            if rehomed:
                logger.info(f"[suite] 已把 {rehomed} 个 Handler 归位到 {main_path}")

        try:
            from astrbot.core.provider.register import llm_tools
        except Exception:  # pragma: no cover
            llm_tools = None
        if llm_tools is not None:
            for tool in getattr(llm_tools, "func_list", []) or []:
                path = getattr(tool, "handler_module_path", "") or ""
                if not (path == prefix or path.startswith(prefix + ".")):
                    continue
                tool.handler_module_path = main_path
                func = getattr(tool, "handler", None)
                if callable(func):
                    try:
                        func.__module__ = main_path
                    except Exception:
                        pass

    # ==================== 配置迁移 ====================

    @staticmethod
    def _config_dir() -> str:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_config_path

            return str(get_astrbot_config_path())
        except Exception:
            return os.path.join(os.getcwd(), "data", "config")

    def _migrate_legacy_config(self) -> None:
        """把旧四个插件的配置填进新命名空间。

        只在「新配置里还是默认值」时才覆盖，尽量不打乱用户已经在新配置里改过的项。
        """
        schema = _load_schema()
        if not schema:
            return
        config_dir = self._config_dir()
        changed = 0
        for module, legacy_name in LEGACY_MODULES.items():
            legacy_path = os.path.join(config_dir, f"{legacy_name}_config.json")
            try:
                with open(legacy_path, "r", encoding="utf-8") as file:
                    legacy = json.load(file)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(legacy, dict):
                continue
            flat = flatten_dict(legacy)
            module_spec = schema.get(module)
            if not isinstance(module_spec, dict):
                continue
            current = self.config.get(module)
            if not isinstance(current, dict):
                current = _defaults_of(module_spec)
                self.config[module] = current
            for path, spec in _iter_leaves(module_spec, (module,)):
                key = path[-1]
                if key not in flat:
                    continue
                old_value = flat[key]
                # 说说提醒概率在旧版是 0-1 小数，新版统一为 0-100 百分比
                if (
                    path[-3:] == ("toolbox", "feed", "feed_hint_chance")
                    and isinstance(old_value, (int, float))
                    and not isinstance(old_value, bool)
                    and 0 < float(old_value) <= 1
                ):
                    old_value = round(float(old_value) * 100)
                existing = nested_get(current, list(path[1:]))
                default = spec.get("default")
                if existing is MISSING or existing == default:
                    if existing == old_value:
                        continue
                    value = _coerce(old_value, spec.get("type"), default)
                    nested_set(current, list(path[1:]), value)
                    changed += 1
        if changed:
            self.save_config()
            logger.info(f"[suite] 已从旧插件配置迁移 {changed} 项设置")

    def _normalize_percent_config(self) -> None:
        """把历史版本按 0-1 小数保存的概率值换算成百分比整数。"""
        try:
            value = nested_get(self.config, ["toolbox", "feed", "feed_hint_chance"])
        except Exception:
            return
        if isinstance(value, bool) or not isinstance(value, float):
            return
        if not 0 <= value <= 1:
            return
        percent = int(round(value * 100))
        nested_set(self.config, ["toolbox", "feed", "feed_hint_chance"], percent)
        self.save_config()
        logger.info(f"[suite] 说说提醒概率已从 {value} 换算为 {percent}%")

    # ==================== Web API 注册 ====================

    def _register_web_apis(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.warning("[suite] 当前 AstrBot 不支持 register_web_api，可视化视图不可用")
            return
        apis = [
            ("status", self.page_status, ["GET"], "合集运行状态"),
            ("config", self.page_get_config, ["GET"], "读取合集配置"),
            ("config/save", self.page_save_config, ["POST"], "保存合集配置"),
            ("action", self.page_action, ["POST"], "触发模块功能"),
            ("voice/stt", self.page_voice_stt, ["POST"], "上传语音并转文字"),
            ("records", self.page_records, ["GET"], "查看告状与操作记录"),
            ("memory", self.page_memory, ["GET"], "查看长期记忆"),
            ("memory/add", self.page_memory_add, ["POST"], "手动添加一条记忆"),
            ("memory/delete", self.page_memory_delete, ["POST"], "删除一条记忆"),
            ("memory/clear", self.page_memory_clear, ["POST"], "清空记忆"),
            ("memory/import", self.page_memory_import, ["POST"], "导入记忆"),
        ]
        for name in (PLUGIN_NAME, *ALIAS_NAMES):
            for route, handler, methods, desc in apis:
                try:
                    register(f"/{name}/{route}", handler, methods, desc)
                except Exception as exc:
                    logger.warning(f"[suite] 注册 Web API {route} 失败: {exc}")

    # ==================== 公共小工具 ====================

    def _view_event(self, params: dict[str, Any]) -> ViewEvent:
        raw_client = self.platform_client()
        bot = ViewBot(raw_client) if raw_client is not None else None
        self_id = str(params.get("self_id") or "").strip()
        if not self_id and bot is not None:
            self_id = bot.self_id
        sender = str(params.get("sender_id") or "").strip()
        if not sender:
            # WebUI 里没有真实发送者，用一个不会匹配任何家人的占位值，
            # 避免告状路由把自己当成欺负者而无处可发。
            sender = self_id if self_id.isdigit() else "0"
        if not sender:
            sender = "0"
        return ViewEvent(
            bot=bot,
            self_id=self_id,
            sender_id=sender,
            sender_name=str(params.get("sender_name") or "主人"),
            group_id=str(params.get("group_id") or ""),
        )

    def _handler_stats(self) -> dict[str, int]:
        try:
            from astrbot.core.star.star_handler import star_handlers_registry

            handlers = list(star_handlers_registry)
        except Exception:
            handlers = []
        main_path = __name__
        mine = [h for h in handlers if getattr(h, "handler_module_path", "") == main_path]
        return {"total": len(mine), "all": len(handlers)}

    def _tool_stats(self) -> dict[str, int]:
        try:
            from astrbot.core.provider.register import llm_tools

            tools = list(getattr(llm_tools, "func_list", []) or [])
        except Exception:
            tools = []
        mine = [t for t in tools if getattr(t, "handler_module_path", "") == __name__]
        return {"total": len(mine)}

    # ==================== Web API: 状态 ====================

    async def page_status(self):
        records = self._complain_load()
        today = time.strftime("%Y-%m-%d")
        usage_state = self._usage_load()
        snapshots = [s for s in (usage_state.get("snapshots") or []) if isinstance(s, dict)]
        trust = [h for h in self._toolbox_load() if h.get("date") == today]

        toolbox_counts = {}
        for kind in ("feed", "like", "comment", "delete", "card_like", "nickname", "signature"):
            toolbox_counts[kind] = self._toolbox_count_today(kind)

        usage_info: dict[str, Any] = {}
        if snapshots:
            last = snapshots[-1]
            usage_info = {
                "total": last.get("total"),
                "date": last.get("date"),
                "samples": len(snapshots),
            }

        data = {
            "plugin": {
                "name": PLUGIN_NAME,
                "version": SUITE_VERSION,
                "handlers": self._handler_stats(),
                "llm_tools": self._tool_stats(),
                "platforms": [p for p in self._platform_names()],
            },
            "complain": {
                "enabled": bool(self._complain_cfg("enabled", True)),
                "relay_enabled": bool(self._complain_cfg("relay_enabled", True)),
                "owner": self._owner_target() or "",
                "owner_configured": bool(self._owner_target()),
                "guardians": len(self._guardians()),
                "today": self._complain_count_today(records, "complain"),
                "today_relay": self._complain_count_today(records, "relay"),
                "records": len(records),
                "cooldown": max(0, to_int(self._complain_cfg("cooldown_seconds", 600), 600)),
                "daily_limit": max(0, to_int(self._complain_cfg("daily_limit", 5), 5)),
            },
            "usage": {
                "enabled": bool(self._usage_cfg("enabled", True)),
                "api_key_configured": bool(self._api_key()),
                "alert_enabled": bool(self._usage_cfg("alert_enabled", True)),
                "alert_threshold": to_float(self._usage_cfg("alert_threshold", 5), 5.0),
                "check_interval_hours": to_int(self._usage_cfg("check_interval_hours", 6), 6),
                "task_running": bool(self._task and not self._task.done()),
                "last_snapshot": usage_info,
                "last_alert": usage_state.get("alert") or {},
            },
            "voice": {
                "listen_enabled": bool(self._voice_cfg("listen_enabled", True)),
                "speak_enabled": bool(self._voice_cfg("speak_enabled", True)),
                "sing_enabled": bool(self._voice_cfg("sing_enabled", True)),
                "backend": str(self._voice_cfg("tts_backend", "breeze") or "breeze"),
                "breeze_key_configured": bool(self._voice_cfg("breeze_api_key", "")),
                "mimo_key_configured": bool(
                    self._voice_cfg("mimo_tts_api_key", "")
                    or self._voice_cfg("mimo_stt_api_key", "")
                    or self._voice_cfg("breeze_api_key", "")
                ),
                "breeze_voice_id": str(self._voice_cfg("breeze_voice_id", "") or ""),
                "speak_prob_percent": to_int(self._voice_cfg("speak_prob_percent", 10), 10),
            },
            "toolbox": {
                "admin_only": bool(self._toolbox_cfg("admin_only", True)),
                "cached_sessions": len(self._toolbox_sess_cache),
                "cooldown_remaining": self._cooldown_remaining(),
                "today": toolbox_counts,
                "history": len(self._toolbox_load()),
                "domain": str(self._toolbox_cfg("qzone_domain", "user.qzone.qq.com") or ""),
            },
            "memory": self._memory_stats(),
            "runtime": self._runtime_info(),
        }
        return json_response(data)

    def _runtime_info(self) -> dict[str, Any]:
        """运行环境自检：在安卓 / Linux / Windows 上都能看出缺什么。"""
        return {
            "os": sys.platform,
            "python": platform.python_version(),
            "silk": _has_module("pysilk"),
            "audioop": _has_module("audioop") or _has_module("audioop_lts"),
            "onebot": bool(self.platform_client()),
            "data_dir": os.path.dirname(self._memory_path),
        }

    def _platform_names(self) -> list[str]:
        names: list[str] = []
        try:
            for platform in self.context.platform_manager.platform_insts:
                name = getattr(platform, "name", "") or getattr(platform, "platform_name", "")
                if name:
                    names.append(str(name))
        except Exception:
            pass
        return names

    # ==================== Web API: 配置 ====================

    async def page_get_config(self):
        schema = _load_schema()
        values = _defaults_of(schema)
        for module in values:
            actual = self.config.get(module)
            if isinstance(actual, dict):
                values[module] = _deep_merge(values[module], actual)
        configured: dict[str, bool] = {}
        for path in _secret_paths():
            dotted = ".".join(path)
            value = nested_get(values, list(path))
            configured[dotted] = bool(value) if value is not MISSING else False
            if value is not MISSING:
                nested_set(values, list(path), "")
        return json_response(
            {
                "schema": schema,
                "values": values,
                "secrets": configured,
                "modules": ["complain", "usage", "voice", "toolbox", "memory"],
            }
        )

    async def page_save_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        raw_values = payload.get("values")
        if not isinstance(raw_values, dict):
            return error_response("缺少 values 字段")

        changed = 0
        skipped: list[str] = []
        for dotted, value in raw_values.items():
            path = str(dotted).split(".")
            if len(path) < 2:
                skipped.append(dotted)
                continue
            spec = _spec_at(path)
            if spec is None:
                skipped.append(dotted)
                continue
            if spec.get("secret") and (value is None or str(value).strip() == ""):
                continue
            coerced = _coerce(value, spec.get("type"), spec.get("default"))
            nested_set(self.config, path, coerced)
            changed += 1
        if changed:
            self.save_config()
        return json_response({"changed": changed, "skipped": skipped})

    # ==================== Web API: 动作分发 ====================

    async def page_action(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        module = str(payload.get("module") or "")
        action = str(payload.get("action") or "")
        params = payload.get("params")
        if not isinstance(params, dict):
            params = {}
        if module not in LEGACY_MODULES:
            return error_response(f"未知模块：{module}")
        if not action:
            return error_response("缺少 action")
        event = self._view_event(params)
        try:
            result = await self._dispatch(module, action, params, event)
        except Exception as exc:
            logger.error(f"[suite] 视图动作 {module}.{action} 失败: {exc!r}")
            return error_response(f"{module}.{action} 执行失败：{exc}", status_code=500)
        if isinstance(result, dict) and result.get("__error__"):
            return error_response(str(result.get("message") or "执行失败"), status_code=400)
        return json_response(result)

    async def _dispatch(self, module: str, action: str, params: dict, event: ViewEvent) -> dict:
        if module == "complain":
            return await self._action_complain(action, params, event)
        if module == "usage":
            return await self._action_usage(action, params)
        if module == "voice":
            return await self._action_voice(action, params)
        return await self._action_toolbox(action, params, event)

    # ---------- 告状 ----------

    async def _action_complain(self, action: str, params: dict, event: ViewEvent) -> dict:
        if action == "preview":
            to_title, owner = self._route(event)
            wanted = str(params.get("to") or "").strip()
            if wanted:
                found = self._resolve_target(wanted)
                if found:
                    to_title, owner = found
            if not owner:
                return {"__error__": True, "message": "还没配置告状对象（owner_umo / owner_qq / guardians）"}
            name, _ = self._speaker(event, str(params.get("who") or ""))
            text = self._compose(to_title, name, str(params.get("bully") or ""), str(params.get("words") or ""))
            max_len = to_int(self._complain_cfg("max_length", 80), 80)
            if len(text) > max_len:
                text = text[: max_len - 1] + "…"
            return {"to_title": to_title, "target": owner, "text": text}
        if action == "complain":
            if event.bot is None:
                return {"__error__": True, "message": "当前没有可用的 aiocqhttp 客户端，无法发送"}
            message = await self.tool_complain(
                event,
                bully=str(params.get("bully") or ""),
                words=str(params.get("words") or ""),
                who=str(params.get("who") or ""),
            )
            return {"message": message}
        if action == "relay":
            if event.bot is None:
                return {"__error__": True, "message": "当前没有可用的 aiocqhttp 客户端，无法发送"}
            message = await self.tool_relay(
                event,
                to=str(params.get("to") or ""),
                content=str(params.get("content") or ""),
                words=str(params.get("words") or ""),
            )
            return {"message": message}
        if action == "records":
            records = self._complain_load()[-20:]
            return {"items": list(reversed(records))}
        return {"__error__": True, "message": f"告状模块不支持动作 {action}"}

    # ---------- 用量 ----------

    async def _action_usage(self, action: str, params: dict) -> dict:
        if action == "query":
            if not self._usage_cfg("enabled", True):
                return {"__error__": True, "message": "用量功能已关闭"}
            if not self._api_key():
                return {"__error__": True, "message": "还没配置 DeepSeek API Key"}
            force = bool(params.get("force", True))
            info = await self._balance(force=force)
            report = self._report(info)
            return {"info": info, "report": report, "text": self._format(info, report)}
        if action == "check_alert":
            if not self._usage_cfg("enabled", True) or not self._usage_cfg("alert_enabled", True):
                return {"__error__": True, "message": "用量或低余额提醒已关闭"}
            if not self._api_key():
                return {"__error__": True, "message": "还没配置 DeepSeek API Key"}
            await self._check_alert()
            state = self._usage_load()
            return {"alert": state.get("alert") or {}, "message": "已执行一次低余额检查"}
        return {"__error__": True, "message": f"用量模块不支持动作 {action}"}

    # ---------- 语音 ----------

    async def _action_voice(self, action: str, params: dict) -> dict:
        if action not in ("speak", "sing"):
            return {"__error__": True, "message": f"语音模块不支持动作 {action}"}
        if not self._voice_cfg("speak_enabled", True):
            return {"__error__": True, "message": "语音合成已关闭"}
        text = str(params.get("text") or "").strip()
        if not text:
            return {"__error__": True, "message": "请填写要合成的文本"}
        if action == "sing" and not self._voice_cfg("sing_enabled", True):
            return {"__error__": True, "message": "歌唱模式已关闭"}
        style = str(params.get("style") or "").strip()
        if action == "sing":
            # 切句与停顿交给 _synthesize / _format_lyrics，这里不要再手工塞省略号
            text = text[:400]
            emotion = "sing"
        else:
            emotion = str(params.get("emotion") or "").strip() or self._detect_emotion(text)
            text = text[:300]
        path = await self._synthesize(text, emotion, style)
        try:
            with open(path, "rb") as file:
                raw = file.read()
            suffix = os.path.splitext(path)[1].lower()
            if raw[:4] == b"RIFF" or suffix == ".wav":
                mime = "audio/wav"
            elif suffix in (".mp3", ".mpeg") or raw[:3] == b"ID3":
                mime = "audio/mpeg"
            else:
                mime = "audio/wav"
            return {
                "audio": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}",
                "mime": mime,
                "emotion": emotion,
                "style": style,
                "chars": len(text),
            }
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

    async def page_voice_stt(self):
        files = await request.files()
        upload = files.get("file") if isinstance(files, dict) else None
        if upload is None:
            return error_response("缺少上传文件（字段名必须为 file）")
        filename = getattr(upload, "filename", "") or "voice.tmp"
        suffix = os.path.splitext(filename)[1] or ".mp3"
        handle = tempfile.NamedTemporaryFile(prefix="suite_stt_", suffix=suffix, delete=False)
        handle.close()
        path = handle.name
        try:
            save = getattr(upload, "save", None)
            if callable(save):
                await save(path)
            else:  # pragma: no cover
                raw = await upload.read() if hasattr(upload, "read") else b""
                with open(path, "wb") as file:
                    file.write(raw)
            text = await self._transcribe(path)
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass
        if not text:
            return error_response("语音识别失败：请检查 MiMo API Key 与音频格式（wav/mp3）", status_code=502)
        return json_response({"text": text, "chars": len(text)})

    # ---------- QQ 工具箱 ----------

    async def _action_toolbox(self, action: str, params: dict, event: ViewEvent) -> dict:
        if action == "test_connection":
            sess = await self._get_session(event, force=True)
            return {
                "uin": sess.uin,
                "has_skey": bool(sess.p_skey or sess.skey),
                "domain": str(self._toolbox_cfg("qzone_domain", "user.qzone.qq.com") or ""),
                "message": f"已连上空间，uin={sess.uin}",
            }
        if action == "list":
            count = max(1, min(to_int(params.get("count"), 5), 20))
            sess = await self._get_session(event)
            items = await self._msg_list(sess, count, event)
            self._toolbox_last_list[sess.uin] = items
            data = []
            for index, item in enumerate(items, 1):
                comments = [c for c in (item.get("commentlist") or []) if isinstance(c, dict)]
                data.append(
                    {
                        "index": index,
                        "tid": str(item.get("tid") or ""),
                        "content": str(item.get("content") or ""),
                        "likes": item.get("likenum") or 0,
                        "comments": len(comments),
                        "created_time": item.get("created_time"),
                    }
                )
            return {"items": data}
        if action == "ai_preview":
            text, err = await self._generate_post(event, str(params.get("topic") or ""))
            if err:
                return {"__error__": True, "message": err}
            return {"text": text}
        if action == "avatar":
            source = str(params.get("image") or "").strip()
            if not source:
                return {"__error__": True, "message": "请填写头像图片地址（URL / base64:// / 本地路径）"}
            await self._call(event, "set_qq_avatar", file=source)
            return {"message": "头像已更新"}

        handlers = {
            "nickname": lambda: self.tool_set_nickname(event, str(params.get("nickname") or "")),
            "signature": lambda: self.tool_set_signature(
                event,
                str(params.get("signature") or ""),
                owner_asked=bool(params.get("owner_asked", True)),
                reason=str(params.get("reason") or "WebUI 手动修改"),
            ),
            "publish": lambda: self.tool_post_feed(
                event,
                str(params.get("content") or ""),
                str(params.get("images") or ""),
            ),
            "ai_post": lambda: self.tool_ai_post_feed(event, str(params.get("topic") or "")),
            "like": lambda: self.tool_like_feed(event, str(params.get("target") or "1")),
            "comment": lambda: self.tool_comment_feed(
                event,
                str(params.get("content") or ""),
                str(params.get("target") or "1"),
            ),
            "reply_comment": lambda: self.tool_reply_comment(
                event,
                str(params.get("feed") or "1"),
                str(params.get("comment") or "1"),
                str(params.get("content") or ""),
            ),
            "delete": lambda: self.tool_delete_feed(event, str(params.get("target") or "1")),
            "like_card": lambda: self.tool_like_card(
                event,
                str(params.get("user_id") or ""),
                str(params.get("times") or "10"),
            ),
        }
        runner = handlers.get(action)
        if runner is None:
            return {"__error__": True, "message": f"工具箱不支持动作 {action}"}
        message = await runner()
        return {"message": message}

    # ==================== Web API: 记录 ====================

    async def page_records(self):
        records = self._complain_load()
        history = self._toolbox_load()
        return json_response(
            {
                "complain": list(reversed(records[-50:])),
                "toolbox": list(reversed(history[-50:])),
            }
        )

    # ==================== Web API: 长期记忆 ====================

    async def page_memory(self):
        return json_response(
            {
                "users": self._memory_all(),
                "stats": self._memory_stats(),
                "path": self._memory_path,
            }
        )

    async def page_memory_add(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        sender = str(payload.get("sender") or "").strip()
        text = str(payload.get("text") or "").strip()
        if not sender:
            return error_response("缺少 sender（要记在谁名下，填 QQ 号）")
        if not text:
            return error_response("缺少 text（要记住的内容）")
        self._memory_add(sender, text, weight=3)
        return json_response({"message": f"已记在 {sender} 名下", "facts": len(self._memory_list(sender))})

    async def page_memory_delete(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        sender = str(payload.get("sender") or "").strip()
        keyword = str(payload.get("keyword") or "").strip()
        if not sender or not keyword:
            return error_response("缺少 sender 或 keyword")
        removed = self._memory_delete(sender, keyword)
        return json_response({"removed": removed, "facts": len(self._memory_list(sender))})

    async def page_memory_clear(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        sender = str(payload.get("sender") or "").strip()
        removed = self._memory_clear(sender or None)
        return json_response({"removed": removed})

    async def page_memory_import(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        raw = payload.get("data", payload)
        # 兼容两种格式：导出文件 {"users": [...]} 与 memory.json {"users": {...}}
        users: dict[str, list[str]] = {}
        if isinstance(raw, dict):
            container = raw
            while isinstance(container.get("data"), dict):
                container = container["data"]
            items = container.get("users")
        else:
            items = None
        if isinstance(items, list):
            for user in items:
                if not isinstance(user, dict):
                    continue
                sender = str(user.get("sender") or user.get("id") or "").strip()
                for fact in user.get("facts") or []:
                    text = fact.get("text") if isinstance(fact, dict) else fact
                    if sender and text:
                        users.setdefault(sender, []).append(str(text))
        elif isinstance(items, dict):
            for sender, user in items.items():
                if not isinstance(user, dict):
                    continue
                for fact in user.get("facts") or []:
                    text = fact.get("text") if isinstance(fact, dict) else fact
                    if str(sender).strip() and text:
                        users.setdefault(str(sender).strip(), []).append(str(text))
        if not users:
            return error_response("没有解析出任何记忆（请选择导出的 JSON 文件）")
        added = 0
        for sender, texts in users.items():
            for text in texts:
                if self._memory_add(sender, text, weight=2):
                    added += 1
        return json_response({
            "added": added,
            "skipped": sum(len(texts) for texts in users.values()) - added,
            "users": len(users),
        })



# AstrBot 兼容：有些旧版本会按类名查找（以 Plugin 结尾）
__all__ = ["DafeiyuSuitePlugin"]
