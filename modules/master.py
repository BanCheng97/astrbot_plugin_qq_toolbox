"""AstrBot 插件：大肥鱼认主。

支持多位主人，每位一个固定称呼（可以写多个叫法）。用于：

- 聊天前把「谁是主人、该怎么称呼」注入 system_prompt，她开口就能叫对；
- 主人可以给其他人认主、改称呼、逐出；
- 和告状 / 传话 / QQ 工具箱联动：主人自动获得相应权限，传话时能按称呼找到人。

数据存 ``plugin_data/astrbot_plugin_qq_toolbox/masters.json``，纯本地 JSON。
所有方法都带 ``master`` 前缀，避免多继承时和其他模块重名。
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At

from .base import SuiteBase, load_json, save_json, to_int

# 沿用旧数据目录，和记忆 / 历史记录放在一起
NAME = "astrbot_plugin_qq_toolbox"

HELP_TEXT = """认主相关指令：
/认主 称呼  认她当主人（还没有主人时可直接用；已有主人需现有主人或管理员）
/认主 @某人 称呼  主人/管理员给某个人认主
/主人列表  看看现在有哪些主人、怎么称呼
/改称呼 @某人 新称呼  给某位主人换个叫法（主人/管理员）
/取消认主 @某人  取消某位主人（主人/管理员）
/认主帮助  显示这段说明"""

_TITLE_LIMIT = 16


class MasterMixin(SuiteBase):
    def setup_master(self) -> None:
        self._master_path = os.path.join(self.module_data_dir(NAME), "masters.json")
        self._master_data: dict[str, Any] | None = None

    def _master_cfg(self, key: str, default: Any = None) -> Any:
        return self.cfg("master", key, default)

    # ---------------- 存取 -----------------------------

    def _master_load(self) -> dict[str, Any]:
        if self._master_data is not None:
            return self._master_data
        raw = load_json(self._master_path, {})
        if not isinstance(raw, dict):
            raw = {}
        items = raw.get("masters")
        cleaned: list[dict[str, Any]] = []
        if isinstance(items, list):
            for item in items:
                entry = self._master_normalize(item)
                if entry and all(entry["qq"] != old["qq"] for old in cleaned):
                    cleaned.append(entry)
        raw["masters"] = cleaned
        self._master_data = raw
        return raw

    def _master_save(self) -> None:
        data = self._master_load()
        data["updated"] = int(time.time())
        save_json(self._master_path, data)

    def _master_normalize(self, item: Any) -> dict[str, Any] | None:
        """把一条记录整理成固定结构；脏数据直接丢弃。"""
        if not isinstance(item, dict):
            return None
        qq = re.sub(r"\D", "", str(item.get("qq") or item.get("id") or ""))
        if len(qq) < 5:
            return None
        title = self._master_clean_title(item.get("title"), "")
        # 优先沿用已保存的别名，否则从称呼里拆（「哥哥/哥」）
        raw_aliases = item.get("aliases")
        if isinstance(raw_aliases, list) and raw_aliases:
            aliases = [str(one).strip()[:_TITLE_LIMIT] for one in raw_aliases if str(one).strip()]
        else:
            aliases = [one.strip()[:_TITLE_LIMIT] for one in re.split(r"[/|｜、]+", str(item.get("title") or "")) if one.strip()]
        aliases = list(dict.fromkeys(aliases)) or [title]
        return {
            "qq": qq,
            "name": str(item.get("name") or "").strip()[:24],
            "title": title,
            "aliases": list(dict.fromkeys(aliases)),
            "added_at": to_int(item.get("added_at", 0), 0),
            "updated": to_int(item.get("updated", 0), 0),
        }

    def _master_items(self) -> list[dict[str, Any]]:
        return list(self._master_load()["masters"])

    # ---------------- 查询 -----------------------------

    def _master_clean_title(self, raw: Any, fallback: str = "") -> str:
        text = re.sub(r"\s+", " ", str(raw or "")).strip()
        text = text.strip("「」“”\"'").strip()
        text = text.split("/")[0].split("｜")[0].split("、")[0].strip()
        if not text:
            text = str(fallback or self._master_cfg("default_title", "主人") or "主人")
        return text[:_TITLE_LIMIT]

    def _master_find(self, qq: str) -> dict[str, Any] | None:
        target = re.sub(r"\D", "", str(qq or ""))
        if not target:
            return None
        for item in self._master_items():
            if item["qq"] == target:
                return item
        return None

    def _master_enabled(self) -> bool:
        return bool(self._master_cfg("enabled", True))

    def _master_find_by_name(self, name: str) -> tuple[str, str] | None:
        """按称呼 / 名字 / QQ 找主人，返回（称呼, QQ）。"""
        if not self._master_enabled():
            return None
        key = str(name or "").strip().lstrip("@").strip()
        if not key:
            return None
        items = self._master_items()
        for item in items:
            if key == item["title"] or key in item["aliases"]:
                return item["title"], item["qq"]
        if key.isdigit():
            for item in items:
                if item["qq"] == key:
                    return item["title"], item["qq"]
        for item in items:
            if item["name"] and (key == item["name"] or key in item["name"] or item["name"] in key):
                return item["title"], item["qq"]
        for item in items:
            for alias in item["aliases"]:
                if key in alias or alias in key:
                    return item["title"], item["qq"]
        return None

    def _master_title_for(self, qq: str) -> str:
        if not self._master_enabled():
            return ""
        item = self._master_find(str(qq or ""))
        return item["title"] if item else ""

    def _master_sender_qq(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id() or "")
        except Exception:
            return ""

    def _master_is_master(self, event: Any) -> bool:
        if not self._master_enabled():
            return False
        qq = self._master_sender_qq(event) if hasattr(event, "get_sender_id") else str(event or "")
        return bool(qq and self._master_find(qq))

    def _master_can_manage(self, event: AstrMessageEvent) -> bool:
        if self._master_is_master(event):
            return True
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    # ---------------- 增删改 -----------------------------

    def _master_add(
        self,
        qq: str,
        title: str = "",
        name: str = "",
        added_by: str = "",
    ) -> tuple[str, dict[str, Any] | None]:
        """添加/更新一位主人。返回（结果码, 记录）。

        结果码：added / updated / exists / limit / invalid
        """
        target = re.sub(r"\D", "", str(qq or ""))
        if len(target) < 5:
            return "invalid", None
        clean_title = self._master_clean_title(title, "")
        existing = self._master_find(target)
        now = int(time.time())
        if existing:
            changed = False
            if title:
                parts = [p.strip() for p in re.split(r"[/|｜、]+", str(title)) if p.strip()]
                aliases = [p[:_TITLE_LIMIT] for p in parts] or [clean_title]
                existing["title"] = clean_title
                existing["aliases"] = list(dict.fromkeys(aliases))
                changed = True
            if name:
                existing["name"] = str(name).strip()[:24]
                changed = True
            if changed:
                existing["updated"] = now
                self._master_save()
                return "updated", existing
            return "exists", existing

        limit = max(1, to_int(self._master_cfg("max_masters", 8), 8))
        if len(self._master_items()) >= limit:
            return "limit", None

        parts = [p.strip() for p in re.split(r"[/|｜、]+", str(title)) if p.strip()]
        aliases = [p[:_TITLE_LIMIT] for p in parts] or [clean_title]
        entry = {
            "qq": target,
            "name": str(name or "").strip()[:24],
            "title": clean_title,
            "aliases": list(dict.fromkeys(aliases)),
            "added_at": now,
            "updated": now,
            "added_by": str(added_by or "").strip(),
        }
        data = self._master_load()
        data["masters"].append(entry)
        self._master_save()
        return "added", entry

    def _master_update(self, qq: str, title: str = "", name: str = "") -> bool:
        item = self._master_find(qq)
        if not item:
            return False
        if title:
            parts = [p.strip() for p in re.split(r"[/|｜、]+", str(title)) if p.strip()]
            item["title"] = self._master_clean_title(title, item["title"])
            item["aliases"] = list(dict.fromkeys(
                [p[:_TITLE_LIMIT] for p in parts] or [item["title"]]
            ))
        if name:
            item["name"] = str(name).strip()[:24]
        item["updated"] = int(time.time())
        self._master_save()
        return True

    def _master_remove(self, qq: str) -> bool:
        target = re.sub(r"\D", "", str(qq or ""))
        data = self._master_load()
        kept = [item for item in data["masters"] if item["qq"] != target]
        if len(kept) == len(data["masters"]):
            return False
        data["masters"] = kept
        self._master_save()
        return True

    def _master_clear(self) -> int:
        data = self._master_load()
        count = len(data["masters"])
        data["masters"] = []
        self._master_save()
        return count

    def _master_stats(self) -> dict[str, Any]:
        items = self._master_items()
        return {
            "enabled": bool(self._master_cfg("enabled", True)),
            "inject_enabled": bool(self._master_cfg("inject_enabled", True)),
            "count": len(items),
            "max": max(1, to_int(self._master_cfg("max_masters", 8), 8)),
            "default_title": self._master_clean_title("", ""),
        }

    def _master_display(self) -> list[dict[str, Any]]:
        return [
            {
                "qq": item["qq"],
                "name": item["name"],
                "title": item["title"],
                "aliases": item["aliases"],
                "added_at": item["added_at"],
                "updated": item["updated"],
            }
            for item in self._master_items()
        ]

    # ---------------- 指令解析 -----------------------------

    def _master_parse(self, event: AstrMessageEvent, command: str) -> tuple[str, str, str]:
        """解析指令里的目标 QQ 和称呼，返回（QQ, 称呼, @到的名字）。"""
        at_qq, at_name = "", ""
        try:
            for comp in event.get_messages():
                if isinstance(comp, At):
                    qq = str(getattr(comp, "qq", "") or "")
                    if qq.isdigit():
                        at_qq = qq
                        at_name = str(getattr(comp, "name", "") or "")
                    else:
                        # @全体成员 之类，当作非法目标，避免误走自助认主
                        at_qq = "0"
                    break
        except Exception:
            pass

        text = str(getattr(event, "message_str", "") or "").strip()
        text = text.lstrip("/／").strip()
        index = text.find(command)
        if index >= 0:
            text = text[index + len(command):].strip()
        text = re.sub(r"\[CQ:[^\]]+\]", " ", text).strip()

        if at_qq:
            text = re.sub(r"^@[^\s]+", "", text).strip()
            if at_name and text.startswith(at_name):
                text = text[len(at_name):].strip()
            return at_qq, text, at_name

        match = re.match(r"^(\d{5,})\s+(.*)$", text, re.S)
        if match:
            return match.group(1), match.group(2).strip(), ""
        if text.isdigit() and len(text) >= 5:
            return text, "", ""
        return "", text, ""

    def _master_reply_add(self, code: str, entry: dict[str, Any] | None) -> str:
        if code == "invalid":
            return "QQ 号看起来不对，要用 5 位以上的数字。"
        if code == "limit":
            limit = max(1, to_int(self._master_cfg("max_masters", 8), 8))
            return f"主人已经有 {limit} 位了，先 /取消认主 掉一位再加吧。"
        who = entry["qq"] if entry else ""
        title = entry["title"] if entry else "主人"
        if code == "added":
            return f"记住了，{who} 以后就是我的主人，我称呼 TA 为「{title}」。"
        if code == "updated":
            return f"好，以后我称呼 {who} 为「{title}」。"
        return f"{who} 已经是我的主人了，称呼是「{title}」。"

    # ---------------- 聊天注入 -----------------------------

    @filter.on_llm_request()
    async def master_inject(self, event: AstrMessageEvent, req: Any):
        """把主人名单与称呼拼进 system_prompt，让她知道该叫谁什么。"""
        if not bool(self._master_cfg("enabled", True)):
            return
        if not bool(self._master_cfg("inject_enabled", True)):
            return
        if getattr(req, "_master_injected", False):
            return
        try:
            items = self._master_items()
            if not items:
                return
            sender = self._master_sender_qq(event)
            current = self._master_find(sender)
            if current:
                block = (
                    "【认主关系】\n"
                    f"当前和你说话的是你的主人：{current['name'] or current['qq']}"
                    f"（QQ {current['qq']}），你要称呼 TA 为「{current['title']}」。\n"
                    "TA 是你的主人，可以亲近、撒娇、听话，直接用这个称呼开口，"
                    "别叫「用户」「对方」「您」这种生分的说法。"
                )
            else:
                lines = []
                for item in items:
                    label = item["name"] or item["qq"]
                    lines.append(f"- {label}（QQ {item['qq']}），你称呼 TA 为「{item['title']}」")
                block = (
                    "【认主关系】\n你的主人有：\n"
                    + "\n".join(lines)
                    + "\n当前说话的人不是你的主人：对 TA 用平常规矩和语气就行，"
                    "别把「主人」这个称呼用在 TA 身上，也别对着 TA 自称奴仆或下人。"
                )
            existing = str(getattr(req, "system_prompt", "") or "")
            req.system_prompt = f"{existing}\n\n{block}" if existing else block
            try:
                req._master_injected = True
            except Exception:
                pass
        except Exception as exc:
            logger.warning(f"[master] 注入认主关系失败: {exc}")

    # ---------------- 斜杠指令 -----------------------------

    @filter.command("认主")
    async def cmd_master_claim(self, event: AstrMessageEvent):
        if not bool(self._master_cfg("enabled", True)):
            yield event.plain_result("认主功能关着。")
            return

        target, rest, at_name = self._master_parse(event, "认主")
        sender = self._master_sender_qq(event)
        if not sender:
            yield event.plain_result("没拿到你的 QQ 号，认不了。")
            return

        if not target:
            # 自己认主
            existing = self._master_find(sender)
            if existing:
                if rest:
                    if self._master_update(sender, rest):
                        entry = self._master_find(sender)
                        yield event.plain_result(self._master_reply_add("updated", entry))
                    else:
                        yield event.plain_result("改称呼失败了，稍后再试。")
                else:
                    yield event.plain_result(
                        f"你已经是我的主人了，我称呼你为「{existing['title']}」。"
                        "想换称呼可以发 /改称呼 新称呼。"
                    )
                return
            if self._master_items():
                names = "、".join(item["title"] for item in self._master_items())
                yield event.plain_result(
                    f"已经有主人了（{names}），让现有主人或管理员发 /认主 @你 称呼 来加你。"
                )
                return
            if not bool(self._master_cfg("allow_self_claim_when_empty", True)) and not self._master_can_manage(event):
                yield event.plain_result(
                    "还没有主人，但自助认主关着；让管理员发 /认主 @你 称呼 来认。"
                )
                return
            name = ""
            try:
                name = str(event.get_sender_name() or "").strip()
            except Exception:
                pass
            code, entry = self._master_add(sender, rest, name, added_by=sender)
            yield event.plain_result(self._master_reply_add(code, entry))
            return

        # 给别人认主
        if not self._master_can_manage(event):
            yield event.plain_result("只有主人或管理员才能给别人认主。")
            return
        code, entry = self._master_add(target, rest, at_name, added_by=sender)
        yield event.plain_result(self._master_reply_add(code, entry))

    @filter.command("主人列表")
    async def cmd_master_list(self, event: AstrMessageEvent):
        items = self._master_items()
        if not items:
            yield event.plain_result("还没有认主。发 /认主 称呼 就可以认她当主人。")
            return
        sender = self._master_sender_qq(event)
        lines = ["现在的主人："]
        for index, item in enumerate(items, 1):
            label = item["name"] or item["qq"]
            mark = "（你）" if item["qq"] == sender else ""
            lines.append(f"{index}. {label}（QQ {item['qq']}）→ 叫她称呼「{item['title']}」{mark}")
        lines.append("主人可以发 /认主 @某人 称呼、/改称呼 @某人 新称呼、/取消认主 @某人。")
        yield event.plain_result("\n".join(lines))

    @filter.command("改称呼")
    async def cmd_master_rename(self, event: AstrMessageEvent):
        if not self._master_can_manage(event):
            yield event.plain_result("只有主人或管理员才能改称呼。")
            return
        target, rest, _ = self._master_parse(event, "改称呼")
        if not target:
            target = self._master_sender_qq(event)
        if not rest:
            yield event.plain_result("用法：/改称呼 @某人 新称呼，或者直接 /改称呼 新称呼 改自己。")
            return
        if not self._master_update(target, rest):
            yield event.plain_result(f"{target} 还不是主人，先用 /认主 加进来。")
            return
        entry = self._master_find(target)
        yield event.plain_result(f"好，以后我称呼 {target} 为「{entry['title']}」。")

    @filter.command("取消认主")
    async def cmd_master_remove(self, event: AstrMessageEvent):
        if not self._master_can_manage(event):
            yield event.plain_result("只有主人或管理员才能取消认主。")
            return
        target, _, _ = self._master_parse(event, "取消认主")
        if not target:
            target = self._master_sender_qq(event)
        item = self._master_find(target)
        if not item:
            yield event.plain_result(f"{target} 本来就不是主人。")
            return
        self._master_remove(target)
        yield event.plain_result(f"好，{target}（{item['title']}）不再是我的主人了。")

    @filter.command("认主帮助")
    async def cmd_master_help(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)
