"""AstrBot 插件：大肥鱼主动搭话。

记录最近聊过的会话，在合适的时间用当前人格主动发一条消息：

- 只找最近活跃、且已经安静一会儿的会话，不打断正在聊天的人；
- 深夜默认安静，每个会话每天有上限，间隔在配置范围内随机；
- 文案由 AstrBot 当前人格的 system prompt + 当前时间 / 上次聊天间隔 /
  主人称呼 / 长期记忆生成，开口就是她自己的语气；
- 模型不可用时退回配置里的兜底话术，不会硬塞模板。

数据存 ``plugin_data/astrbot_plugin_qq_toolbox/proactive.json``，纯本地 JSON。
所有方法是 ``proactive`` 前缀，避免多继承时和其他模块重名。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain

from .base import SuiteBase, load_json, save_json, to_int

# 沿用旧数据目录，和记忆 / 历史记录放在一起
NAME = "astrbot_plugin_qq_toolbox"

HELP_TEXT = """主动搭话相关指令：
/主动说话 [会话UMO]  马上让她给最近聊过的会话（或指定会话）发一条
/主动帮助  显示这段说明

平时的主动搭话由后台定时触发：只在安静下来的会话里发，
深夜不打扰，每个会话每天有次数上限，可以在插件配置的「主动搭话」里调整。"""

# 不同时间段 / 不同心情下的开场方向
_MODE_HINTS = {
    "morning": "现在是早上，跟对方道个早安，问问今天怎么样",
    "noon": "到饭点了，问问对方吃饭了没",
    "afternoon": "下午有点犯困，跟对方闲聊两句，问问在忙什么",
    "evening": "到晚上了，问问对方今天过得怎么样",
    "night": "夜深了，轻轻道个晚安，提醒早点睡",
    "miss": "没有特别的事，就是突然想起对方了，说一句想念的话",
    "memory": "顺着你还记得的关于对方的一件事自然开口",
}

# 模型接不上时的兜底话术，保证「像她随口说的」，不留模板痕迹
_FALLBACKS = {
    "morning": "早呀，醒了没？",
    "noon": "吃饭了没？别又随便对付一口。",
    "afternoon": "在忙什么呀，我突然有点无聊。",
    "evening": "今天过得怎么样？想听听你的。",
    "night": "该睡啦，别熬夜，晚安。",
    "miss": "在忙吗？突然有点想你了。",
    "memory": "突然想起你跟我说过的事，有点想知道后来怎么样了。",
}


def _gap_text(seconds: float) -> str:
    """把秒数说成人话：几分钟 / 几小时 / 几天。"""
    minutes = max(1, int(seconds // 60))
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小时"
    return f"{hours // 24} 天"


class ProactiveMixin(SuiteBase):
    def setup_proactive(self) -> None:
        self._proactive_path = self.data_file(NAME, "proactive.json")
        self._proactive_state: dict[str, Any] | None = None
        self._proactive_task: asyncio.Task | None = None
        self._proactive_dirty = False

    def _proactive_cfg(self, key: str, default: Any = None) -> Any:
        return self.cfg("proactive", key, default)

    # ---------------- 存取 -----------------------------

    def _proactive_load(self) -> dict[str, Any]:
        if self._proactive_state is not None:
            return self._proactive_state
        raw = load_json(self._proactive_path, {}) if self._proactive_path else {}
        if not isinstance(raw, dict):
            raw = {}
        if not isinstance(raw.get("sessions"), dict):
            raw["sessions"] = {}
        if not isinstance(raw.get("history"), list):
            raw["history"] = []
        self._proactive_state = raw
        return raw

    def _proactive_save(self) -> None:
        self._proactive_dirty = False
        if not self._proactive_path:
            return
        data = self._proactive_load()
        data["updated"] = int(time.time())
        save_json(self._proactive_path, data)

    def _proactive_list(self, key: str) -> list[str]:
        raw = self._proactive_cfg(key, [])
        if isinstance(raw, list):
            return [str(item).strip() for item in raw if str(item).strip()]
        if isinstance(raw, str):
            return [item.strip() for item in re.split(r"[\s,，;]+", raw) if item.strip()]
        return []

    @staticmethod
    def _proactive_peer(umo: str) -> tuple[str, str]:
        """从 UMO 拆出（消息类型, 会话 id）。"""
        parts = str(umo or "").split(":")
        if len(parts) >= 3:
            return parts[-2], parts[-1]
        return "", ""

    def _proactive_enrich(self, umo: str, info: dict[str, Any]) -> dict[str, Any]:
        """生成文案前按 UMO 重新确认「对方是谁、该怎么称呼」。

        会话记录里的说话人 / 称呼是聊天当时的快照，之后改认主、改称呼、
        或固定发送对象还没聊过，都可能过期。私聊直接用 UMO 里的 QQ，
        称呼每次现查，避免把妈叫成爹。
        """
        view = dict(info or {})
        peer_type, peer_id = self._proactive_peer(umo)
        sender = str(view.get("sender") or "")
        if peer_type in ("FriendMessage", "TempMessage") and peer_id.isdigit():
            sender = peer_id
        if not sender:
            return view
        view["sender"] = sender
        title_getter = getattr(self, "_master_title_for", None)
        if callable(title_getter):
            try:
                view["master_title"] = str(title_getter(sender) or "")
            except Exception:
                view["master_title"] = ""
        if not view.get("sender_name"):
            finder = getattr(self, "_master_find", None)
            if callable(finder):
                try:
                    item = finder(sender)
                except Exception:
                    item = None
                if isinstance(item, dict) and item.get("name"):
                    view["sender_name"] = str(item["name"])
        return view

    # ---------------- 时间与状态 -----------------------------

    def _proactive_gap_seconds(self) -> int:
        low = max(1, to_int(self._proactive_cfg("min_interval_minutes", 90), 90))
        high = max(low, to_int(self._proactive_cfg("max_interval_minutes", 240), 240))
        return random.randint(low, high) * 60

    def _proactive_quiet(self) -> bool:
        """当前是否处于安静时段（支持跨零点，比如 23 点到 8 点）。"""
        start = to_int(self._proactive_cfg("quiet_start", 0), 0) % 24
        end = to_int(self._proactive_cfg("quiet_end", 7), 7) % 24
        hour = time.localtime().tm_hour
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _proactive_mode(self, info: dict[str, Any], has_memory: bool) -> str:
        hour = time.localtime().tm_hour
        if has_memory and random.randint(1, 100) <= 30:
            return "memory"
        if 5 <= hour < 10:
            return "morning"
        if 11 <= hour < 14:
            return "noon"
        if 14 <= hour < 18:
            return "afternoon"
        if 18 <= hour < 23:
            return "evening"
        if hour >= 23 or hour < 5:
            return "night"
        return "miss"

    # ---------------- 文案生成 -----------------------------

    def _proactive_facts(self, info: dict[str, Any]) -> list[str]:
        if not bool(self._proactive_cfg("include_memory", True)):
            return []
        sender = str(info.get("sender") or "")
        getter = getattr(self, "_memory_facts", None)
        if not sender or not callable(getter):
            return []
        try:
            return [str(item) for item in getter(sender, 4) if str(item).strip()]
        except Exception:
            return []

    def _proactive_prompt(self, info: dict[str, Any], mode: str, facts: list[str]) -> str:
        now = time.strftime("%Y-%m-%d %H:%M")
        who = str(info.get("sender_name") or info.get("sender") or "对方")
        title = str(info.get("master_title") or "")
        last = float(info.get("last_active", 0) or 0)
        gap = _gap_text(max(0, time.time() - last)) if last else "一阵子"
        limit = max(10, to_int(self._proactive_cfg("max_length", 60), 60))

        lines = [
            f"现在是 {now}。你平时就认识 {who}，已经安静了 {gap}，你想主动找 TA 说句话。",
            f"这次的方向：{_MODE_HINTS.get(mode, _MODE_HINTS['miss'])}。",
        ]
        if title:
            lines.append(f"{who} 是你的主人，你平时叫 TA「{title}」。")
        last_text = str(info.get("last_text") or "").strip()
        if last_text:
            lines.append(f"TA 上次跟你说的是：「{last_text}」。")
        if facts:
            lines.append("你记得关于 TA 的事：" + "；".join(facts) + "。")
        recent = [str(item) for item in (info.get("recent") or []) if str(item).strip()]
        if recent:
            lines.append("你最近主动说过：" + " / ".join(recent[-3:]) + "，别重复。")
        lines.append(
            f"请用你自己的口吻给 TA 发一条主动消息：一到两句，不超过 {limit} 字；"
            "不要 @ 任何人，不要加引号或标题，不要解释这是主动消息，"
            "不要提插件、接口、模型或系统提示，只输出消息正文。"
        )
        return "\n".join(lines)

    @staticmethod
    def _proactive_clean(raw: Any, limit: int) -> str:
        text = re.sub(r"```[a-zA-Z]*", "", str(raw or "")).replace("`", "")
        text = text.strip().strip("\"'“”「」『』")
        for prefix in ("消息：", "消息:", "主动消息：", "主动消息:", "回复：", "回复:"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return ""
        if len(text) > limit:
            text = text[: max(1, limit - 1)].rstrip() + "…"
        return text

    def _proactive_provider(self, umo: str) -> Any:
        provider = None
        provider_id = str(self._proactive_cfg("provider_id", "") or "").strip()
        try:
            if provider_id:
                getter = getattr(self.context, "get_provider_by_id", None)
                manager = getattr(self.context, "provider_manager", None)
                inst_map = getattr(manager, "inst_map", None) if manager is not None else None
                if isinstance(inst_map, dict) and inst_map and provider_id not in inst_map:
                    provider = None
                elif callable(getter):
                    provider = getter(provider_id)
            if provider is None:
                getter = getattr(self.context, "get_using_provider", None)
                if callable(getter):
                    try:
                        provider = getter(umo) if umo else getter()
                    except TypeError:
                        provider = getter()
        except Exception as exc:
            logger.warning(f"[proactive] 获取对话模型失败: {exc}")
            return None
        if provider is None or not hasattr(provider, "text_chat"):
            return None
        return provider

    async def _proactive_system_prompt(self, umo: str) -> str:
        """优先用 AstrBot 给这个会话配置的人格，保证语气一致。"""
        override = str(self._proactive_cfg("persona_prompt", "") or "").strip()
        if override:
            return override
        if not bool(self._proactive_cfg("use_persona", True)):
            return ""
        manager = getattr(self.context, "persona_manager", None)
        getter = getattr(manager, "get_default_persona_v3", None)
        if not callable(getter):
            return ""
        persona = None
        try:
            persona = await getter(umo)
        except Exception:
            try:
                persona = await getter()
            except Exception:
                return ""
        if isinstance(persona, dict):
            return str(persona.get("prompt") or persona.get("system_prompt") or "")
        return str(getattr(persona, "prompt", "") or "")

    async def _proactive_compose(
        self, umo: str, info: dict[str, Any], mode: str
    ) -> tuple[str, list[str]]:
        limit = max(10, to_int(self._proactive_cfg("max_length", 60), 60))
        view = self._proactive_enrich(umo, info)
        facts = self._proactive_facts(view)
        provider = self._proactive_provider(umo)
        if provider is None:
            return "", facts
        system_prompt = await self._proactive_system_prompt(umo)
        prompt = self._proactive_prompt(view, mode, facts)
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                system_prompt=system_prompt or None,
            )
        except Exception as exc:
            logger.warning(f"[proactive] 生成主动消息失败: {exc}")
            return "", facts
        text = self._proactive_clean(str(getattr(resp, "completion_text", "") or ""), limit)
        return text, facts

    def _proactive_fallback(self, mode: str) -> str:
        limit = max(10, to_int(self._proactive_cfg("max_length", 60), 60))
        custom = str(self._proactive_cfg("fallback_text", "") or "").strip()
        text = custom or _FALLBACKS.get(mode, _FALLBACKS["miss"])
        return self._proactive_clean(text, limit)

    def _proactive_stats(self) -> dict[str, Any]:
        data = self._proactive_load()
        sessions = data["sessions"]
        today = time.strftime("%Y-%m-%d")
        history = [item for item in data.get("history") or [] if isinstance(item, dict)]
        return {
            "enabled": bool(self._proactive_cfg("enabled", True)),
            "quiet": self._proactive_quiet(),
            "sessions": len(sessions),
            "sent_today": sum(
                int(info.get("sent_today", 0) or 0)
                for info in sessions.values()
                if isinstance(info, dict) and info.get("sent_date") == today
            ),
            "history": len(history),
            "last_sent": history[-1] if history else {},
            "task_running": bool(self._proactive_task and not self._proactive_task.done()),
            "path": self._proactive_path,
        }

    # ---------------- 发送与记录 -----------------------------

    async def _proactive_send(self, umo: str, text: str) -> bool:
        try:
            sent = await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as exc:
            logger.warning(f"[proactive] 发送到 {umo} 失败: {exc}")
            return False
        if not sent:
            logger.warning(f"[proactive] 找不到对应平台，消息没发出去: {umo}")
            return False
        logger.info(f"[proactive] 已主动发给 {umo}：{text[:40]}")
        return True

    def _proactive_record(self, umo: str, text: str, info: dict[str, Any]) -> None:
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        if info.get("sent_date") != today:
            info["sent_date"] = today
            info["sent_today"] = 0
        info["sent_today"] = int(info.get("sent_today", 0) or 0) + 1
        info["last_sent"] = now
        info["last_text_sent"] = text
        recent = [str(item) for item in (info.get("recent") or []) if str(item).strip()]
        recent.append(text)
        info["recent"] = recent[-5:]
        info["next_due"] = now + self._proactive_gap_seconds()
        data = self._proactive_load()
        history = data.setdefault("history", [])
        history.append({"umo": umo, "date": today, "time": time.strftime("%H:%M:%S"), "text": text})
        data["history"] = history[-50:]
        self._proactive_save()

    # ---------------- 定时循环 -----------------------------

    def _proactive_targets(self, now: float) -> list[tuple[str, dict[str, Any]]]:
        data = self._proactive_load()
        sessions = data["sessions"]
        explicit = self._proactive_list("target_umos")
        window = max(1, to_int(self._proactive_cfg("active_window_hours", 48), 48)) * 3600
        items: list[tuple[str, dict[str, Any]]] = []
        for umo, info in sessions.items():
            if not isinstance(info, dict):
                continue
            if explicit and umo not in explicit:
                continue
            last = float(info.get("last_active", 0) or 0)
            if last and now - last > window:
                continue
            items.append((umo, info))
        for umo in explicit:
            if umo not in sessions:
                items.append((umo, {"explicit": True}))
        items.sort(key=lambda pair: float(pair[1].get("last_active", 0) or 0), reverse=True)
        return items

    async def _proactive_tick(self) -> None:
        now = time.time()
        if self._proactive_dirty:
            self._proactive_save()
        if not bool(self._proactive_cfg("enabled", True)):
            return
        if self._proactive_quiet():
            return
        targets = self._proactive_targets(now)
        if not targets:
            return

        idle = max(0, to_int(self._proactive_cfg("idle_minutes", 20), 20)) * 60
        daily = max(0, to_int(self._proactive_cfg("daily_limit", 3), 3))
        chance = max(0, min(100, to_int(self._proactive_cfg("chance_percent", 60), 60)))
        max_round = max(1, to_int(self._proactive_cfg("max_per_round", 1), 1))
        today = time.strftime("%Y-%m-%d")
        sent_count = 0

        for umo, info in targets:
            if sent_count >= max_round:
                break
            last = float(info.get("last_active", 0) or 0)
            if last and now - last < idle:
                continue
            due = float(info.get("next_due", 0) or 0)
            if due and now < due:
                continue
            sent_today = int(info.get("sent_today", 0) or 0) if info.get("sent_date") == today else 0
            # 先排下一次，避免同一会话每轮都被重试
            info["next_due"] = now + self._proactive_gap_seconds()
            self._proactive_dirty = True
            if sent_today >= daily or chance <= 0:
                continue
            if random.randint(1, 100) > chance:
                continue
            view = self._proactive_enrich(umo, info)
            facts = self._proactive_facts(view)
            mode = self._proactive_mode(view, bool(facts))
            text, facts = await self._proactive_compose(umo, info, mode)
            if not text:
                text = self._proactive_fallback(mode)
            if not text:
                continue
            if not await self._proactive_send(umo, text):
                continue
            self._proactive_record(umo, text, info)
            sent_count += 1

    async def _proactive_loop(self) -> None:
        await asyncio.sleep(45)
        while True:
            try:
                await self._proactive_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[proactive] 主动搭话循环出错: {exc}")
            await asyncio.sleep(60)

    async def _proactive_start(self) -> None:
        if self._proactive_task and not self._proactive_task.done():
            return
        self._proactive_task = asyncio.create_task(self._proactive_loop())

    async def _proactive_stop(self) -> None:
        task = self._proactive_task
        self._proactive_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(f"[proactive] 停止主动搭话任务失败: {exc}")
        self._proactive_save()

    # ---------------- Handler：记录会话 -----------------------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def proactive_observe(self, event: AstrMessageEvent):
        """记下最近说话的会话，供主动搭话时挑选对象。"""
        if not bool(self._proactive_cfg("enabled", True)):
            return
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if not umo:
                return
            now = time.time()
            data = self._proactive_load()
            sessions = data["sessions"]
            info = sessions.get(umo)
            if not isinstance(info, dict):
                info = {"next_due": now + self._proactive_gap_seconds()}
                sessions[umo] = info
            info["last_active"] = now
            sender = str(event.get_sender_id() or "")
            if sender:
                info["sender"] = sender
                getter = getattr(self, "_master_title_for", None)
                if callable(getter):
                    try:
                        info["master_title"] = str(getter(sender) or "")
                    except Exception:
                        info["master_title"] = ""
            try:
                name = str(event.get_sender_name() or "")
            except Exception:
                name = ""
            if name:
                info["sender_name"] = name
            text = str(getattr(event, "message_str", "") or "").strip()
            if text and not text.startswith("/"):
                info["last_text"] = text[:80]
            limit = max(1, to_int(self._proactive_cfg("max_sessions", 30), 30))
            if len(sessions) > limit:
                kept = sorted(
                    sessions.items(),
                    key=lambda pair: float(pair[1].get("last_active", 0) or 0),
                )[-limit:]
                data["sessions"] = dict(kept)
            self._proactive_dirty = True
        except Exception as exc:
            logger.warning(f"[proactive] 记录会话失败: {exc}")

    # ---------------- Handler：手动触发 -----------------------------

    def _proactive_can_manage(self, event: AstrMessageEvent) -> bool:
        checker = getattr(self, "_complain_is_owner", None)
        if callable(checker):
            try:
                if checker(event):
                    return True
            except Exception:
                pass
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    @staticmethod
    def _proactive_arg(event: AstrMessageEvent, command: str) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        index = text.find(command)
        if index >= 0:
            text = text[index + len(command):].strip()
        return text

    def _proactive_recent_umo(self) -> str:
        data = self._proactive_load()
        sessions = {key: value for key, value in data["sessions"].items() if isinstance(value, dict)}
        if not sessions:
            return ""
        umo, _ = max(sessions.items(), key=lambda pair: float(pair[1].get("last_active", 0) or 0))
        return umo

    @filter.command("主动说话")
    async def cmd_proactive_say(self, event: AstrMessageEvent):
        """手动让她给最近聊过的会话发一条，用于测试或想她了的时候。"""
        if not self._proactive_can_manage(event):
            yield event.plain_result("只有主人或管理员能让她主动说话。")
            return
        target = self._proactive_arg(event, "主动说话") or self._proactive_recent_umo()
        if not target:
            yield event.plain_result("还没记下任何聊过的会话，先让别人跟她说句话再来。")
            return
        info = self._proactive_load()["sessions"].get(target)
        if not isinstance(info, dict):
            info = {}
        view = self._proactive_enrich(target, info)
        facts = self._proactive_facts(view)
        mode = self._proactive_mode(view, bool(facts))
        text, facts = await self._proactive_compose(target, info, mode)
        if not text:
            text = self._proactive_fallback(mode)
        if not text:
            yield event.plain_result("模型没接上，也没配置兜底话术，这次先不发了。")
            return
        if not await self._proactive_send(target, text):
            yield event.plain_result(f"没发出去：{target} 找不到对应的平台。")
            return
        info.setdefault("sent_date", time.strftime("%Y-%m-%d"))
        info.setdefault("sent_today", 0)
        self._proactive_record(target, text, info)
        yield event.plain_result(f"已经主动发给 {target}：\n{text}")

    @filter.command("主动帮助")
    async def cmd_proactive_help(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)
