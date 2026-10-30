"""大肥鱼合集插件：四个模块共用的底座。

把四个原插件里重复的东西收拢到这里：
- 配置查找（按模块命名空间隔离，兼容旧版扁平配置）
- 数据目录 / JSON 读写
- OneBot 客户端获取
- 数值转换

各模块的 Mixin 只保留自己的业务逻辑，方法名统一加模块前缀，避免 MRO 覆盖。
"""

from __future__ import annotations

import json
import os
from typing import Any

from astrbot import logger

PLUGIN_NAME = "astrbot_plugin_dafeiyu_toolbox"

MISSING = object()


def plugin_root() -> str:
    """插件根目录（main.py 所在目录）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def to_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "1", "on", "开"):
            return True
        if low in ("false", "no", "0", "off", "关"):
            return False
    return default


def find_key(node: Any, key: str, max_depth: int = 4) -> Any:
    """在（可能分组的）配置里递归找某个键；找不到返回 MISSING。"""
    if not isinstance(node, dict):
        return MISSING
    if key in node:
        return node[key]
    if max_depth <= 0:
        return MISSING
    for value in node.values():
        if isinstance(value, dict):
            found = find_key(value, key, max_depth - 1)
            if found is not MISSING:
                return found
    return MISSING


def flatten_dict(node: Any, out: dict[str, Any] | None = None) -> dict[str, Any]:
    """把任意层级的分组配置拍平成一层的键值表（同名键后面的覆盖前面的）。"""
    flat: dict[str, Any] = {} if out is None else out
    if not isinstance(node, dict):
        return flat
    for key, value in node.items():
        if isinstance(value, dict):
            flatten_dict(value, flat)
        else:
            flat[key] = value
    return flat


def nested_get(node: Any, path: list[str]) -> Any:
    current = node
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return MISSING
        current = current[part]
    return current


def nested_set(node: dict, path: list[str], value: Any) -> None:
    current = node
    for part in path[:-1]:
        nxt = current.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            current[part] = nxt
        current = nxt
    current[path[-1]] = value


def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return default
    except Exception as exc:
        logger.warning(f"[suite] 读取 {path} 失败: {exc}")
        return default


def save_json(path: str, data: Any) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning(f"[suite] 写入 {path} 失败: {exc}")


def resolve_data_dir(plugin_name: str) -> str:
    """数据目录：优先 AstrBot 的插件数据目录，失败时落到插件目录下的 data/。"""
    try:
        from astrbot.api.star import StarTools

        path = str(StarTools.get_data_dir(plugin_name))
    except Exception as exc:
        logger.warning(f"[suite] 获取 {plugin_name} 数据目录失败，改用本地 data/: {exc}")
        path = os.path.join(plugin_root(), "data", plugin_name)
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


class SuiteBase:
    """四个 Mixin 的公共父类（不注册为插件类）。"""

    config: dict

    # ---------------- 配置 ----------------

    def cfg(self, module: str, key: str, default: Any = None) -> Any:
        """读取某个模块命名空间下的配置键。

        先在自己的模块分组里递归找；分组已经存在但键缺失时直接返回默认值，
        避免串到别的模块的同名键（例如 owner_session / max_length / enabled）。
        只有在完全没有该模块分组（旧版扁平配置）时才回退到全局查找。
        """
        config = getattr(self, "config", None)
        if not isinstance(config, dict):
            return default
        namespace = config.get(module)
        if isinstance(namespace, dict):
            found = find_key(namespace, key)
            return default if found is MISSING else found
        if module in config:
            return default
        found = find_key(config, key)
        return default if found is MISSING else found

    def cfg_group(self, module: str, group: str) -> dict[str, Any]:
        config = getattr(self, "config", None)
        if not isinstance(config, dict):
            return {}
        namespace = config.get(module)
        if not isinstance(namespace, dict):
            return {}
        value = namespace.get(group)
        return value if isinstance(value, dict) else {}

    def save_config(self) -> None:
        save = getattr(self.config, "save_config", None)
        if callable(save):
            try:
                save()
            except Exception as exc:
                logger.warning(f"[suite] 保存配置失败: {exc}")

    # ---------------- 数据文件 ----------------

    def module_data_dir(self, plugin_name: str) -> str:
        return resolve_data_dir(plugin_name)

    # ---------------- OneBot 客户端 ----------------

    @staticmethod
    def onebot_client(event: Any) -> Any:
        """从当前事件上拿 OneBot v11 客户端（NapCat / LLOneBot）。"""
        bot = getattr(event, "bot", None)
        if bot is not None:
            api = getattr(bot, "api", None) or bot
            if hasattr(api, "call_action"):
                return api
        return None

    def platform_client(self) -> Any:
        """从平台管理器里拿第一个支持 call_action 的客户端（无事件时用）。"""
        try:
            for platform in self.context.platform_manager.platform_insts:
                getter = getattr(platform, "get_client", None)
                inst = getter() if callable(getter) else platform
                client = getattr(inst, "api", None) or inst
                if hasattr(client, "call_action"):
                    return client
        except Exception:
            pass
        return None
