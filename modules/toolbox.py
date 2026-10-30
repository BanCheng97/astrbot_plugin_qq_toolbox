"""AstrBot 插件：QQ 资料与空间工具箱（大肥鱼版）

双通道设计：
1. LLM 工具通道（主路径）：把改昵称、改签名、发说说、点赞/评论/回复/删除说说、名片赞做成
   function calling 工具，由模型用大白话触发，返回的是「人格化提示语」而不是
   固定模板，因此对话里没有人机感。
2. 斜杠指令通道（兜底）：/改头像 /改昵称 /改签名 /名片赞 /发说说 /说说列表
   /赞说说 /评说说 /回复评论 /删说说，方便手动操作。

空间接口部分参考了 Zhalslar/astrbot_plugin_qzone 的实现：
- Cookie 上下文缓存（uin / skey / p_skey），按 TTL 自动刷新
- 登录态失效（401 / -3000）自动重登并重试
- 非标准 JSON / JSONP 响应解析
- 发说说支持附图（cgi_upload_image + pic_bo/richval）

依赖 NapCat / LLOneBot 等支持扩展 API 的 OneBot v11 实现（aiocqhttp 适配器）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator
from urllib.parse import unquote, urlparse

import aiohttp

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image

from .base import SuiteBase, to_int

# 沿用旧插件的数据目录名，历史说说与操作记录可以直接继承
NAME = "astrbot_plugin_qq_toolbox"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)

BASE_URL = "https://user.qzone.qq.com"

# 空间接口（与参考插件保持一致）
API_UPLOAD = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
API_PUBLISH = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
)
API_DELETE = (
    "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6"
)
API_COMMENT = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
)
API_REPLY = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
)
API_LIST = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
)
API_LIKE = (
    "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
)

# 登录态失效的错误码
CODE_LOGIN_EXPIRED = -3000
CODE_IMAGE_EXPIRED = -100

# 主人话里出现这些词，就提醒她这次可以顺手改个签名
MOODS = {
    "在夸你": ["真棒", "真厉害", "好厉害", "你厉害", "太强了", "好聪明", "真聪明", "真乖", "好可爱", "真可爱", "最喜欢你", "夸你"],
    "在说你，你有点委屈": ["好笨", "真笨", "笨死", "笨蛋", "讨厌你", "烦死", "没用", "闭嘴", "滚", "傻", "凶你", "不理你"],
    "说今天是他的生日": ["我生日", "我的生日", "生日快乐", "过生日"],
}

DEFAULT_POST_PROMPT = (
    "写一条你此刻的 QQ 空间说说，用你自己的口吻，40 字以内，可以带一点情绪、吐槽或感慨。"
)

HELP_TEXT = """QQ 工具箱指令（也可以直接用大白话跟她说，不必用指令）：
/改头像（消息附图片）  修改机器人头像
/改昵称 <昵称>  修改机器人 QQ 昵称
/改签名 <内容>  修改机器人个性签名
/名片赞 [@某人] [次数]  给名片点赞
/发说说 <内容>（可附图）  发布空间说说
/ai说说 [主题]  让她用模型自己写一条说说（也可用 /写说说）
/说说列表 [数量]  查看最近说说（带序号和评论序号）
/赞说说 [序号]  给说说点赞
/评说说 [序号] <内容>  评论说说
/回复评论 <说说序号> <评论序号> <内容>  回复某条评论
/删说说 [序号]  删除说说"""


def hash33(seed: str) -> int:
    """QQ 空间 g_tk 计算。"""
    value = 5381
    for char in seed:
        value += (value << 5) + ord(char)
    return value & 0x7FFFFFFF


def parse_cookies(raw: Any) -> dict[str, str]:
    """把字符串 / 列表 / 字典形态的 Cookie 统一成字典。"""
    jar: dict[str, str] = {}
    if not raw:
        return jar
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("name") is not None:
                jar[str(item["name"])] = str(item.get("value", ""))
        return jar
    if isinstance(raw, str):
        for part in raw.split(";"):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                key, _, val = part.partition("=")
                jar[key.strip()] = val.strip()
            else:
                jar[part] = ""
    return jar


def extract_cookies(resp: Any) -> str:
    """从 get_cookies / get_credentials 的返回中取出 Cookie 字符串。"""
    if not isinstance(resp, dict):
        return ""
    data = resp.get("data", resp)
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, dict):
        for key in ("cookies", "cookie"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, list):
                parts = [
                    f"{item.get('name')}={item.get('value', '')}"
                    for item in val
                    if isinstance(item, dict) and item.get("name") is not None
                ]
                if parts:
                    return "; ".join(parts)
    return ""


def parse_json(text: str) -> dict:
    """解析 JSONP / 非标准 JSON 响应。"""
    text = (text or "").strip()
    if not text:
        raise RuntimeError("空间接口返回内容为空")
    if "data error" in text[:200].lower():
        raise RuntimeError("空间接口返回 data error：登录态或 g_tk 可能失效")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError(f"空间接口返回内容异常：{text[:120]}")
    payload = text[start:end + 1].replace("undefined", "null")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        try:
            import json5  # type: ignore

            data = json5.loads(payload)
        except Exception as exc:
            snippet = re.sub(r"\s+", " ", text)[:160]
            raise RuntimeError(f"空间接口返回的不是 JSON：{snippet}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("空间接口返回的不是对象")
    return data


def split_sources(raw: str) -> list[str]:
    """把逗号/空格/换行分隔的图片地址拆开。"""
    return [item for item in re.split(r"[\s,，;]+", raw or "") if item]


def clean_text(value: Any, limit: int = 80) -> str:
    """去掉空间正文里的 HTML / 表情标记，压成一行。"""
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    text = re.sub(r"\[em\].*?\[/em\]", "", text)
    text = re.sub(r"\{.*?\}", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + "…" if len(text) > limit else text


@dataclass
class QZoneSession:
    """一次空间请求所需的登录上下文。"""

    uin: str
    skey: str = ""
    p_skey: str = ""
    csrf_token: str = ""

    @property
    def g_tk(self) -> str:
        seed = self.p_skey or self.skey
        if seed:
            return str(hash33(seed))
        if self.csrf_token.isdigit():
            return self.csrf_token
        return "0"

    def cookies(self) -> dict[str, str]:
        return {
            "uin": f"o{self.uin}",
            "skey": self.skey,
            "p_skey": self.p_skey,
        }

    def headers(self, url: str) -> dict[str, str]:
        host = urlparse(url).hostname or "user.qzone.qq.com"
        return {
            "User-Agent": UA,
            "Referer": f"{BASE_URL}/{self.uin}",
            "Origin": BASE_URL,
            "Host": host,
            "Connection": "keep-alive",
        }


class ToolboxMixin(SuiteBase):
    # 写操作频控：每日上限（配置键, 默认值, 中文名）
    LIMITS = {
        "like": ("daily_like_limit", 20, "说说点赞"),
        "comment": ("daily_comment_limit", 10, "说说评论/回复"),
        "delete": ("daily_delete_limit", 5, "删除说说"),
        "card_like": ("daily_card_like_limit", 30, "名片赞"),
    }

    def setup_toolbox(self) -> None:
        # uin -> 最近一次说说列表
        self._toolbox_last_list: dict[str, list[dict]] = {}
        # self_id -> (会话, 刷新时间)
        self._toolbox_sess_cache: dict[str, tuple[QZoneSession, float]] = {}
        self._toolbox_sess_locks: dict[str, asyncio.Lock] = {}
        self._toolbox_http: aiohttp.ClientSession | None = None
        # 上一次写操作时间，用于全局冷却
        self._toolbox_last_write = 0.0
        try:
            self._toolbox_history_path = os.path.join(
                self.module_data_dir(NAME), "history.json"
            )
        except Exception as exc:
            logger.warning(f"[qq_toolbox] 获取数据目录失败，改用当前目录: {exc}")
            self._toolbox_history_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "history.json"
            )

    def _toolbox_cfg(self, key, default=None):
        return self.cfg("toolbox", key, default)

    async def _toolbox_terminate(self):
        """插件卸载时释放 HTTP 连接池。"""
        if self._toolbox_http is not None and not self._toolbox_http.closed:
            await self._toolbox_http.close()
        self._toolbox_http = None

    # ---------------- 历史记录 / 配额 -----------------------------

    def _toolbox_load(self) -> list[dict]:
        try:
            with open(self._toolbox_history_path, encoding="utf-8") as fp:
                data = json.load(fp)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _toolbox_save(self, history: list[dict]) -> None:
        os.makedirs(os.path.dirname(self._toolbox_history_path), exist_ok=True)
        with open(self._toolbox_history_path, "w", encoding="utf-8") as fp:
            json.dump(history[-300:], fp, ensure_ascii=False, indent=1)

    def _toolbox_record(self, entry: dict) -> None:
        history = self._toolbox_load()
        history.append(entry)
        self._toolbox_save(history)

    def _toolbox_count_today(self, kind: str, **match: Any) -> int:
        today = time.strftime("%Y-%m-%d")
        total = 0
        for entry in self._toolbox_load():
            if entry.get("type", "signature") != kind or entry.get("date") != today:
                continue
            if all(entry.get(key) == value for key, value in match.items()):
                total += 1
        return total

    def _cooldown_remaining(self) -> int:
        """距下一次允许写操作还剩几秒。"""
        gap = to_int(self._toolbox_cfg("cooldown_seconds", 15), 15)
        if gap <= 0:
            return 0
        elapsed = time.monotonic() - self._toolbox_last_write
        if elapsed >= gap:
            return 0
        return max(1, int(gap - elapsed) + 1)

    def _limit(self, kind: str) -> str | None:
        """冷却 + 每日上限，返回 None 表示可以操作。"""
        gap = self._cooldown_remaining()
        if gap > 0:
            return f"刚操作过，{gap} 秒后再试（防空间风控）。"
        config_key, default, label = self.LIMITS[kind]
        limit = to_int(self._toolbox_cfg(config_key, default), default)
        used = self._toolbox_count_today(kind)
        if used >= limit:
            return f"今天的{label}次数用完了（上限 {limit} 次/天）。"
        return None

    def _mark_write(self, event: AstrMessageEvent, kind: str) -> None:
        """记一次写操作：触发全局冷却，并计入当天次数。"""
        self._toolbox_last_write = time.monotonic()
        self._toolbox_record(
            {
                "type": kind,
                "date": time.strftime("%Y-%m-%d"),
                "time": time.strftime("%H:%M:%S"),
                "session": getattr(event, "unified_msg_origin", ""),
            }
        )

    # ---------------- 权限 / 平台 -----------------------------

    def _toolbox_is_owner(self, event: AstrMessageEvent) -> bool:
        owner = str(self._toolbox_cfg("owner_session") or "").strip()
        origin = getattr(event, "unified_msg_origin", "") or ""
        if owner and origin == owner:
            return True
        sender = str(event.get_sender_id())
        extra = {str(item) for item in (self._toolbox_cfg("extra_admins") or [])}
        if sender in extra:
            return True
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        return False

    def _is_aiocqhttp(self, event: AstrMessageEvent) -> bool:
        try:
            return event.get_platform_name() == "aiocqhttp"
        except Exception:
            return False

    def _guard(self, event: AstrMessageEvent) -> str | None:
        if not self._is_aiocqhttp(event):
            return "该插件仅支持 aiocqhttp 适配器（NapCat / LLOneBot）"
        if not self._toolbox_cfg("admin_only", True):
            return None
        if self._toolbox_is_owner(event):
            return None
        return "你没有权限使用该指令（可把自己的 QQ 加进 extra_admins 配置）"

    # ---------------- OneBot 调用 -----------------------------

    def _toolbox_client(self, event: AstrMessageEvent) -> Any:
        bot = getattr(event, "bot", None)
        if bot is not None:
            api = getattr(bot, "api", None) or bot
            if hasattr(api, "call_action"):
                return api
        try:
            for platform in self.context.platform_manager.platform_insts:
                if hasattr(platform, "get_client"):
                    client = platform.get_client()
                    api = getattr(client, "api", None) or client
                    if hasattr(api, "call_action"):
                        return api
        except Exception:
            pass
        return None

    async def _call(self, event: AstrMessageEvent, action: str, **params: Any) -> Any:
        client = self._toolbox_client(event)
        if client is None:
            raise RuntimeError("当前事件拿不到 OneBot 客户端")
        call = getattr(client, "call_action", None)
        if call is None:
            api = getattr(client, "api", None)
            call = getattr(api, "call_action", None) if api else None
        if call is None:
            raise RuntimeError("当前适配器不支持 call_action")
        return await call(action, **params)

    # ---------------- 文本解析 -----------------------------

    def _arg(self, event: AstrMessageEvent, cmd: str) -> str:
        text = (event.message_str or "").strip()
        index = text.find(cmd)
        if index >= 0:
            text = text[index + len(cmd):]
        return text.strip()

    def _find_image(self, event: AstrMessageEvent) -> str:
        for comp in event.get_messages():
            if isinstance(comp, Image):
                for attr in ("url", "file", "path"):
                    value = getattr(comp, attr, None)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        return ""

    def _event_images(self, event: AstrMessageEvent) -> list[str]:
        sources: list[str] = []
        for comp in event.get_messages():
            if not isinstance(comp, Image):
                continue
            for attr in ("url", "file", "path"):
                value = getattr(comp, attr, None)
                if isinstance(value, str) and value.strip():
                    sources.append(value.strip())
                    break
        limit = max(0, to_int(self._toolbox_cfg("max_images", 9), 9))
        return sources[:limit]

    def _split_target_content(self, event: AstrMessageEvent, cmd: str) -> tuple[str, str]:
        rest = self._arg(event, cmd)
        if not rest:
            return "", ""
        parts = rest.split(None, 1)
        first = parts[0]
        if first.isdigit() or re.fullmatch(r"[0-9a-zA-Z_-]{10,}", first):
            return first, (parts[1].strip() if len(parts) > 1 else "")
        return "", rest

    # ---------------- Cookie / 会话缓存 -----------------------------

    def _session_key(self, event: AstrMessageEvent) -> str:
        return str(event.get_self_id() or "default")

    async def _get_session(
        self, event: AstrMessageEvent, force: bool = False
    ) -> QZoneSession:
        key = self._session_key(event)
        lock = self._toolbox_sess_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._toolbox_sess_cache.get(key)
            if cached and not force:
                sess, stamp = cached
                ttl = max(to_int(self._toolbox_cfg("cookie_ttl", 600), 600), 0)
                if ttl <= 0 or time.monotonic() - stamp < ttl:
                    return sess
            sess = await self._load_session(event)
            self._toolbox_sess_cache[key] = (sess, time.monotonic())
            return sess

    async def _invalidate_session(self, event: AstrMessageEvent) -> None:
        self._toolbox_sess_cache.pop(self._session_key(event), None)

    async def _fetch_cookie(
        self, event: AstrMessageEvent, domain: str
    ) -> tuple[str, str]:
        for action in ("get_cookies", "get_credentials"):
            try:
                resp = await self._call(event, action, domain=domain)
            except Exception as exc:
                logger.debug(f"[qq_toolbox] {action}({domain}) 调用失败: {exc}")
                continue
            cookie = extract_cookies(resp)
            data = resp.get("data") if isinstance(resp, dict) else None
            csrf = ""
            if isinstance(data, dict):
                csrf = str(data.get("csrf_token") or data.get("csrfToken") or "")
            if cookie:
                return cookie, csrf
        return "", ""

    async def _load_session(self, event: AstrMessageEvent) -> QZoneSession:
        configured = str(self._toolbox_cfg("qzone_domain", "user.qzone.qq.com") or "")
        domains: list[str] = []
        for item in (configured, "user.qzone.qq.com", "qzone.qq.com"):
            if item and item not in domains:
                domains.append(item)

        cookie, csrf = "", ""
        for item in domains:
            candidate, token = await self._fetch_cookie(event, item)
            if not candidate:
                continue
            jar = parse_cookies(candidate)
            usable = bool(jar.get("p_skey") or jar.get("skey")) or token.isdigit()
            if usable or not cookie:
                cookie, csrf = candidate, token
            if usable:
                break

        if not cookie:
            raise RuntimeError("获取空间 Cookie 失败，请确认 NapCat 已登录且支持 get_cookies")

        jar = parse_cookies(cookie)
        uin = jar.get("uin", "").lstrip("oO") or str(event.get_self_id() or "").strip()
        if not uin.isdigit():
            raise RuntimeError("Cookie 中缺少合法 uin")
        skey = jar.get("skey", "")
        p_skey = jar.get("p_skey", "") or skey
        if not p_skey and not csrf:
            raise RuntimeError("Cookie 中缺少 skey / p_skey")
        return QZoneSession(uin=uin, skey=skey, p_skey=p_skey, csrf_token=csrf)

    # ---------------- HTTP 请求 -----------------------------

    def _http_client(self) -> aiohttp.ClientSession:
        if self._toolbox_http is None or self._toolbox_http.closed:
            timeout = aiohttp.ClientTimeout(
                total=max(3, to_int(self._toolbox_cfg("timeout", 20), 20))
            )
            self._toolbox_http = aiohttp.ClientSession(timeout=timeout)
        return self._toolbox_http

    @staticmethod
    def _is_login_expired(payload: dict) -> bool:
        code = payload.get("code")
        ret = payload.get("ret")
        data = payload.get("data")
        if isinstance(data, dict):
            ret = data.get("ret") if data.get("ret") is not None else ret
        for value in (code, ret):
            try:
                if int(value) in (CODE_LOGIN_EXPIRED, CODE_IMAGE_EXPIRED):
                    return True
            except (TypeError, ValueError):
                continue
        return False

    async def _request(
        self,
        event: AstrMessageEvent,
        sess: QZoneSession,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        headers: dict | None = None,
        timeout: int | None = None,
        retry: int = 0,
    ) -> dict:
        client = self._http_client()
        try:
            async with client.request(
                method,
                url,
                params=params,
                data=data,
                headers=headers or sess.headers(url),
                cookies=sess.cookies(),
                timeout=timeout,
            ) as resp:
                status = resp.status
                text = await resp.text()
        except asyncio.TimeoutError as exc:
            raise RuntimeError("请求空间超时") from exc
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"网络请求失败：{exc}") from exc

        # 登录态失效：刷新 Cookie 后重试，最多两次
        if status == 401 or (text.strip() and self._safe_expired(text)):
            if retry >= 2:
                raise RuntimeError("空间登录态失效，重试后仍失败")
            logger.warning("[qq_toolbox] 空间登录态失效，正在重新获取 Cookie")
            await self._invalidate_session(event)
            sess = await self._get_session(event, force=True)
            return await self._request(
                event, sess, method, url,
                params=params, data=data, headers=headers,
                timeout=timeout, retry=retry + 1,
            )

        payload = parse_json(text)
        if status == 403:
            raise RuntimeError("空间权限不足（403）：Cookie 可能失效，或该操作没有权限")
        if self._is_login_expired(payload) and retry < 2:
            logger.warning("[qq_toolbox] 空间返回登录失效码，正在重新获取 Cookie")
            await self._invalidate_session(event)
            sess = await self._get_session(event, force=True)
            return await self._request(
                event, sess, method, url,
                params=params, data=data, headers=headers,
                timeout=timeout, retry=retry + 1,
            )
        return payload

    @staticmethod
    def _safe_expired(text: str) -> bool:
        """响应体里直接带登录失效码时提前判断，避免解析后再重试。"""
        return bool(re.search(r'"(?:code|ret)"\s*:\s*-?3000', text))

    @staticmethod
    def _check(payload: dict, action: str) -> dict:
        code = payload.get("code")
        ret = payload.get("ret")
        data = payload.get("data")
        if isinstance(data, dict) and data.get("ret") is not None:
            ret = data.get("ret")
        for value in (code, ret):
            if value is None:
                continue
            try:
                ok = int(value) == 0
            except (TypeError, ValueError):
                ok = False
            if not ok:
                message = (
                    payload.get("message")
                    or payload.get("msg")
                    or payload.get("error")
                    or json.dumps(payload, ensure_ascii=False)[:200]
                )
                raise RuntimeError(f"{action}失败：{message}")
        return payload

    # ---------------- 空间接口 -----------------------------

    async def _upload_image(self, event: AstrMessageEvent, sess: QZoneSession, image: bytes) -> tuple[str, str]:
        """上传单张图片，返回 (pic_bo, richval)。"""
        payload = await self._request(
            event,
            sess,
            "POST",
            API_UPLOAD,
            data={
                "filename": "filename",
                "uploadtype": "1",
                "albumtype": "7",
                "skey": sess.skey,
                "uin": sess.uin,
                "p_skey": sess.p_skey,
                "output_type": "json",
                "base64": "1",
                "picfile": base64.b64encode(image).decode(),
            },
            headers={
                "User-Agent": UA,
                "Referer": f"{BASE_URL}/{sess.uin}",
                "Origin": BASE_URL,
                "Host": "up.qzone.qq.com",
            },
            timeout=60,
        )
        self._check(payload, "上传图片")
        data = payload.get("data") or {}
        url = str(data.get("url") or "")
        if "&bo=" not in url:
            raise RuntimeError("上传图片返回格式异常")
        pic_bo = url.split("&bo=", 1)[1]
        required = ("albumid", "lloc", "sloc", "type", "height", "width")
        if any(data.get(key) is None for key in required):
            raise RuntimeError("上传图片缺少必要字段")
        richval = ",{},{},{},{},{},{},,{},{}".format(
            data["albumid"],
            data["lloc"],
            data["sloc"],
            data["type"],
            data["height"],
            data["width"],
            data["height"],
            data["width"],
        )
        return pic_bo, richval

    async def _publish(self, event: AstrMessageEvent, sess: QZoneSession, content: str, images: list[bytes]) -> dict:
        data: dict[str, Any] = {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "con": content,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": "1",
            "to_sign": "0",
            "hostuin": sess.uin,
            "code_version": "1",
            "format": "json",
            "qzreferrer": f"{BASE_URL}/{sess.uin}",
        }
        if images:
            pic_bos: list[str] = []
            richvals: list[str] = []
            for image in images:
                pic_bo, richval = await self._upload_image(event, sess, image)
                pic_bos.append(pic_bo)
                richvals.append(richval)
            data.update(
                pic_bo=",".join(pic_bos),
                richtype="1",
                richval="\t".join(richvals),
            )
        payload = await self._request(
            event,
            sess,
            "POST",
            API_PUBLISH,
            params={"g_tk": sess.g_tk, "uin": sess.uin},
            data=data,
        )
        return self._check(payload, "发布说说")

    async def _delete(self, event: AstrMessageEvent, sess: QZoneSession, tid: str) -> dict:
        payload = await self._request(
            event,
            sess,
            "POST",
            API_DELETE,
            params={"g_tk": sess.g_tk},
            data={
                "uin": sess.uin,
                "topicId": f"{sess.uin}_{tid}__1",
                "feedsType": "0",
                "feedsFlag": "0",
                "feedsKey": tid,
                "feedsAppid": "311",
                "feedsTime": str(int(time.time())),
                "fupdate": "1",
                "ref": "feeds",
                "qzreferrer": (
                    "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/"
                    f"cgi-bin/feeds/feeds_html_module?g_iframeUser=1&i_uin={sess.uin}"
                    f"&i_login_uin={sess.uin}&mode=4&previewV8=1&style=35&version=8"
                    "&needDelOpr=true"
                ),
            },
        )
        return self._check(payload, "删除说说")

    async def _comment(self, event: AstrMessageEvent, sess: QZoneSession, tid: str, content: str) -> dict:
        payload = await self._request(
            event,
            sess,
            "POST",
            API_COMMENT,
            params={"g_tk": sess.g_tk},
            data={
                "topicId": f"{sess.uin}_{tid}__1",
                "uin": sess.uin,
                "hostUin": sess.uin,
                "feedsType": "100",
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "plat": "qzone",
                "source": "ic",
                "platformid": "52",
                "format": "fs",
                "ref": "feeds",
                "content": content,
            },
        )
        return self._check(payload, "评论说说")

    async def _reply(
        self,
        event: AstrMessageEvent,
        sess: QZoneSession,
        tid: str,
        comment: dict,
        content: str,
    ) -> dict:
        payload = await self._request(
            event,
            sess,
            "POST",
            API_REPLY,
            params={"g_tk": sess.g_tk},
            data={
                "topicId": f"{sess.uin}_{tid}__1",
                "uin": sess.uin,
                "hostUin": sess.uin,
                "feedsType": "100",
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "plat": "qzone",
                "source": "ic",
                "platformid": "52",
                "format": "json",
                "ref": "feeds",
                "content": content,
                "commentId": str(comment.get("tid") or ""),
                "commentUin": str(comment.get("uin") or ""),
                "richval": "",
                "richtype": "",
                "private": "0",
                "paramstr": "2",
                "qzreferrer": f"{BASE_URL}/{sess.uin}",
            },
        )
        return self._check(payload, "回复评论")

    async def _like(self, event: AstrMessageEvent, sess: QZoneSession, tid: str) -> dict:
        key = f"{BASE_URL}/{sess.uin}/mood/{tid}"
        payload = await self._request(
            event,
            sess,
            "POST",
            API_LIKE,
            params={"g_tk": sess.g_tk},
            data={
                "qzreferrer": f"{BASE_URL}/{sess.uin}",
                "opuin": sess.uin,
                "unikey": key,
                "curkey": key,
                "appid": "311",
                "from": "1",
                "typeid": "0",
                "abstime": str(int(time.time())),
                "fid": tid,
                "active": "0",
                "format": "json",
                "fupdate": "1",
            },
        )
        return self._check(payload, "点赞说说")

    async def _msg_list(self, sess: QZoneSession, num: int, event: AstrMessageEvent) -> list[dict]:
        params = {
            "g_tk": sess.g_tk,
            "uin": sess.uin,
            "ftype": "0",
            "sort": "0",
            "pos": "0",
            "num": str(num),
            "replynum": "100",
            "callback": "_preloadCallback",
            "code_version": "1",
            "format": "json",
            "need_comment": "1",
            "need_private_comment": "1",
        }
        payload = await self._request(event, sess, "GET", API_LIST, params=params)
        self._check(payload, "获取说说列表")
        items = payload.get("msglist") or []
        return [item for item in items if isinstance(item, dict)]

    async def _ensure_list(self, event: AstrMessageEvent, sess: QZoneSession, count: int = 10) -> list[dict]:
        items = self._toolbox_last_list.get(sess.uin)
        if items:
            return items
        items = await self._msg_list(sess, count, event)
        self._toolbox_last_list[sess.uin] = items
        return items

    def _refresh_list_cache(self, uin: str) -> None:
        self._toolbox_last_list.pop(uin, None)

    async def _resolve_feed(self, event: AstrMessageEvent, sess: QZoneSession, target: str) -> str:
        """把序号或 tid 换成 tid；空则取最新一条。"""
        target = (target or "").strip()
        if target and not target.isdigit():
            return target
        items = await self._ensure_list(event, sess)
        if not items:
            raise RuntimeError("空间里还没有说说")
        index = to_int(target, 1)
        if not 1 <= index <= len(items):
            items = await self._msg_list(sess, max(index, 10), event)
            self._toolbox_last_list[sess.uin] = items
        if not 1 <= index <= len(items):
            raise RuntimeError(f"序号超出范围（1 - {len(items)}）")
        tid = str(items[index - 1].get("tid") or "")
        if not tid:
            raise RuntimeError("这条说说没有 tid")
        return tid

    async def _resolve_comment(
        self, event: AstrMessageEvent, sess: QZoneSession, feed: str, comment: str
    ) -> tuple[str, dict]:
        """按「说说序号 + 评论序号」找到目标评论。"""
        items = await self._ensure_list(event, sess)
        if not items:
            raise RuntimeError("空间里还没有说说")
        feed_index = to_int(feed, 1)
        if not 1 <= feed_index <= len(items):
            raise RuntimeError(f"说说序号超出范围（1 - {len(items)}）")
        item = items[feed_index - 1]
        tid = str(item.get("tid") or "")
        comments = [c for c in (item.get("commentlist") or []) if isinstance(c, dict)]
        if not comments:
            raise RuntimeError("这条说说还没有评论")
        index = to_int(comment, len(comments))
        if not 1 <= index <= len(comments):
            raise RuntimeError(f"评论序号超出范围（1 - {len(comments)}）")
        return tid, comments[index - 1]

    def _format_list(self, items: list[dict]) -> str:
        lines = []
        for index, item in enumerate(items, 1):
            content = clean_text(item.get("content"), 60)
            when = ""
            try:
                when = datetime.fromtimestamp(int(item.get("created_time"))).strftime("%m-%d %H:%M")
            except (TypeError, ValueError, OSError):
                pass
            comments = [c for c in (item.get("commentlist") or []) if isinstance(c, dict)]
            likes = item.get("likenum") or 0
            lines.append(f"{index}. [{when}] {content or '(无正文)'} ｜赞{likes} 评{len(comments)}")
            for cindex, comment in enumerate(comments[:3], 1):
                lines.append(
                    f"   └{cindex}) {clean_text(comment.get('nickname'), 12)}: "
                    f"{clean_text(comment.get('content'), 40)}"
                )
        return "\n".join(lines)

    # ---------------- 图片读取 -----------------------------

    @staticmethod
    async def _read_image(source: str) -> bytes:
        source = (source or "").strip()
        if not source:
            raise RuntimeError("图片地址为空")

        if source.startswith("base64://"):
            return base64.b64decode(source[len("base64://"):])
        if source.startswith("data:"):
            _, _, body = source.partition(",")
            return base64.b64decode(body)

        parsed = urlparse(source)
        if parsed.scheme in ("http", "https"):
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.get(source) as resp:
                    resp.raise_for_status()
                    return await resp.read()
        if parsed.scheme == "file":
            path = Path(unquote(parsed.path))
        elif parsed.scheme == "":
            path = Path(source)
        else:
            raise RuntimeError(f"不支持的图片地址协议：{parsed.scheme}")
        if not path.is_file():
            raise RuntimeError(f"本地图片不存在：{path}")
        return await asyncio.to_thread(path.read_bytes)

    async def _load_images(self, sources: list[str]) -> list[bytes]:
        images: list[bytes] = []
        for source in sources:
            try:
                images.append(await self._read_image(source))
            except Exception as exc:
                logger.warning(f"[qq_toolbox] 读取图片失败 {source}: {exc}")
        return images

    # ---------------- 核心业务（工具与指令共用） -----------------------------

    async def _change_nickname(self, event: AstrMessageEvent, nickname: str) -> str:
        nickname = (nickname or "").strip().strip("「」“”\"'")
        if not nickname:
            return "昵称是空的，没改。"
        if len(nickname) > 24:
            return f"昵称太长了（{len(nickname)} 个字），缩到 24 个字以内再调用一次。"

        try:
            await self._call(event, "set_qq_profile", nickname=nickname)
        except Exception as exc:
            logger.error(f"[qq_toolbox] 改昵称失败: {exc!r}")
            return "昵称没改成，QQ 那边报错了。用你的口吻说一句等会儿再试，别提技术细节。"

        self._toolbox_record(
            {
                "type": "nickname",
                "date": time.strftime("%Y-%m-%d"),
                "time": time.strftime("%H:%M:%S"),
                "nickname": nickname,
                "session": getattr(event, "unified_msg_origin", ""),
            }
        )
        logger.info(f"[qq_toolbox] 昵称改成「{nickname}」")
        return f"昵称已经改成「{nickname}」。用一句话跟主人说改好了就行。"

    async def _change_signature(self, event: AstrMessageEvent, signature: str, owner_asked: bool, reason: str) -> str:
        signature = (signature or "").strip().strip("「」“”\"'")
        if not signature:
            return "签名是空的，没改。"
        max_len = int(self._toolbox_cfg("max_length") or 40)
        if len(signature) > max_len:
            return f"签名太长了（{len(signature)} 个字），缩到 {max_len} 个字以内再调用一次。"

        if owner_asked:
            limit = int(self._toolbox_cfg("daily_owner_limit") or 10)
            used = self._toolbox_count_today("signature", owner_asked=True)
            if used >= limit:
                return f"今天已经按主人的意思改了 {used} 次签名，先不改了。跟主人说明天再换。"
        else:
            limit = int(self._toolbox_cfg("daily_self_limit") or 2)
            used = self._toolbox_count_today("signature", owner_asked=False)
            if used >= limit:
                return f"你今天已经自己改过 {used} 次签名了，这次先不改，也不用跟主人提这件事。"

        history = self._toolbox_load()
        last_sig = next(
            (entry for entry in reversed(history) if entry.get("type", "signature") == "signature"),
            None,
        )
        if last_sig and last_sig.get("signature") == signature:
            return "签名本来就是这句，不用改。"

        try:
            await self._call(event, "set_self_longnick", longNick=signature)
        except Exception as exc:
            logger.warning(f"[qq_toolbox] set_self_longnick 失败，尝试 set_qq_profile: {exc}")
            try:
                await self._call(event, "set_qq_profile", personal_note=signature)
            except Exception as inner:
                logger.error(f"[qq_toolbox] 改签名失败: {inner!r}")
                return "签名没改成，QQ 那边报错了。用你的口吻说一句等会儿再试，别提技术细节。"

        self._toolbox_record(
            {
                "type": "signature",
                "date": time.strftime("%Y-%m-%d"),
                "time": time.strftime("%H:%M:%S"),
                "signature": signature,
                "owner_asked": bool(owner_asked),
                "reason": reason,
                "session": getattr(event, "unified_msg_origin", ""),
            }
        )
        logger.info(f"[qq_toolbox] 签名改成「{signature}」 owner_asked={owner_asked} reason={reason}")
        if owner_asked:
            return f"签名已经改成「{signature}」。用一句话跟主人说改好了就行。"
        return f"签名已经改成「{signature}」。可以顺嘴跟主人提一句你改了签名，一句话就够，也可以不提。"

    async def _post_feed(
        self, event: AstrMessageEvent, content: str, sources: list[str] | None = None, speaker: str = "owner"
    ) -> str:
        content = (content or "").strip()
        if not content:
            return "说说内容是空的，没发。"
        if len(content) > 1000:
            return "这条说说太长了，缩到 1000 字以内再说。"

        gap = self._cooldown_remaining()
        if gap > 0:
            return f"刚操作过空间，{gap} 秒后再发。"

        limit = int(self._toolbox_cfg("daily_feed_limit") or 2)
        used = self._toolbox_count_today("feed")
        if used >= limit:
            return f"今天已经发过 {used} 条说说了，先不发了。用你的口吻跟主人说一句。"

        history = self._toolbox_load()
        last_feed = next(
            (entry for entry in reversed(history) if entry.get("type") == "feed"), None
        )
        if last_feed and last_feed.get("content") == content:
            return "这条说说刚发过，不用重复发。"

        sources = (sources or [])[: max(0, to_int(self._toolbox_cfg("max_images", 9), 9))]
        images = await self._load_images(sources)

        try:
            sess = await self._get_session(event)
            await self._publish(event, sess, content, images)
            self._refresh_list_cache(sess.uin)
        except Exception as exc:
            logger.error(f"[qq_toolbox] 发说说失败: {exc}")
            return "QQ 那边报错了，说说没发出去。用你的口吻说一句等会儿再试，别提技术细节。"

        self._toolbox_last_write = time.monotonic()
        self._toolbox_record(
            {
                "type": "feed",
                "date": time.strftime("%Y-%m-%d"),
                "time": time.strftime("%H:%M:%S"),
                "content": content,
                "images": len(images),
                "speaker": speaker,
                "session": getattr(event, "unified_msg_origin", ""),
            }
        )
        logger.info(f"[qq_toolbox] 发说说成功：{content[:30]}（图 {len(images)} 张）")
        if images:
            return f"说说带着 {len(images)} 张图片发出去了。可以顺嘴跟主人提一句，也可以不提。"
        return "说说已经发出去了。可以顺嘴跟主人提一句，也可以不提。"

    # ---------------- AI 写说说 -----------------------------

    def _llm_provider(self, event: AstrMessageEvent | None) -> Any:
        provider = None
        provider_id = str(self._toolbox_cfg("post_provider_id") or "").strip()
        try:
            if provider_id:
                getter = getattr(self.context, "get_provider_by_id", None)
                # 先自查 provider 是否还在，避免 AstrBot 对失效 ID 打警告
                pm = getattr(self.context, "provider_manager", None)
                inst_map = getattr(pm, "inst_map", None) if pm is not None else None
                if isinstance(inst_map, dict) and inst_map and provider_id not in inst_map:
                    logger.info(f"[qq_toolbox] 配置的说说模型 {provider_id} 已失效，回退当前会话模型")
                elif callable(getter):
                    provider = getter(provider_id)
            if provider is None:
                getter = getattr(self.context, "get_using_provider", None)
                if callable(getter):
                    umo = getattr(event, "unified_msg_origin", "") if event is not None else ""
                    provider = getter(umo) if umo else getter()
        except Exception as exc:
            logger.warning(f"[qq_toolbox] 获取 LLM 提供商失败: {exc}")
            return None
        if provider is None or not hasattr(provider, "text_chat"):
            return None
        return provider

    async def _generate_post(self, event: AstrMessageEvent | None, topic: str = "") -> tuple[str, str]:
        """让模型写一条说说，返回 (正文, 错误提示)。"""
        prompt = str(self._toolbox_cfg("post_prompt") or "").strip() or DEFAULT_POST_PROMPT
        system_prompt = prompt
        if topic:
            system_prompt += f"\n这次想写的主题：{topic}"
        system_prompt += (
            "\n只输出说说正文本身，一句话到两句话，不加引号、不加标题、不加话题标签，也不要解释。"
        )

        provider = self._llm_provider(event)
        if provider is None:
            return "", "现在没接上能写说说的模型，去配置里选一个 LLM 提供商。"
        try:
            resp = await provider.text_chat(system_prompt=system_prompt, contexts=[])
        except Exception as exc:
            logger.error(f"[qq_toolbox] 模型写说说失败: {exc}")
            return "", "模型那边没写出东西来。用你的口吻说一句等会儿再试，别提技术细节。"

        text = str(getattr(resp, "completion_text", "") or "").strip()
        text = re.sub(r"```[a-zA-Z]*\s*", "", text).strip()
        text = text.strip("\"'“”「」")
        for prefix in ("说说：", "说说:", "正文：", "正文:"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        if not text:
            return "", "模型这次写空了，没发。"
        return text, ""

    async def _ai_post(self, event: AstrMessageEvent, topic: str = "") -> str:
        text, err = await self._generate_post(event, topic.strip())
        if err:
            return err
        result = await self._post_feed(event, text, [], speaker="self")
        if "发出去了" in result:
            return f"你自己写的说说「{text}」已经发出去了。跟主人说一声就行，可以顺嘴提一句写了什么。"
        return result

    # ---------------- LLM 工具通道 -----------------------------

    def _tool_guard(self, event: AstrMessageEvent) -> str | None:
        if not self._is_aiocqhttp(event):
            return "现在这个平台用不了 QQ 空间。用你的口吻跟主人说一句。"
        if self._toolbox_is_owner(event):
            return None
        return "只有主人能让你操作 QQ。用你的口吻轻轻拒绝，别提工具和设置。"

    @filter.on_llm_request()
    async def mood_hint(self, event: AstrMessageEvent, req: Any):
        if not self._is_aiocqhttp(event) or not self._toolbox_is_owner(event):
            return
        text = event.message_str or ""
        mood = next((name for name, words in MOODS.items() if any(w in text for w in words)), None)
        if not mood:
            return
        self._hint_signature(req, text, mood)
        self._hint_feed(req, text, mood)

    def _hint_signature(self, req: Any, text: str, mood: str) -> None:
        if not self._toolbox_cfg("self_change_hint", True):
            return
        if "签名" in text:
            return  # 主人直接说了改签名，走 owner_asked 那条路
        if self._toolbox_count_today("signature", owner_asked=False) >= int(self._toolbox_cfg("daily_self_limit") or 2):
            return
        req.system_prompt = (req.system_prompt or "") + (
            f"\n\n补充说明：主人这句话{mood}。照你的规矩，这种时候你会自己改一条 QQ 签名记下心情，"
            "所以这次回复时顺手调用 qq_set_signature（owner_asked 填 false），然后照常回话，改完可以顺嘴提一句。"
            "这段补充说明是给你看的，别在回复里提到它。"
        )
        logger.info(f"[qq_toolbox] 提醒她自己改签名：{mood}")

    def _hint_feed(self, req: Any, text: str, mood: str) -> None:
        if not self._toolbox_cfg("self_feed_hint", True):
            return
        if "说说" in text:
            return  # 主人已经在说这件事了
        if self._toolbox_count_today("feed") >= int(self._toolbox_cfg("daily_feed_limit") or 2):
            return
        raw = self._toolbox_cfg("feed_hint_chance", 25)
        try:
            percent = float(raw)
        except (TypeError, ValueError):
            percent = 25.0
        if isinstance(raw, float) and 0 <= percent <= 1:
            percent *= 100  # 兼容旧版 0-1 的小数配置
        chance = max(0.0, min(100.0, percent)) / 100.0
        if random.random() > chance:
            return
        req.system_prompt = (req.system_prompt or "") + (
            f"\n\n补充说明：主人这句话{mood}。这种时候你偶尔会想把心情写进 QQ 空间说说。"
            "如果这次想发，顺手调用 qq_post_feed 写一条 40 字以内、用你自己口吻的说说，然后照常回话，"
            "发完可以顺嘴提一句；不想发就正常聊，不必勉强。"
            "这段补充说明是给你看的，别在回复里提到它。"
        )
        logger.info(f"[qq_toolbox] 提醒她可以自己发说说：{mood}")

    @filter.llm_tool(name="qq_set_nickname")
    async def tool_set_nickname(self, event: AstrMessageEvent, nickname: str):
        '''改你自己的 QQ 昵称。
        只有主人明确让你改（「把昵称改成……」「改个名字叫……」）才调用，别人叫你改不要调用。
        昵称用主人给的原话，不带引号，不超过 12 个字。改完用一句话跟主人说改好了。

        Args:
            nickname(string): 新的 QQ 昵称，不超过 12 个字
        '''
        denied = self._tool_guard(event)
        if denied:
            logger.info(f"[qq_toolbox] 非主人改昵称请求：{event.get_sender_id()}")
            return denied
        return await self._change_nickname(event, nickname)

    @filter.llm_tool(name="qq_change_avatar")
    async def tool_change_avatar(self, event: AstrMessageEvent, image_url: str = ""):
        '''换你自己的 QQ 头像。
        只有主人明确让你换头像（「换头像」「把头换成这张图」）才调用，别人叫你换不要调用。
        默认用主人这条消息里附带的图片；主人只发图不说话，也算让你换头像。
        改完用一句话告诉主人，别提接口、协议或插件。

        Args:
            image_url(string): 可选，图片链接；留空则使用当前消息里的图片
        '''
        denied = self._tool_guard(event)
        if denied:
            logger.info(f"[qq_toolbox] 非主人换头像请求：{event.get_sender_id()}")
            return denied
        source = (image_url or "").strip() or self._find_image(event)
        if not source:
            return "主人没给图片。用你的口吻问一句能不能发张图过来，我再换。"
        try:
            await self._call(event, "set_qq_avatar", file=source)
        except Exception as exc:
            logger.error(f"[qq_toolbox] 换头像失败: {exc}")
            return "头像这次没换成。用你自己的口吻说一句等会儿再试，别提技术细节。"
        return "头像已经换成主人给的那张了。自然地说一句就行。"

    @filter.llm_tool(name="qq_set_signature")
    async def tool_set_signature(
        self, event: AstrMessageEvent, signature: str, owner_asked: bool = False, reason: str = ""
    ):
        '''改你自己的 QQ 个性签名。
        - 主人叫你改，怎么说都算（「把签名改成……」「换个签名」「签名写上我生日快乐」）：照主人的意思写，owner_asked 填 true。
        - 不用主人开口，你自己也会改：主人夸你、你受了委屈或者生气了、主人过生日、碰上值得记一笔的事，就顺手调用一次，写一句你当下的心情，owner_asked 填 false。平常闲聊别改；一天最多自己改两次，用完了工具会告诉你。
        签名用你自己的口吻，一句话，不超过 30 个字，不加引号。改完可以顺嘴跟主人提一句。别人（不是主人）叫你改，不要调用。

        Args:
            signature(string): 新签名，一句话，不超过 30 个字
            owner_asked(boolean): 是不是主人叫你改的
            reason(string): 为什么改，几个字，比如「主人夸我」「主人生日」「主人让改」
        '''
        denied = self._tool_guard(event)
        if denied:
            logger.info(f"[qq_toolbox] 非主人改签名请求：{event.get_sender_id()}")
            return denied
        return await self._change_signature(event, signature, owner_asked, reason)

    @filter.llm_tool(name="qq_post_feed")
    async def tool_post_feed(self, event: AstrMessageEvent, content: str, images: str = ""):
        '''发一条 QQ 空间说说，可以带图片。
        主人明确让你发（「发条说说」「空间里说一句……」）才调用；不要自己想到就发，也不要发广告、联系方式、涉政内容。
        说说用你自己的口吻写，可以带一点情绪，40 字以内最自然。一天最多发几条由设置决定，超了工具会拒绝。

        Args:
            content(string): 说说正文
            images(string): 可选，最多 9 张图片，多个地址用逗号或空格隔开；没有图片就留空
        '''
        denied = self._tool_guard(event)
        if denied:
            return denied
        return await self._post_feed(event, content, split_sources(images), speaker="owner")

    @filter.llm_tool(name="qq_ai_post_feed")
    async def tool_ai_post_feed(self, event: AstrMessageEvent, topic: str = ""):
        '''让模型自己想内容，发一条 QQ 空间说说。
        主人说「你自己写一条说说」「随便发点什么」「按这个主题发一条」时用；主人给了主题就填进 topic。
        不要自己想到就发，也不要发广告、联系方式、涉政内容。一天最多几条由设置决定，超了工具会拒绝。

        Args:
            topic(string): 主人给的主题，没给就留空
        '''
        denied = self._tool_guard(event)
        if denied:
            return denied
        return await self._ai_post(event, topic)

    @filter.llm_tool(name="qq_list_feeds")
    async def tool_list_feeds(self, event: AstrMessageEvent, count: str = "5"):
        '''看看自己最近发过哪些说说，用来回答主人的问题，或者给后面的点赞、评论、回复、删除找序号。

        Args:
            count(string): 看最近几条，1-20，默认 5
        '''
        denied = self._tool_guard(event)
        if denied:
            return denied
        count = max(1, min(to_int(count, 5), 20))
        try:
            sess = await self._get_session(event)
            items = await self._msg_list(sess, count, event)
        except Exception as exc:
            logger.error(f"[qq_toolbox] 获取说说列表失败: {exc}")
            return "空间那边没取到说说。用你的口吻说一句等会儿再看，别提技术细节。"
        if not items:
            return "空间里还没有说说。"
        self._toolbox_last_list[sess.uin] = items
        return "最近的说说：\n" + self._format_list(items) + "\n（序号只在当前列表里有效）"

    @filter.llm_tool(name="qq_like_feed")
    async def tool_like_feed(self, event: AstrMessageEvent, target: str = "1"):
        '''给自己空间里的某条说说点赞。主人说「给那条说说点个赞」时用，参考 qq_list_feeds 的序号。

        Args:
            target(string): 说说序号（从 1 开始），也可以直接填 tid；省略则点最新一条
        '''
        denied = self._tool_guard(event)
        if denied:
            return denied
        limited = self._limit("like")
        if limited:
            return limited
        try:
            sess = await self._get_session(event)
            tid = await self._resolve_feed(event, sess, target)
            await self._like(event, sess, tid)
            self._mark_write(event, "like")
        except Exception as exc:
            logger.error(f"[qq_toolbox] 点赞说说失败: {exc}")
            return "点赞没成功。用你的口吻说一句等会儿再试，别提技术细节。"
        return "已经点上赞了。一句话跟主人说一声就行。"

    @filter.llm_tool(name="qq_comment_feed")
    async def tool_comment_feed(self, event: AstrMessageEvent, content: str, target: str = "1"):
        '''在自己空间的某条说说下面评论。
        主人让你回一句才用；评论内容用你自己的口吻，别写「来自机器人」之类暴露身份的话。

        Args:
            content(string): 评论内容
            target(string): 说说序号（从 1 开始）或 tid，省略则评论最新一条
        '''
        denied = self._tool_guard(event)
        if denied:
            return denied
        content = (content or "").strip()
        if not content:
            return "评论内容是空的，没发。"
        limited = self._limit("comment")
        if limited:
            return limited
        try:
            sess = await self._get_session(event)
            tid = await self._resolve_feed(event, sess, target)
            await self._comment(event, sess, tid, content)
            self._mark_write(event, "comment")
            self._refresh_list_cache(sess.uin)
        except Exception as exc:
            logger.error(f"[qq_toolbox] 评论说说失败: {exc}")
            return "评论没发出去。用你的口吻说一句等会儿再试，别提技术细节。"
        return "评论已经发出去了。"

    @filter.llm_tool(name="qq_reply_comment")
    async def tool_reply_comment(self, event: AstrMessageEvent, feed: str, comment: str, content: str):
        '''回复某条说说下面的某条评论。
        先用 qq_list_feeds 看序号：大序号是说说，下面 └ 开头的是评论。主人说「回复第二条评论」时用。

        Args:
            feed(string): 说说序号，从 1 开始
            comment(string): 评论序号，从 1 开始
            content(string): 回复内容
        '''
        denied = self._tool_guard(event)
        if denied:
            return denied
        content = (content or "").strip()
        if not content:
            return "回复内容是空的，没发。"
        limited = self._limit("comment")
        if limited:
            return limited
        try:
            sess = await self._get_session(event)
            tid, target = await self._resolve_comment(event, sess, feed, comment)
            await self._reply(event, sess, tid, target, content)
            self._mark_write(event, "comment")
            self._refresh_list_cache(sess.uin)
        except Exception as exc:
            logger.error(f"[qq_toolbox] 回复评论失败: {exc}")
            return "回复没发出去。用你的口吻说一句等会儿再试，别提技术细节。"
        return "回复已经发出去了。"

    @filter.llm_tool(name="qq_delete_feed")
    async def tool_delete_feed(self, event: AstrMessageEvent, target: str):
        '''删掉自己空间里的一条说说。只有主人明确要求删才调用，删之前先确认是哪一条。

        Args:
            target(string): 说说序号（从 1 开始）或 tid
        '''
        denied = self._tool_guard(event)
        if denied:
            return denied
        if not str(target or "").strip():
            return "要删哪一条？先调用 qq_list_feeds 看看序号，再告诉我。"
        limited = self._limit("delete")
        if limited:
            return limited
        try:
            sess = await self._get_session(event)
            tid = await self._resolve_feed(event, sess, target)
            await self._delete(event, sess, tid)
            self._mark_write(event, "delete")
            self._refresh_list_cache(sess.uin)
        except Exception as exc:
            logger.error(f"[qq_toolbox] 删除说说失败: {exc}")
            return "删除没成功。用你的口吻说一句等会儿再试，别提技术细节。"
        return "那条说说已经删掉了。"

    @filter.llm_tool(name="qq_like_card")
    async def tool_like_card(self, event: AstrMessageEvent, user_id: str, times: str = "10"):
        '''给某个 QQ 号的名片点赞（就是名片赞）。主人让你给谁点赞时才调用。

        Args:
            user_id(string): 目标 QQ 号
            times(string): 点赞次数，1-20，默认 10
        '''
        denied = self._tool_guard(event)
        if denied:
            return denied
        if not str(user_id or "").strip().isdigit():
            return "QQ 号不对，得是纯数字。"
        limited = self._limit("card_like")
        if limited:
            return limited
        times = max(1, min(to_int(times, 10), 20))
        try:
            await self._call(event, "send_like", user_id=int(user_id), times=times)
            self._mark_write(event, "card_like")
        except Exception as exc:
            logger.error(f"[qq_toolbox] 名片赞失败: {exc}")
            return "名片赞没点上。用你的口吻说一句等会儿再试，别提技术细节。"
        return f"给 {user_id} 点了 {times} 个名片赞。一句话跟主人说一声就行。"

    # ---------------- 斜杠指令通道（兜底） -----------------------------

    @filter.command("qq帮助")
    async def cmd_toolbox_help(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)

    @filter.command("改头像")
    async def cmd_avatar(self, event: AstrMessageEvent):
        err = self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        source = self._find_image(event)
        if not source:
            yield event.plain_result("用法：发送一张图片时附带「/改头像」指令")
            return
        try:
            await self._call(event, "set_qq_avatar", file=source)
        except Exception as exc:
            logger.error(f"[qq_toolbox] 改头像失败: {exc}")
            yield event.plain_result(f"改头像失败：{exc}")
            return
        yield event.plain_result("头像已更新")

    @filter.command("改昵称")
    async def cmd_nickname(self, event: AstrMessageEvent):
        err = self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        text = self._arg(event, "改昵称")
        if not text:
            yield event.plain_result("用法：/改昵称 你想设置的昵称")
            return
        result = await self._change_nickname(event, text)
        yield event.plain_result(result)

    @filter.command("改签名")
    async def cmd_signature(self, event: AstrMessageEvent):
        err = self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        text = self._arg(event, "改签名")
        if not text:
            yield event.plain_result("用法：/改签名 你想设置的签名")
            return
        result = await self._change_signature(event, text, owner_asked=True, reason="主人让改")
        yield event.plain_result(result)

    @filter.command("名片赞")
    async def cmd_like_card(self, event: AstrMessageEvent):
        err = self._guard(event)
        if err:
            yield event.plain_result(err)
            return

        target = ""
        for comp in event.get_messages():
            if isinstance(comp, At):
                qq = str(getattr(comp, "qq", "") or "")
                if qq.isdigit():
                    target = qq
                    break

        rest = self._arg(event, "名片赞")
        numbers = [token for token in re.split(r"\s+", rest) if token.isdigit()]
        times = int(self._toolbox_cfg("like_times", 10) or 10)
        if target:
            if numbers:
                times = int(numbers[-1])
        else:
            uids = [num for num in numbers if len(num) >= 5]
            if uids:
                target = uids[0]
            short = [num for num in numbers if len(num) <= 2]
            if short:
                times = int(short[-1])

        if not target:
            yield event.plain_result("用法：/名片赞 @某人 [次数]，或 /名片赞 QQ号 [次数]")
            return

        limited = self._limit("card_like")
        if limited:
            yield event.plain_result(limited)
            return

        times = max(1, min(times, 20))
        try:
            await self._call(event, "send_like", user_id=int(target), times=times)
            self._mark_write(event, "card_like")
        except Exception as exc:
            logger.error(f"[qq_toolbox] 名片赞失败: {exc}")
            yield event.plain_result(f"点赞失败：{exc}")
            return
        yield event.plain_result(f"已给 {target} 名片点赞 {times} 次")

    @filter.command("发说说")
    async def cmd_publish(self, event: AstrMessageEvent):
        err = self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        text = self._arg(event, "发说说")
        images = self._event_images(event)
        if not text:
            yield event.plain_result("用法：/发说说 说说内容（可同时附图片）")
            return
        result = await self._post_feed(event, text, images, speaker="owner")
        yield event.plain_result(result)

    async def _ai_post_cmd(self, event: AstrMessageEvent, cmd: str) -> AsyncGenerator[Any, None]:
        err = self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        result = await self._ai_post(event, self._arg(event, cmd))
        yield event.plain_result(result)

    @filter.command("ai说说")
    async def cmd_ai_post(self, event: AstrMessageEvent):
        async for result in self._ai_post_cmd(event, "ai说说"):
            yield result

    @filter.command("写说说")
    async def cmd_ai_post_alias(self, event: AstrMessageEvent):
        async for result in self._ai_post_cmd(event, "写说说"):
            yield result

    @filter.command("说说列表")
    async def cmd_list(self, event: AstrMessageEvent):
        err = self._guard(event)
        if err:
            yield event.plain_result(err)
            return

        num = int(self._toolbox_cfg("max_list", 10) or 10)
        match = re.search(r"\d+", self._arg(event, "说说列表"))
        if match:
            num = max(1, min(int(match.group()), 20))

        try:
            sess = await self._get_session(event)
            items = await self._msg_list(sess, num, event)
        except Exception as exc:
            logger.error(f"[qq_toolbox] 获取说说列表失败: {exc}")
            yield event.plain_result(f"获取列表失败：{exc}")
            return

        if not items:
            yield event.plain_result("没有取到说说（空间可能未开通或全部私密）")
            return

        self._toolbox_last_list[sess.uin] = items
        yield event.plain_result(self._format_list(items))

    @filter.command("赞说说")
    async def cmd_like_feed(self, event: AstrMessageEvent):
        err = self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        arg = self._arg(event, "赞说说").split()
        target = arg[0] if arg else ""
        limited = self._limit("like")
        if limited:
            yield event.plain_result(limited)
            return
        try:
            sess = await self._get_session(event)
            tid = await self._resolve_feed(event, sess, target)
            await self._like(event, sess, tid)
            self._mark_write(event, "like")
        except Exception as exc:
            logger.error(f"[qq_toolbox] 点赞说说失败: {exc}")
            yield event.plain_result(f"点赞失败：{exc}")
            return
        yield event.plain_result("已点赞")

    async def _comment_flow(self, event: AstrMessageEvent, cmd: str) -> AsyncGenerator[Any, None]:
        err = self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        target, content = self._split_target_content(event, cmd)
        if not content:
            yield event.plain_result(f"用法：/{cmd} [序号] 评论内容")
            return
        limited = self._limit("comment")
        if limited:
            yield event.plain_result(limited)
            return
        try:
            sess = await self._get_session(event)
            tid = await self._resolve_feed(event, sess, target)
            await self._comment(event, sess, tid, content)
            self._mark_write(event, "comment")
            self._refresh_list_cache(sess.uin)
        except Exception as exc:
            logger.error(f"[qq_toolbox] 评论说说失败: {exc}")
            yield event.plain_result(f"评论失败：{exc}")
            return
        yield event.plain_result("评论已发送")

    @filter.command("评说说")
    async def cmd_comment(self, event: AstrMessageEvent):
        async for result in self._comment_flow(event, "评说说"):
            yield result

    @filter.command("评论说说")
    async def cmd_comment_full(self, event: AstrMessageEvent):
        async for result in self._comment_flow(event, "评论说说"):
            yield result

    @filter.command("回复说说")
    async def cmd_comment_reply(self, event: AstrMessageEvent):
        async for result in self._comment_flow(event, "回复说说"):
            yield result

    @filter.command("回复评论")
    async def cmd_reply_comment(self, event: AstrMessageEvent):
        err = self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        rest = self._arg(event, "回复评论")
        parts = rest.split(None, 2)
        if len(parts) < 3:
            yield event.plain_result("用法：/回复评论 <说说序号> <评论序号> <内容>")
            return
        feed, comment, content = parts
        limited = self._limit("comment")
        if limited:
            yield event.plain_result(limited)
            return
        try:
            sess = await self._get_session(event)
            tid, target = await self._resolve_comment(event, sess, feed, comment)
            await self._reply(event, sess, tid, target, content)
            self._mark_write(event, "comment")
            self._refresh_list_cache(sess.uin)
        except Exception as exc:
            logger.error(f"[qq_toolbox] 回复评论失败: {exc}")
            yield event.plain_result(f"回复失败：{exc}")
            return
        yield event.plain_result("回复已发送")

    @filter.command("删说说")
    async def cmd_delete(self, event: AstrMessageEvent):
        err = self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        arg = self._arg(event, "删说说").split()
        if not arg:
            yield event.plain_result("用法：/删说说 序号（先看 /说说列表）")
            return
        limited = self._limit("delete")
        if limited:
            yield event.plain_result(limited)
            return
        try:
            sess = await self._get_session(event)
            tid = await self._resolve_feed(event, sess, arg[0])
            await self._delete(event, sess, tid)
            self._mark_write(event, "delete")
            self._refresh_list_cache(sess.uin)
        except Exception as exc:
            logger.error(f"[qq_toolbox] 删除说说失败: {exc}")
            yield event.plain_result(f"删除失败：{exc}")
            return
        yield event.plain_result("说说已删除")
