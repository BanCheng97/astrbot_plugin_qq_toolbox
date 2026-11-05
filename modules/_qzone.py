"""QQ 空间接口的纯函数、常量与会话上下文。

从 toolbox.py 拆出的无状态部分：不依赖 AstrBot 运行时，便于单测与复用。
所有符号保持原名，toolbox.py 直接 import 回来，行为完全不变。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

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
    "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6"
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

HELP_TEXT = """QQ 助手指令（也可以直接用大白话跟她说，不必用指令）：
/改头像（消息附图片）  修改机器人头像
/改昵称 <昵称>  修改机器人 QQ 昵称
/改签名 <内容>  修改机器人个性签名
/删签名  清空（删除）机器人个性签名
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


def parse_cookies(raw) -> dict[str, str]:
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


def extract_cookies(resp) -> str:
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


def clean_text(value, limit: int = 80) -> str:
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
