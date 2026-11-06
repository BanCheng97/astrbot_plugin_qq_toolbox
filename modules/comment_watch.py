"""AstrBot 插件：大肥鱼空间评论监控与自动回复。

后台定时拉取最近说说的评论，发现新评论后按人格生成回复并直接发到空间：

- 首次启用只把现有评论记为基线，不回复历史评论，避免一上线就回一遍；
- 用「说说 tid + 评论 tid」去重，存本地 JSON，同一条评论不会重复回；
- 遵守 toolbox 的全局冷却与每日评论上限（daily_comment_limit），不会刷屏；
- 可配置只回白名单或任何人，默认不回自己发的评论；
- 回复后 / 失败时可选私聊通知主人一声。

数据存 ``plugin_data/astrbot_plugin_qq_toolbox/comment_watch.json``。
所有方法用 ``cw_`` 前缀，避免多继承时和其他模块重名。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

from ._qzone import clean_text
from .base import SuiteBase, load_json, save_json, to_bool, to_int

NAME = "astrbot_plugin_qq_toolbox"

HELP_TEXT = """评论监控相关：
/评论监控  查看当前状态（是否启用、基线、已回条数、上次检查）

平时由后台定时拉取空间新评论并自动回复，开关和频率在插件配置的「评论监控」里调整。"""


class _BgEvent:
    """后台定时用的最小事件代理。

    空间接口（取 Cookie / 拉列表 / 发回复）只用到 event 的 ``get_self_id``
    和 ``bot`` 两个属性：self_id 仅作会话缓存 key，bot 为 None 时
    ``_toolbox_client`` 会退化为遍历平台管理器找 OneBot 客户端，所以后台
    没有真实消息事件也能正常调用空间接口。
    """

    def __init__(self, self_id: str = "background") -> None:
        self._self_id = self_id
        self.bot = None

    def get_self_id(self) -> str:
        return self._self_id


class CommentWatchMixin(SuiteBase):
    def setup_comment_watch(self) -> None:
        self._cw_path = self.data_file(NAME, "comment_watch.json")
        self._cw_state: dict[str, Any] | None = None
        self._cw_task: asyncio.Task | None = None
        self._cw_dirty = False

    def _cw_cfg(self, key: str, default: Any = None) -> Any:
        return self.cfg("comment_watch", key, default)

    # ---------------- 存取 -----------------------------

    def _cw_load(self) -> dict[str, Any]:
        if self._cw_state is not None:
            return self._cw_state
        raw = load_json(self._cw_path, {}) if self._cw_path else {}
        if not isinstance(raw, dict):
            raw = {}
        if not isinstance(raw.get("seen"), dict):
            raw["seen"] = {}
        if not isinstance(raw.get("history"), list):
            raw["history"] = []
        raw.setdefault("baseline_done", False)
        self._cw_state = raw
        return raw

    def _cw_save(self) -> None:
        self._cw_dirty = False
        if not self._cw_path:
            return
        data = self._cw_load()
        data["updated"] = int(time.time())
        save_json(self._cw_path, data)

    def _cw_mark_seen(self, key: str, info: dict[str, Any]) -> None:
        data = self._cw_load()
        seen = data["seen"]
        seen[key] = {"t": int(time.time()), **info}
        # 只保留最近 1000 条，避免文件无限增长
        if len(seen) > 1000:
            kept = sorted(seen.items(), key=lambda kv: kv[1].get("t", 0))[-1000:]
            data["seen"] = dict(kept)
        self._cw_dirty = True

    def _cw_record_history(
        self, feed_tid: str, comment: dict, text: str, status: str
    ) -> None:
        data = self._cw_load()
        history = data.setdefault("history", [])
        history.append(
            {
                "t": int(time.time()),
                "date": time.strftime("%Y-%m-%d"),
                "feed": feed_tid,
                "uin": str(comment.get("uin") or ""),
                "nick": clean_text(comment.get("nickname") or "", 16),
                "content": clean_text(comment.get("content"), 60),
                "reply": text,
                "status": status,
            }
        )
        data["history"] = history[-200:]
        self._cw_dirty = True

    # ---------------- 配置辅助 -----------------------------

    def _cw_owner_umo(self) -> str:
        return str(self._toolbox_cfg("owner_session") or "").strip()

    def _cw_quiet(self) -> bool:
        start = to_int(self._cw_cfg("quiet_start", 0), 0) % 24
        end = to_int(self._cw_cfg("quiet_end", 0), 0) % 24
        if start == end:
            return False
        hour = time.localtime().tm_hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    # ---------------- 评论发现 -----------------------------

    @staticmethod
    def _cw_key(feed_tid: str, comment_tid: str) -> str:
        return f"{feed_tid}|{comment_tid}"

    def _cw_should_reply(self, comment: dict, sess_uin: str) -> bool:
        uin = str(comment.get("uin") or "")
        if to_bool(self._cw_cfg("skip_self", True)) and uin and uin == sess_uin:
            return False
        if not str(comment.get("content") or "").strip():
            return False
        if to_bool(self._cw_cfg("reply_anyone", True)):
            return True
        whitelist = self._cw_cfg("whitelist_uins", []) or []
        allowed = {str(item).strip() for item in whitelist if str(item).strip()}
        return uin in allowed

    async def _cw_collect_new(
        self, bg_event: _BgEvent, sess: Any
    ) -> list[tuple[str, dict, dict]]:
        """拉最近说说，返回未处理过的新评论 ``[(feed_tid, feed, comment), ...]``。"""
        count = max(1, min(to_int(self._cw_cfg("list_count", 10), 10), 20))
        items = await self._msg_list(sess, count, bg_event)
        seen = self._cw_load()["seen"]
        new: list[tuple[str, dict, dict]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            feed_tid = str(item.get("tid") or "")
            if not feed_tid:
                continue
            comments = [c for c in (item.get("commentlist") or []) if isinstance(c, dict)]
            for comment in comments:
                comment_tid = str(comment.get("tid") or "")
                if not comment_tid:
                    continue
                if self._cw_key(feed_tid, comment_tid) in seen:
                    continue
                new.append((feed_tid, item, comment))
        return new

    # ---------------- 回复生成 -----------------------------

    def _cw_provider(self, umo: str) -> Any:
        """拿写回复用的模型：优先 comment_watch.provider_id，否则用当前会话模型。"""
        provider = None
        provider_id = str(self._cw_cfg("provider_id", "") or "").strip()
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
            logger.warning(f"[comment_watch] 获取对话模型失败: {exc}")
            return None
        if provider is None or not hasattr(provider, "text_chat"):
            return None
        return provider

    async def _cw_system_prompt(self, umo: str) -> str:
        """写回复用的人格：优先 comment_watch.persona_prompt，否则读 AstrBot 人格。"""
        override = str(self._cw_cfg("persona_prompt", "") or "").strip()
        if override:
            return override
        if not to_bool(self._cw_cfg("use_persona", True)):
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

    def _cw_reply_prompt(self, feed: dict, comment: dict) -> str:
        now = time.strftime("%Y-%m-%d %H:%M")
        nick = clean_text(comment.get("nickname") or comment.get("name") or "对方", 16)
        feed_text = clean_text(feed.get("content"), 80) or "(无正文)"
        comment_text = clean_text(comment.get("content"), 100) or "(无内容)"
        limit = max(10, to_int(self._cw_cfg("reply_max_length", 40), 40))
        return "\n".join(
            [
                f"现在是 {now}。你在自己的 QQ 空间里，{nick} 评论了你的说说。",
                f"你的说说是：「{feed_text}」",
                f"TA 的评论是：「{comment_text}」",
                f"请用你自己的口吻回复 TA 这条评论：一到两句，不超过 {limit} 字，"
                "像真人随手回的，有情绪起伏，不要播音腔、不要念稿感；"
                "不要提插件、模型、系统提示或接口，不要加引号标题，只输出回复正文。",
            ]
        )

    async def _cw_compose(self, owner_umo: str, feed: dict, comment: dict) -> str:
        limit = max(10, to_int(self._cw_cfg("reply_max_length", 40), 40))
        provider = self._cw_provider(owner_umo)
        if provider is None:
            return ""
        system_prompt = await self._cw_system_prompt(owner_umo)
        prompt = self._cw_reply_prompt(feed, comment)
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                system_prompt=system_prompt or None,
            )
        except Exception as exc:
            logger.warning(f"[comment_watch] 生成回复失败: {exc}")
            return ""
        return self._proactive_clean(str(getattr(resp, "completion_text", "") or ""), limit)

    def _cw_fallback(self) -> str:
        limit = max(10, to_int(self._cw_cfg("reply_max_length", 40), 40))
        text = str(self._cw_cfg("fallback_text", "") or "").strip()
        return self._proactive_clean(text, limit) if text else ""

    # ---------------- 通知主人 -----------------------------

    async def _cw_notify_owner(self, text: str) -> None:
        umo = self._cw_owner_umo()
        if not umo:
            return
        try:
            await self._proactive_send(umo, text)
        except Exception as exc:
            logger.warning(f"[comment_watch] 通知主人失败: {exc}")

    # ---------------- 主循环 -----------------------------

    async def _cw_tick(self) -> None:
        if not to_bool(self._cw_cfg("enabled", False)):
            return
        if not to_bool(self._toolbox_cfg("enabled", True)):
            return
        if self._cw_quiet():
            return
        if self.platform_client() is None:
            logger.debug("[comment_watch] 无可用 OneBot 客户端，跳过本轮")
            return

        bg_event = _BgEvent()
        try:
            sess = await self._get_session(bg_event)
        except Exception as exc:
            logger.warning(f"[comment_watch] 获取空间会话失败: {exc}")
            return

        data = self._cw_load()
        try:
            new_comments = await self._cw_collect_new(bg_event, sess)
        except Exception as exc:
            logger.warning(f"[comment_watch] 拉取评论失败: {exc}")
            data["last_check"] = int(time.time())
            self._cw_save()
            return
        data["last_check"] = int(time.time())
        self._cw_dirty = True

        # 首次启用：把当前所有评论记为基线，不回复历史评论
        if not data.get("baseline_done"):
            for feed_tid, _feed, comment in new_comments:
                key = self._cw_key(feed_tid, str(comment.get("tid") or ""))
                self._cw_mark_seen(
                    key,
                    {
                        "feed": feed_tid,
                        "uin": str(comment.get("uin") or ""),
                        "nick": clean_text(comment.get("nickname") or "", 16),
                        "content": clean_text(comment.get("content"), 60),
                        "status": "baseline",
                    },
                )
            data["baseline_done"] = True
            self._cw_dirty = True
            logger.info(
                f"[comment_watch] 基线已建立，记录 {len(new_comments)} 条现有评论，"
                "后续新评论才会自动回复"
            )
            self._cw_save()
            return

        if not new_comments:
            self._cw_save()
            return

        owner_umo = self._cw_owner_umo()
        sess_uin = sess.uin
        replied = 0
        for feed_tid, feed, comment in new_comments:
            comment_tid = str(comment.get("tid") or "")
            key = self._cw_key(feed_tid, comment_tid)

            if not self._cw_should_reply(comment, sess_uin):
                self._cw_mark_seen(
                    key,
                    {
                        "feed": feed_tid,
                        "uin": str(comment.get("uin") or ""),
                        "nick": clean_text(comment.get("nickname") or "", 16),
                        "content": clean_text(comment.get("content"), 60),
                        "status": "skipped",
                    },
                )
                continue

            limited = self._limit("comment")
            if limited:
                logger.info(f"[comment_watch] 频率限制，剩余新评论留到下一轮：{limited}")
                break

            text = await self._cw_compose(owner_umo, feed, comment)
            if not text:
                text = self._cw_fallback()
            if not text:
                # 模型和兜底都没产出：不标记 seen，下一轮再试，但记日志避免无提示
                logger.warning("[comment_watch] 回复内容为空，跳过该评论（下轮重试）")
                continue

            try:
                await self._reply(bg_event, sess, feed_tid, comment, text)
                self._mark_write(bg_event, "comment")
            except Exception as exc:
                logger.error(f"[comment_watch] 回复评论失败: {exc}")
                self._cw_mark_seen(
                    key,
                    {
                        "feed": feed_tid,
                        "uin": str(comment.get("uin") or ""),
                        "nick": clean_text(comment.get("nickname") or "", 16),
                        "content": clean_text(comment.get("content"), 60),
                        "reply": text,
                        "status": "failed",
                        "error": str(exc)[:120],
                    },
                )
                self._cw_record_history(feed_tid, comment, text, "failed")
                if to_bool(self._cw_cfg("notify_owner_on_fail", True)):
                    nick = clean_text(comment.get("nickname") or "某人", 16)
                    await self._cw_notify_owner(f"空间有条评论没回出去（{nick}），等会儿我再试。")
                continue

            self._cw_mark_seen(
                key,
                {
                    "feed": feed_tid,
                    "uin": str(comment.get("uin") or ""),
                    "nick": clean_text(comment.get("nickname") or "", 16),
                    "content": clean_text(comment.get("content"), 60),
                    "reply": text,
                    "status": "replied",
                },
            )
            self._cw_record_history(feed_tid, comment, text, "replied")
            replied += 1
            if to_bool(self._cw_cfg("notify_owner_on_reply", True)):
                nick = clean_text(comment.get("nickname") or "某人", 16)
                await self._cw_notify_owner(f"已回 {nick} 的空间评论：{text}")

        if self._cw_dirty:
            self._cw_save()
        if replied:
            logger.info(f"[comment_watch] 本轮自动回复了 {replied} 条新评论")

    # ---------------- 状态 -----------------------------

    def _cw_stats(self) -> dict[str, Any]:
        data = self._cw_load()
        history = [h for h in data.get("history") or [] if isinstance(h, dict)]
        today = time.strftime("%Y-%m-%d")
        last = int(data.get("last_check") or 0)
        return {
            "enabled": to_bool(self._cw_cfg("enabled", False)),
            "baseline_done": bool(data.get("baseline_done")),
            "seen": len(data.get("seen") or {}),
            "replied_today": sum(
                1 for h in history if h.get("date") == today and h.get("status") == "replied"
            ),
            "last_check": last,
            "last_check_text": (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last)) if last else "还没检查过"
            ),
            "task_running": bool(self._cw_task and not self._cw_task.done()),
        }

    # ---------------- 循环与生命周期 -----------------------------

    async def _cw_loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            try:
                await self._cw_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[comment_watch] 评论监控循环出错: {exc}")
            interval = max(1, to_int(self._cw_cfg("poll_minutes", 10), 10)) * 60
            await asyncio.sleep(interval)

    async def _cw_start(self) -> None:
        if self._cw_task and not self._cw_task.done():
            return
        self._cw_task = asyncio.create_task(self._cw_loop())

    async def _cw_stop(self) -> None:
        task = self._cw_task
        self._cw_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(f"[comment_watch] 停止评论监控任务失败: {exc}")
        if self._cw_dirty:
            self._cw_save()

    # ---------------- 指令 -----------------------------

    @filter.command("评论监控")
    async def cmd_comment_watch(self, event: AstrMessageEvent):
        if not self._is_explicit_command(event):
            return
        s = self._cw_stats()
        lines = [
            "评论监控状态：",
            f"启用：{'开' if s['enabled'] else '关'}",
            f"基线：{'已建立' if s['baseline_done'] else '未建立（首次运行会先记录现有评论）'}",
            f"已记录评论：{s['seen']} 条",
            f"今天已回复：{s['replied_today']} 条",
            f"上次检查：{s['last_check_text']}",
            f"后台任务：{'运行中' if s['task_running'] else '未运行'}",
        ]
        yield event.plain_result("\n".join(lines))
