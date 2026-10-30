"""AstrBot 插件：大肥鱼告状

被欺负时，她会私聊主人告状。

- LLM 工具通道（主路径）：模型判断对方在骂她、羞辱她、故意欺负她，
  调用 qq_complain，插件把告状的话私聊发给主人。
- 斜杠指令通道（兜底）：管理员用 /告状 内容 手动触发一次。
- 频控：同一个人有冷却时间，每天有总上限，避免刷屏打扰主人。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain

from .base import SuiteBase, to_int

NAME = "astrbot_plugin_dafeiyu_complain"

HELP_TEXT = """告状用法（平时直接跟她说话就行，不用指令）：
/告状 <内容>  手动让她私聊主人告一次状（仅管理员）"""


class ComplainMixin(SuiteBase):
    def setup_complain(self) -> None:
        self._complain_lock = asyncio.Lock()
        # 被谁欺负 -> 上次告状时间，做单人冷却
        self._last_sent: dict[str, float] = {}
        try:
            self._complain_data_path = os.path.join(
                self.module_data_dir(NAME), "complaints.json"
            )
        except Exception as exc:
            logger.warning(f"[complain] 获取数据目录失败，改用当前目录: {exc}")
            self._complain_data_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "complaints.json"
            )

    # ---------------- 配置读取 -----------------------------

    def _complain_cfg(self, key: str, default: Any = None) -> Any:
        return self.cfg("complain", key, default)

    @staticmethod
    def _target_qq(target: str) -> str:
        match = re.search(r"(\d{5,})$", target or "")
        return match.group(1) if match else ""

    def _owner_target(self, event: AstrMessageEvent | None = None) -> str:
        umo = str(self._complain_cfg("owner_umo", "") or "").strip()
        if umo:
            return umo
        raw = str(self._complain_cfg("owner_qq", "") or "").strip()
        for token in re.split(r"[,，\s]+", raw):
            if token.isdigit():
                return token
        origin = str(self._complain_cfg("owner_session", "") or "")
        return origin if self._target_qq(origin) else ""

    def _guardians(self) -> list[tuple[str, list[str], str]]:
        """解析 guardians，返回（显示称呼, 别名列表, 收件目标）。

        每项支持「称呼:QQ」「称呼:UMO」，称呼里可用 / 分隔多个叫法，
        例如「爸爸/爹/老爸:大肥鱼:FriendMessage:123456」。
        """
        raw = self._complain_cfg("guardians", []) or []
        if isinstance(raw, str):
            items: list[Any] = re.split(r"[,，;\n]+", raw)
        elif isinstance(raw, list):
            items = raw
        else:
            items = []
        result: list[tuple[str, list[str], str]] = []
        for item in items:
            title, target = "", ""
            if isinstance(item, dict):
                title = str(item.get("name") or item.get("title") or "").strip()
                target = str(
                    item.get("umo") or item.get("qq") or item.get("id") or ""
                ).strip()
            else:
                text = str(item or "").strip()
                if not text:
                    continue
                if re.search(r"[:：]", text):
                    title, target = re.split(r"[:：]", text, maxsplit=1)
                    title, target = title.strip(), target.strip()
                else:
                    target = text
            aliases = [one for one in re.split(r"[/|｜、]+", title) if one.strip()]
            aliases = [one.strip() for one in aliases] or [self._target_qq(target) or target]
            if target and all(target != exist for _, _, exist in result):
                result.append((aliases[0], aliases, target))
        return result

    def _find_guardian(self, name: str) -> tuple[str, str] | None:
        key = (name or "").strip().lstrip("@").strip()
        if not key:
            return None
        # 先精确匹配别名，再退化成包含匹配，避免「妈」被别的称呼抢先匹配
        for display, aliases, target in self._guardians():
            if key in aliases:
                return display, target
        for display, aliases, target in self._guardians():
            for alias in aliases:
                if key in alias or alias in key:
                    return display, target
        if key.isdigit():
            for display, _, target in self._guardians():
                if self._target_qq(target) == key:
                    return display, target
        return None

    def _owner_aliases(self) -> list[str]:
        raw = str(self._complain_cfg("owner_title", "主人") or "主人")
        aliases = [one.strip() for one in re.split(r"[/|｜、]+", raw) if one.strip()]
        return aliases or ["主人"]

    def _resolve_target(self, name: str) -> tuple[str, str] | None:
        """先按家人名单找，再按主人的称呼兜底。

        有人把另一位家人只写进了 owner_umo / owner_qq，名单里没有，
        这时候「给妈带个话」也要能对上。
        """
        found = self._find_guardian(name)
        if found:
            return found
        key = (name or "").strip().lstrip("@").strip()
        if not key:
            return None
        owner = self._owner_target()
        if not owner:
            return None
        if key.isdigit() and self._target_qq(owner) == key:
            return self._owner_aliases()[0], owner
        for alias in self._owner_aliases():
            if key == alias or key in alias or alias in key:
                return alias, owner
        return None

    def _route(self, event: AstrMessageEvent) -> tuple[str, str]:
        """选告状收件人：欺负者是某位家人时，发给另一位家人；否则发给 owner。"""
        sender = str(event.get_sender_id() or "")
        guardians = self._guardians()
        candidates: list[tuple[str, str]] = []
        if any(self._target_qq(target) == sender for _, _, target in guardians):
            candidates.extend(
                [
                    (display, target)
                    for display, _, target in guardians
                    if self._target_qq(target) != sender
                ]
            )
        owner = self._owner_target(event)
        if owner and self._target_qq(owner) != sender:
            candidates.append((str(self._complain_cfg("owner_title", "主人") or "主人"), owner))
        candidates.extend(
            [
                (display, target)
                for display, _, target in guardians
                if self._target_qq(target) != sender
            ]
        )
        for title, target in candidates:
            if target and self._target_qq(target) != sender:
                return title, target
        return "", ""

    def _sender_title(self, event: AstrMessageEvent) -> str:
        sender = str(event.get_sender_id() or "")
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        for display, _, target in self._guardians():
            if sender and self._target_qq(target) == sender:
                return display
        # 名单里配的是 UMO 且和当前会话完全一致，也算本人
        for display, _, target in self._guardians():
            if origin and not target.isdigit() and target == origin:
                return display
        session = str(self._complain_cfg("owner_session", "") or "")
        if session and origin == session:
            return self._owner_aliases()[0]
        return ""

    def _complain_is_owner(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id() or "")
        owner = self._target_qq(self._owner_target(event))
        if owner and sender == owner:
            return True
        if any(self._target_qq(target) == sender for _, _, target in self._guardians()):
            return True
        origin = getattr(event, "unified_msg_origin", "") or ""
        session = str(self._complain_cfg("owner_session", "") or "")
        if session and origin == session:
            return True
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    # ---------------- 消息发送 -----------------------------

    def _complain_client(self, event: AstrMessageEvent) -> Any:
        bot = getattr(event, "bot", None)
        if bot is not None:
            api = getattr(bot, "api", None) or bot
            if hasattr(api, "call_action"):
                return api
        return None

    async def _complain_group_at(
        self, event: AstrMessageEvent, qq: str, text: str
    ) -> bool:
        """私聊发不出去时，退一步在当前群里 @ 对方。"""
        try:
            group_id = str(event.get_group_id() or "")
        except Exception:
            return False
        client = self._complain_client(event)
        if not group_id.isdigit() or client is None:
            return False
        try:
            await client.call_action(
                "send_group_msg",
                group_id=int(group_id),
                message=[
                    {"type": "at", "data": {"qq": qq}},
                    {"type": "text", "data": {"text": " " + text}},
                ],
            )
        except Exception as exc:
            logger.warning(f"[complain] 群里 @ 发送失败: {exc}")
            return False
        logger.info(f"[complain] 私聊不可用，已在群里 @{qq} 送达")
        return True

    async def _complain_send(
        self, event: AstrMessageEvent, target: str, text: str
    ) -> None:
        """发送顺序：纯数字先私聊；UMO 先走 AstrBot 主动消息；
        失败后依次回退到私聊、当前群 @，都失败才报错。"""
        qq = target if target.isdigit() else self._target_qq(target)
        client = self._complain_client(event)

        if target.isdigit() and client is not None:
            try:
                await client.call_action(
                    "send_private_msg", user_id=int(target), message=text
                )
                return
            except Exception as exc:
                logger.warning(f"[complain] 私聊 {target} 失败: {exc}")

        if not target.isdigit():
            ok = False
            try:
                ok = await self.context.send_message(target, MessageChain([Plain(text)]))
            except Exception as exc:
                logger.warning(f"[complain] UMO 发送异常: {exc}")
            if ok:
                return
            if qq and client is not None:
                try:
                    await client.call_action(
                        "send_private_msg", user_id=int(qq), message=text
                    )
                    logger.info(f"[complain] UMO {target} 不可用，已改用私聊 {qq}")
                    return
                except Exception as exc:
                    logger.warning(f"[complain] UMO 回退私聊 {qq} 失败: {exc}")

        if qq and await self._complain_group_at(event, qq, text):
            return
        raise RuntimeError(f"所有发送方式都失败：target={target}")

    # ---------------- 告状记录 -----------------------------

    def _complain_load(self) -> list[dict]:
        try:
            with open(self._complain_data_path, "r", encoding="utf-8") as file:
                data = json.load(file)
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except Exception as exc:
            logger.warning(f"[complain] 读取记录失败: {exc}")
            return []

    def _complain_save(self, records: list[dict]) -> None:
        try:
            os.makedirs(os.path.dirname(self._complain_data_path), exist_ok=True)
            with open(self._complain_data_path, "w", encoding="utf-8") as file:
                json.dump(records[-200:], file, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning(f"[complain] 写入记录失败: {exc}")

    def _complain_count_today(self, records: list[dict], kind: str = "complain") -> int:
        today = time.strftime("%Y-%m-%d")
        return sum(
            1
            for item in records
            if item.get("date") == today and item.get("type", "complain") == kind
        )

    def _same_person_today(self, records: list[dict], sender_id: str) -> int:
        today = time.strftime("%Y-%m-%d")
        return sum(
            1
            for item in records
            if item.get("date") == today
            and item.get("type", "complain") == "complain"
            and str(item.get("from_uin", "")) == sender_id
        )

    # ---------------- 核心业务 -----------------------------

    def _speaker(self, event: AstrMessageEvent, override: str = "") -> tuple[str, str]:
        """返回（称呼, QQ）。优先用模型/指令给的称呼。"""
        who = (override or "").strip().lstrip("@").strip()
        sender_id = str(event.get_sender_id() or "")
        if not who:
            who = self._sender_title(event)
        if not who:
            try:
                who = str(event.get_sender_name() or "").strip()
            except Exception:
                who = ""
        if not who:
            who = sender_id or "有人"
        return who, sender_id

    def _compose(self, to_title: str, who: str, bully: str, words: str) -> str:
        words = (words or "").strip().strip("「」“”\"'")
        if words:
            return words
        prefix = f"{to_title}，" if to_title else ""
        bully = (bully or "").strip().strip("「」“”\"'")
        if bully:
            return f"{prefix}{who} 刚刚说「{bully}」，我有点难受……"
        return f"{prefix}{who} 刚刚欺负我，我有点难受……"

    def _limit_reason(self, kind: str, sender_id: str) -> str | None:
        if kind == "relay":
            gap = to_int(self._complain_cfg("relay_cooldown_seconds", 15), 15)
            limit = to_int(self._complain_cfg("relay_daily_limit", 20), 20)
            label = "传话"
        else:
            gap = to_int(self._complain_cfg("cooldown_seconds", 600), 600)
            limit = to_int(self._complain_cfg("daily_limit", 5), 5)
            label = "告状"
        now = time.monotonic()
        last = self._last_sent.get(f"{kind}:{sender_id}", 0.0)
        if gap > 0 and now - last < gap:
            wait = max(1, int(gap - (now - last)) + 1)
            return f"刚{label}过，{wait} 秒后再来，先等一下。"
        records = self._complain_load()
        if limit > 0 and self._complain_count_today(records, kind) >= limit:
            return f"今天的{label}次数用完了（上限 {limit} 次/天）。"
        return None

    async def _complain(
        self, event: AstrMessageEvent, bully: str = "", words: str = "", who: str = ""
    ) -> str:
        if not self._complain_cfg("enabled", True):
            return "告状开关关着，这次不告了。"

        to_title, owner = self._route(event)
        if not owner:
            return "还没配置能告状的对象，先在插件配置里填 owner_umo / owner_qq 或 guardians。"

        name, sender_id = self._speaker(event, who)
        reason = self._limit_reason("complain", sender_id)
        if reason:
            return reason

        max_len = to_int(self._complain_cfg("max_length", 80), 80)
        repeat = 1
        if self._complain_cfg("escalate_repeat", True):
            repeat = self._same_person_today(self._complain_load(), sender_id) + 1
        text = self._compose(to_title, name, bully, words)
        if repeat >= 2:
            text = text.rstrip("。！!…") + f"，今天已经第 {repeat} 次了。"
        if len(text) > max_len:
            text = text[: max_len - 1] + "…"

        try:
            group_id = str(event.get_group_id() or "")
        except Exception:
            group_id = ""
        if group_id and self._complain_cfg("include_source", True):
            text = f"{text}\n（来自群 {group_id}）"

        try:
            await self._complain_send(event, owner, text)
        except Exception as exc:
            logger.error(f"[complain] 告状发送失败: {exc}")
            return "这次没发出去。用你自己的口吻轻轻说一句等会儿再试，别解释原因，也别提设置。"

        self._last_sent[f"complain:{sender_id}"] = time.monotonic()
        async with self._complain_lock:
            records = self._complain_load()
            records.append(
                {
                    "type": "complain",
                    "repeat": repeat,
                    "date": time.strftime("%Y-%m-%d"),
                    "time": time.strftime("%H:%M:%S"),
                    "from_uin": sender_id,
                    "from_name": name,
                    "to_name": to_title,
                    "bully": (bully or "").strip()[:200],
                    "words": text,
                    "group": group_id,
                    "session": getattr(event, "unified_msg_origin", ""),
                }
            )
            self._complain_save(records)

        logger.info(f"[complain] 已告状给 {to_title}：{name}({sender_id}) 第 {repeat} 次")
        hint = "已经私下告过状了。你可以顺势回一句委屈、告状或者哼一声的话，一句就够，别把私聊内容说出来。"
        return hint if self._complain_cfg("in_chat_hint", True) else "已经私下告过状了。"

    async def _relay(
        self, event: AstrMessageEvent, to: str = "", content: str = "", words: str = ""
    ) -> str:
        if not self._complain_cfg("relay_enabled", True):
            return "传话开关关着，这次不传了。"

        content = (content or "").strip().strip("「」“”\"'")
        if not content:
            return "要传什么话？说清楚一点。"

        sender_id = str(event.get_sender_id() or "")
        origin = str(getattr(event, "unified_msg_origin", "") or "")

        from_title = self._sender_title(event)
        if not from_title and self._complain_is_owner(event):
            from_title = self._owner_aliases()[0]
        if not from_title:
            logger.info(
                f"[complain] 拒绝传话：sender={sender_id} umo={origin} "
                f"名单={[target for _, _, target in self._guardians()]}"
            )
            return "只有家里人能让我传话。"

        to = (to or "").strip()
        if to:
            found = self._resolve_target(to)
        else:
            # 没说要给谁带话：名单里除自己之外只有一位时就直接发给他
            others = [
                (display, target)
                for display, _, target in self._guardians()
                if self._target_qq(target) != sender_id
            ]
            owner = self._owner_target()
            if not others and owner and self._target_qq(owner) != sender_id:
                others = [(self._owner_aliases()[0], owner)]
            found = others[0] if len(others) == 1 else None
            if not found:
                names = "、".join(display for display, _, _ in self._guardians())
                return f"要给谁带话？家里人名单里有：{names or '（空）'}。"
        if not found:
            names = "、".join(display for display, _, _ in self._guardians())
            logger.info(
                f"[complain] 传话找不到收件人：to={to} sender={sender_id} "
                f"名单={[target for _, _, target in self._guardians()]}"
            )
            return f"找不到「{to}」这个人，家里人名单里只有：{names or '（空）'}。"

        to_title, target = found
        if self._target_qq(target) == sender_id:
            return "这就是你自己，不用我传。"

        reason = self._limit_reason("relay", sender_id)
        if reason:
            return reason

        max_len = to_int(self._complain_cfg("max_length", 80), 80)
        words = (words or "").strip().strip("「」“”\"'")
        text = words or f"{to_title}，{from_title}让我跟你说：{content}"
        if len(text) > max_len:
            text = text[: max_len - 1] + "…"

        try:
            await self._complain_send(event, target, text)
        except Exception as exc:
            logger.error(f"[complain] 传话失败: {exc}")
            return "这次没传出去。用你自己的口吻说一句等会儿再试，别解释原因，也别提设置。"

        self._last_sent[f"relay:{sender_id}"] = time.monotonic()
        async with self._complain_lock:
            records = self._complain_load()
            records.append(
                {
                    "type": "relay",
                    "date": time.strftime("%Y-%m-%d"),
                    "time": time.strftime("%H:%M:%S"),
                    "from_uin": sender_id,
                    "from_name": from_title,
                    "to_name": to_title,
                    "content": content[:200],
                    "words": text,
                    "session": getattr(event, "unified_msg_origin", ""),
                }
            )
            self._complain_save(records)

        logger.info(f"[complain] 已传话 {from_title} -> {to_title}：{content[:30]}")
        return f"已经转告{to_title}了。你可以自然回一句「好，我说了」这样的确认，一句就够。"

    # ---------------- LLM 工具 -----------------------------

    @filter.llm_tool(name="qq_complain")
    async def tool_complain(
        self, event: AstrMessageEvent, bully: str = "", words: str = "", who: str = ""
    ):
        '''被欺负时私聊向主人告状。
        有人骂你、羞辱你、人身攻击、故意让你难堪、说很过分的话时调用；正常的玩笑、亲昵的调侃、轻微的吐槽不要调用。
        调用前先看这个人的行为是不是真的让你难受，别为了撒娇滥用。告完状在聊天里可以委屈一下，但不要把私聊内容说出来。

        Args:
            bully(string): 对方让你难受的原话，最多几十个字，没有就留空
            words(string): 你想对收件人说的告状话，一句口语，比如「爸，妈又骂我笨」，留空会自动拼一句
            who(string): 欺负你的人怎么称呼，可以是昵称或 QQ 号，留空则用当前说话人
        '''
        return await self._complain(event, bully=bully, words=words, who=who)

    @filter.llm_tool(name="qq_relay_message")
    async def tool_relay(
        self,
        event: AstrMessageEvent,
        to: str = "",
        content: str = "",
        words: str = "",
    ):
        '''家里人让你给另一位家人带话、传话时调用。
        例如「叫你爹起床」「跟你妈说我不回来吃饭」「让你妈把药吃了」。只是闲聊、让你自己知道一件事、或者不是家里人让你传话时，不要调用。

        Args:
            to(string): 收件人称呼，比如 爸爸、妈、爹，会按家人名单里的别名匹配
            content(string): 要带的话，保持原话意思，简短一点
            words(string): 你转达时实际发出去的那句话，用你自己的语气改写，保留原意，比如「爸～妈喊你起床啦」；留空则用「爸，妈让我跟你说：……」的模板
        '''
        return await self._relay(event, to=to, content=content, words=words)

    # ---------------- 斜杠指令 -----------------------------

    @filter.command("告状")
    async def cmd_complain(self, event: AstrMessageEvent):
        if not self._complain_is_owner(event):
            yield event.plain_result("你没有权限使用该指令")
            return

        text = (event.message_str or "").strip()
        index = text.find("告状")
        if index >= 0:
            text = text[index + 2 :].strip()

        target = ""
        for comp in event.get_messages():
            if isinstance(comp, At):
                qq = str(getattr(comp, "qq", "") or "")
                if qq.isdigit():
                    target = qq
                    break

        if not text:
            yield event.plain_result("用法：/告状 想对收件人说的内容（可以先 @ 那个人）")
            return
        result = await self._complain(event, words=text, who=target)
        yield event.plain_result(result)

    @filter.command("告状帮助")
    async def cmd_complain_help(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)
