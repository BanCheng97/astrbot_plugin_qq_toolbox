"""AstrBot 插件：大肥鱼长期记忆。

纯本地实现，不依赖数据库、embedding、知识库：

- 存：``plugin_data/<旧目录名>/memory.json``（跟历史数据放一起，升级不丢）
- 写：消息监听 + 关键词预筛，攒够若干句再让模型抽一次「关于这个人的事实」
- 读：``on_llm_request`` 时把当前说话人的事实拼进 system_prompt

所有方法都带 ``memory`` 前缀，避免多继承时和别的模块重名。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain

from .base import SuiteBase, load_json, save_json, to_int

# 沿用旧数据目录，告状 / 用量 / 工具箱的历史数据不用搬家
NAME = "astrbot_plugin_qq_toolbox"

# 只有像「在说自己的事」的句子才值得抽取，避免每句话都调一次模型
_FACT_HINTS = (
    "我叫", "我是", "我的", "我住", "我在", "我今年", "我生日", "我喜欢", "我讨厌",
    "我爱", "我养", "我上班", "我工作", "我上学", "我老家", "我家", "我妈", "我爸",
    "我老婆", "我老公", "我对象", "我孩子", "我猫", "我狗",
    "生日", "过敏", "不吃", "爱吃", "害怕", "习惯", "记住", "别忘了",
)

_EXTRACT_PROMPT = """你是一个记忆力整理助手。从下面这个人说过的话里，抽出「关于他自己、值得长期记住」的事实。

要求：
1. 只输出 JSON 数组，每项是一句不超过 20 字的短句，省略主语，例如 "住在杭州"、"养了一只猫叫团子"
2. 只记录稳定的信息：身份、称呼、住址、工作或学校、喜好、厌恶、家人、宠物、作息、忌口、重要日期
3. 不要记录临时状态（今天心情不好、刚吃完饭），不要记录别人的事，不要臆测
4. 没有可记的就输出 []

只输出 JSON，不要解释。"""

HELP_TEXT = """记忆相关指令：
/记忆  看看我记得关于你的事
/记住 <内容>  手动让我记一件事
/忘掉 <关键词>  删掉相关的一条记忆
/清空记忆  清掉自己的记忆（主人可加「全部」清所有人）
/记忆帮助  显示这段说明"""


class MemoryMixin(SuiteBase):
    def setup_memory(self) -> None:
        self._memory_path = os.path.join(self.module_data_dir(NAME), "memory.json")
        self._memory_data: dict[str, Any] | None = None
        # sender -> 攒着待抽取的句子
        self._memory_pending: dict[str, list[str]] = {}
        # sender -> 上次抽取时间（monotonic）
        self._memory_last_extract: dict[str, float] = {}
        self._memory_tasks: set[asyncio.Task] = set()

    def _memory_cfg(self, key: str, default: Any = None) -> Any:
        return self.cfg("memory", key, default)

    # ---------------- 存取 -----------------------------

    def _memory_load(self) -> dict[str, Any]:
        if self._memory_data is not None:
            return self._memory_data
        data = load_json(self._memory_path, {})
        if not isinstance(data, dict):
            data = {}
        if not isinstance(data.get("users"), dict):
            data["users"] = {}
        self._memory_data = data
        return data

    def _memory_save(self) -> None:
        data = self._memory_load()
        data["updated"] = int(time.time())
        # 留一份上一版作为备份，JSON 写坏时还能手动回退
        try:
            if os.path.isfile(self._memory_path):
                with open(self._memory_path, "rb") as src:
                    raw = src.read()
                with open(self._memory_path + ".bak", "wb") as dst:
                    dst.write(raw)
        except Exception:
            pass
        save_json(self._memory_path, data)

    def _memory_user(self, sender: str) -> dict[str, Any]:
        users = self._memory_load()["users"]
        entry = users.get(sender)
        if not isinstance(entry, dict):
            entry = {"facts": [], "updated": 0}
            users[sender] = entry
        if not isinstance(entry.get("facts"), list):
            entry["facts"] = []
        return entry

    def _memory_norm(self, text: str) -> str:
        clean = re.sub(r"\s+", "", str(text or "")).strip("。，,.;；")
        limit = max(8, to_int(self._memory_cfg("fact_max_chars", 40), 40))
        return clean[:limit]

    # ---------------- 增删查 -----------------------------

    def _memory_add(self, sender: str, text: str, weight: int = 1) -> bool:
        """写入一条事实；重复的只加权重，返回是否新增。"""
        sender = str(sender or "").strip()
        clean = self._memory_norm(text)
        if not sender or len(clean) < 2:
            return False
        entry = self._memory_user(sender)
        now = int(time.time())
        for fact in entry["facts"]:
            if not isinstance(fact, dict):
                continue
            old = str(fact.get("text") or "")
            if clean == old or (len(clean) >= 4 and (clean in old or old in clean)):
                fact["weight"] = to_int(fact.get("weight", 1), 1) + max(1, weight)
                fact["ts"] = now
                entry["updated"] = now
                self._memory_save()
                return False
        entry["facts"].append({"text": clean, "ts": now, "weight": max(1, weight)})
        limit = max(5, to_int(self._memory_cfg("max_facts_per_user", 40), 40))
        if len(entry["facts"]) > limit:
            # 权重低的先淘汰，同权重再按时间淘汰
            entry["facts"].sort(
                key=lambda f: (to_int(f.get("weight", 1), 1), to_int(f.get("ts", 0), 0))
            )
            del entry["facts"][: len(entry["facts"]) - limit]
        entry["updated"] = now
        self._memory_save()
        return True

    def _memory_delete(self, sender: str, keyword: str) -> int:
        sender = str(sender or "").strip()
        word = self._memory_norm(keyword)
        if not sender or not word:
            return 0
        entry = self._memory_user(sender)
        kept = [
            fact
            for fact in entry["facts"]
            if not (isinstance(fact, dict) and word in str(fact.get("text") or ""))
        ]
        removed = len(entry["facts"]) - len(kept)
        if removed:
            entry["facts"] = kept
            entry["updated"] = int(time.time())
            self._memory_save()
        return removed

    def _memory_clear(self, sender: str | None = None) -> int:
        users = self._memory_load()["users"]
        if sender:
            entry = users.get(str(sender))
            count = len(entry.get("facts") or []) if isinstance(entry, dict) else 0
            if isinstance(entry, dict):
                entry["facts"] = []
                entry["updated"] = int(time.time())
                self._memory_save()
            return count
        count = sum(
            len(e.get("facts") or []) for e in users.values() if isinstance(e, dict)
        )
        self._memory_data = {"users": {}, "updated": int(time.time())}
        self._memory_save()
        return count

    def _memory_list(self, sender: str) -> list[dict[str, Any]]:
        """按权重 + 时间排序的全部事实。"""
        entry = self._memory_load()["users"].get(str(sender or ""))
        if not isinstance(entry, dict):
            return []
        facts = [f for f in (entry.get("facts") or []) if isinstance(f, dict) and f.get("text")]
        facts.sort(
            key=lambda f: (to_int(f.get("weight", 1), 1), to_int(f.get("ts", 0), 0)),
            reverse=True,
        )
        return facts

    def _memory_facts(self, sender: str, limit: int) -> list[str]:
        return [str(f.get("text")) for f in self._memory_list(sender)[: max(1, limit)]]

    def _memory_stats(self) -> dict[str, Any]:
        users = self._memory_load()["users"]
        facts = sum(
            len([f for f in (e.get("facts") or []) if isinstance(f, dict)])
            for e in users.values()
            if isinstance(e, dict)
        )
        return {
            "enabled": bool(self._memory_cfg("enabled", True)),
            "inject_enabled": bool(self._memory_cfg("inject_enabled", True)),
            "auto_extract": bool(self._memory_cfg("auto_extract", True)),
            "users": len(users),
            "facts": facts,
            "pending": sum(len(v) for v in self._memory_pending.values()),
            "inject_top_n": to_int(self._memory_cfg("inject_top_n", 6), 6),
        }

    def _memory_all(self) -> list[dict[str, Any]]:
        """给控制台用：每个人一条。"""
        users = self._memory_load()["users"]
        out: list[dict[str, Any]] = []
        for sender, entry in users.items():
            if not isinstance(entry, dict):
                continue
            facts = self._memory_list(sender)
            if not facts and not entry.get("updated"):
                continue
            out.append(
                {
                    "sender": str(sender),
                    "name": str(entry.get("name") or ""),
                    "facts": [{"text": str(f.get("text")), "ts": to_int(f.get("ts", 0), 0), "weight": to_int(f.get("weight", 1), 1)} for f in facts],
                    "updated": to_int(entry.get("updated", 0), 0),
                }
            )
        out.sort(key=lambda item: item["updated"], reverse=True)
        return out

    # ---------------- 消息 → 事实 -----------------------------

    def _memory_text(self, event: AstrMessageEvent) -> str:
        text = ""
        try:
            for comp in event.get_messages():
                if isinstance(comp, Plain):
                    text += comp.text or ""
        except Exception:
            text = ""
        if not text:
            text = str(getattr(event, "message_str", "") or "")
        return text.strip()

    def _memory_wants(self, text: str) -> bool:
        if not text or len(text) < 4 or text.startswith("/"):
            return False
        if not bool(self._memory_cfg("keyword_gate", True)):
            return True
        return any(hint in text for hint in _FACT_HINTS)

    def _memory_provider(self, event: AstrMessageEvent | None) -> Any:
        provider = None
        provider_id = str(self._memory_cfg("extract_provider_id") or "").strip()
        try:
            if provider_id:
                pm = getattr(self.context, "provider_manager", None)
                inst_map = getattr(pm, "inst_map", None) if pm is not None else None
                getter = getattr(self.context, "get_provider_by_id", None)
                # 先自查，避免 AstrBot 对失效 ID 打 "Provider xxx was not found" 警告
                if isinstance(inst_map, dict) and inst_map and provider_id not in inst_map:
                    logger.info(f"[memory] 抽取模型 {provider_id} 已失效，回退当前会话模型")
                elif callable(getter):
                    provider = getter(provider_id)
            if provider is None:
                getter = getattr(self.context, "get_using_provider", None)
                if callable(getter):
                    umo = getattr(event, "unified_msg_origin", "") if event is not None else ""
                    provider = getter(umo) if umo else getter()
        except Exception as exc:
            logger.warning(f"[memory] 获取抽取模型失败: {exc}")
            return None
        if provider is None or not hasattr(provider, "text_chat"):
            return None
        return provider

    @staticmethod
    def _memory_parse(raw: str) -> list[str]:
        """从模型回复里取出 JSON 数组；解析失败就退回逐行取。"""
        text = re.sub(r"```[a-zA-Z]*\s*", "", str(raw or "")).replace("`", "").strip()
        start, end = text.find("["), text.rfind("]")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                if isinstance(data, list):
                    return [str(item).strip() for item in data if str(item).strip()]
            except Exception:
                pass
        lines = []
        for line in text.splitlines():
            line = line.strip().lstrip("-•*·、 ").strip("\"'“”")
            if line and line not in ("[", "]") and "json" not in line.lower():
                lines.append(line)
        return lines[:10]

    async def _memory_extract(
        self, sender: str, texts: list[str], event: AstrMessageEvent | None = None
    ) -> None:
        """调一次模型，把攒下的句子抽成关于这个人的事实。"""
        provider = self._memory_provider(event)
        if provider is None:
            return
        body = "\n".join(f"- {text}" for text in texts)
        try:
            resp = await provider.text_chat(
                prompt=body, system_prompt=_EXTRACT_PROMPT, contexts=[]
            )
        except Exception as exc:
            logger.warning(f"[memory] 抽取记忆失败: {exc}")
            return
        added = 0
        for item in self._memory_parse(str(getattr(resp, "completion_text", "") or ""))[:10]:
            if self._memory_add(sender, item):
                added += 1
        if added:
            logger.info(f"[memory] {sender} 新增 {added} 条记忆")

    # ---------------- Handler：观察消息 -----------------------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def memory_observe(self, event: AstrMessageEvent):
        """把疑似「在说自己」的句子攒起来，攒够就后台抽一次记忆。"""
        if not bool(self._memory_cfg("enabled", True)):
            return
        if not bool(self._memory_cfg("auto_extract", True)):
            return
        try:
            sender = str(event.get_sender_id() or "")
            if not sender:
                return
            text = self._memory_text(event)
            if not self._memory_wants(text):
                return
            pending = self._memory_pending.setdefault(sender, [])
            clipped = text[:200]
            if clipped not in pending:
                pending.append(clipped)
            every = max(2, to_int(self._memory_cfg("extract_every_n_messages", 6), 6))
            if len(pending) < every:
                return
            gap = max(0, to_int(self._memory_cfg("extract_min_interval_minutes", 30), 30))
            now = time.monotonic()
            if now - self._memory_last_extract.get(sender, 0) < gap * 60:
                return
            self._memory_last_extract[sender] = now
            batch = pending[:every]
            del pending[:every]
            if not batch:
                return
            task = asyncio.create_task(self._memory_extract(sender, batch, event))
            self._memory_tasks.add(task)
            task.add_done_callback(self._memory_tasks.discard)
        except Exception as exc:
            logger.warning(f"[memory] 观察消息失败: {exc}")

    # ---------------- Handler：注入记忆 -----------------------------

    @filter.on_llm_request()
    async def memory_inject(self, event: AstrMessageEvent, req: Any):
        """把当前说话人的记忆拼进 system_prompt。"""
        if not bool(self._memory_cfg("enabled", True)):
            return
        if not bool(self._memory_cfg("inject_enabled", True)):
            return
        if getattr(req, "_memory_injected", False):
            return
        try:
            sender = str(event.get_sender_id() or "")
            if not sender:
                return
            top_n = max(1, to_int(self._memory_cfg("inject_top_n", 6), 6))
            facts = self._memory_facts(sender, top_n)
            if facts:
                block = (
                    "【你记得关于当前对话者的长期记忆】\n"
                    + "\n".join(f"- {fact}" for fact in facts)
                    + "\n（这些是你记得的事，聊天时自然带出来就好，不要逐条复述，也不要说「根据记忆」）"
                )
            else:
                # 没有记忆时也要明说，否则模型会顺着「你记住了吗」编出记得的事
                block = (
                    "【长期记忆】你目前还没有关于这个人的长期记忆。"
                    "对方问「你还记得我吗」「你记住了什么」时，如实说还没记住，"
                    "让对方直接告诉你值得记的事（也可以发 /记住 内容）；"
                    "绝对不要编造记得的事情，也不要承诺「已经记住了」。"
                )
            current = str(getattr(req, "system_prompt", "") or "")
            req.system_prompt = f"{current}\n\n{block}" if current else block
            try:
                req._memory_injected = True
            except Exception:
                pass
        except Exception as exc:
            logger.warning(f"[memory] 注入记忆失败: {exc}")

    # ---------------- 斜杠指令 -----------------------------

    @staticmethod
    def _memory_arg(event: AstrMessageEvent, command: str) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        index = text.find(command)
        if index >= 0:
            text = text[index + len(command) :].strip()
        return text

    @filter.command("记忆")
    async def cmd_memory(self, event: AstrMessageEvent):
        arg = self._memory_arg(event, "记忆")
        sender = str(event.get_sender_id() or "")
        if arg.isdigit() and arg != sender:
            if not self._complain_is_owner(event):
                yield event.plain_result("只有主人能看别人的记忆。")
                return
            sender = arg
        facts = self._memory_list(sender)
        if not facts:
            yield event.plain_result("我还没记住关于你的事情，多跟我说说就有了～")
            return
        lines = [f"{index}. {fact.get('text')}" for index, fact in enumerate(facts, 1)]
        yield event.plain_result("我记得关于你的事：\n" + "\n".join(lines))

    @filter.command("记住")
    async def cmd_memory_remember(self, event: AstrMessageEvent):
        arg = self._memory_arg(event, "记住")
        if not arg:
            yield event.plain_result("用法：/记住 你想让我记住的事")
            return
        sender = str(event.get_sender_id() or "")
        # 手动记的事更可信，权重给高一点
        self._memory_add(sender, arg, weight=3)
        yield event.plain_result(f"记住了：{self._memory_norm(arg)}")

    @filter.command("忘掉")
    async def cmd_memory_forget(self, event: AstrMessageEvent):
        arg = self._memory_arg(event, "忘掉")
        if not arg:
            yield event.plain_result("用法：/忘掉 关键词")
            return
        removed = self._memory_delete(str(event.get_sender_id() or ""), arg)
        if removed:
            yield event.plain_result(f"忘掉了 {removed} 条相关记忆。")
        else:
            yield event.plain_result("没找到相关的记忆。")

    @filter.command("清空记忆")
    async def cmd_memory_clear(self, event: AstrMessageEvent):
        arg = self._memory_arg(event, "清空记忆")
        if arg == "全部":
            if not self._complain_is_owner(event):
                yield event.plain_result("只有主人能清空所有人的记忆。")
                return
            removed = self._memory_clear(None)
            yield event.plain_result(f"已清空全部记忆（{removed} 条）。")
            return
        removed = self._memory_clear(str(event.get_sender_id() or ""))
        yield event.plain_result(f"已清空关于你的 {removed} 条记忆。")

    @filter.command("记忆帮助")
    async def cmd_memory_help(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)
