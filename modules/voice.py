"""AstrBot 插件：大肥鱼声音

让她能听、能说、还能唱：
- 听懂：收到语音消息自动转文字，她就能理解你在说什么
- 说话：她回复时可以用语音说出来，不是只有文字；也可以手动让她说
- 唱歌：给一句歌词，她用带情绪的调调"唱"给你

TTS 后端：BreezeBlue 或小米 MiMo 原生 TTS（配置页 tts_backend 切换）
- 支持 BreezeBlue voice_id（预设 / 设计 / 克隆音色都能用）与 MiMo 内置音色
STT 后端：只走 MiMo ASR（mimo-v2.5-asr，需 wav / mp3）
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import tempfile
from typing import Any

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain, Record

from .base import SuiteBase, to_int, to_float

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

NAME = "astrbot_plugin_dafeiyu_voice"


class VoiceMixin(SuiteBase):
    def setup_voice(self) -> None:
        self._voice_lock = asyncio.Lock()
        self._voice_data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(self._voice_data_dir, exist_ok=True)
        self._voice_state_path = os.path.join(self._voice_data_dir, "voice_state.json")
        self._voice_state = self._voice_load_state()

    # ---------------- 配置读取 -----------------------------

    def _voice_load_state(self) -> dict:
        try:
            if os.path.isfile(self._voice_state_path):
                with open(self._voice_state_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _voice_save_state(self) -> None:
        try:
            with open(self._voice_state_path, "w", encoding="utf-8") as f:
                json.dump(self._voice_state, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning(f"[voice] 保存状态失败: {exc}")

    def _voice_cfg(self, key: str, default: Any = None) -> Any:
        # 优先读 state 文件（指令设置的），再查 WebUI 配置
        if key in ("breeze_voice_id",):
            state_val = self._voice_state.get(key)
            if state_val:
                return state_val
        return self.cfg("voice", key, default)

    # ---------------- 情绪判定 -----------------------------

    def _detect_emotion(self, text: str) -> str:
        t = text or ""
        # 唱歌优先级最高
        if any(w in t for w in ["唱", "歌", "哼", "旋律", "调调", "歌词"]):
            return "sing"
        happy = any(w in t for w in ["哈哈", "嘻嘻", "开心", "太好了", "哇", "耶", "嘿嘿", "~", "！"])
        sad = any(w in t for w in ["呜", "呜呜", "难过", "伤心", "哭", "委屈", "想你", "……", "。。。"])
        angry = any(w in t for w in ["哼", "讨厌", "烦", "生气", "滚"])
        if sad:
            return "sad"
        if angry:
            return "angry"
        if happy:
            return "happy"
        if any(w in t for w in ["好不好", "好不好嘛", "求你", "好不好嘛", "撒娇"]):
            return "soft"
        return "normal"

    def _user_says_sing(self, event: AstrMessageEvent) -> bool:
        """检测用户原文有没有唱歌请求意图。"""
        try:
            chain = event.message_obj.message
            if hasattr(chain, "chain"):
                chain = chain.chain
            user_text = ""
            for seg in (chain or []):
                if isinstance(seg, Plain):
                    user_text += seg.text or ""
                elif isinstance(seg, str):
                    user_text += seg
            user_text = user_text.strip()
            return any(w in user_text for w in ["唱个歌", "唱歌给我", "唱一首", "会唱", "来首歌", "哼一段", "唱一下", "你唱", "唱两句", "唱个", "唱吧"])
        except Exception:
            return False

    # ---------------- BreezeBlue TTS -----------------------------

    # emotion → 语气指令映射（BreezeBlue / MiMo 通用）
    _EMOTION_INSTRUCTIONS = {
        "normal": "",
        "happy": "像真人聊天一样开心活泼，语速轻快，句尾带笑意和一点上扬",
        "sad": "声音放轻放慢，带一点鼻音和哽咽的气音，不要念课文",
        "angry": "带情绪的抱怨语气，语速偏快，尾音短促，但别像朗读",
        "soft": "温柔撒娇、甜甜糯糯，语速慢一点，句尾轻轻放缓，带一点气声",
        "sing": "用唱歌的调调唱出来，每个字都有旋律感，像歌曲一样有高低起伏",
    }
    _NATURAL_INSTRUCTION = "像真人语音聊天一样口语自然，语气亲切、有起伏和呼吸感，句尾自然收住，不要播音腔和念稿感"

    _EMOJI_RE = re.compile(
        "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\uFE0F\u200D\u2190-\u21FF\u2B00-\u2BFF]+"
    )

    def _clean_tts_text(self, text: str) -> str:
        """清洗 LLM 回复里的 emoji / markdown / 括号动作，避免被念出来显得机械。"""
        t = text or ""
        t = re.sub(r"https?://\S+", "", t)                        # 链接
        t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)            # markdown 链接
        t = re.sub(r"\|", "，", t)                                # 表格分隔
        t = re.sub(r"[*_`#>•·●○■□]+", "", t)                      # markdown 标记
        t = re.sub(r"[（(【\[][^）)】\]]{0,40}[）)】\]]", "", t)    # 括号里的动作描写
        t = self._EMOJI_RE.sub("", t)                             # emoji
        t = re.sub(r"[~～]+", "", t)                               # 波浪号
        t = re.sub(r"\s*\n+\s*", "。", t)                          # 换行 → 停顿
        t = re.sub(r"[ \t]+", "", t)
        t = re.sub(r"[，,]{2,}", "，", t)
        t = re.sub(r"[。！？]{2,}", "。", t)
        return t.strip("，。；;、 ")

    async def _breeze_tts(self, text: str, out_path: str, emotion: str = "normal") -> str:
        """直连 BreezeBlue TTS API。支持预设 / 设计 / 克隆音色的 voice_id。"""
        key = str(self._voice_cfg("breeze_api_key", "") or "").strip()
        voice_id = str(self._voice_cfg("breeze_voice_id", "") or "").strip()
        if not key:
            raise RuntimeError("未配置 BreezeBlue API Key")
        if not voice_id:
            raise RuntimeError("未配置 BreezeBlue Voice ID，请在 https://breezeblue.ai/zh/app/voice-library 选一个音色")

        model = str(self._voice_cfg("breeze_model", "breeze-tts-2") or "breeze-tts-2").strip()
        user_instructions = str(self._voice_cfg("tts_instructions", "") or "").strip()
        # emotion 映射成 BreezeBlue instructions，用户配置页的优先
        emo_instr = self._EMOTION_INSTRUCTIONS.get(emotion, "")
        if emo_instr and not user_instructions:
            instructions = emo_instr
        elif emo_instr and user_instructions:
            instructions = f"{user_instructions}，{emo_instr}"
        else:
            instructions = user_instructions or self._NATURAL_INSTRUCTION
        timeout = to_float(self._voice_cfg("timeout_seconds", 30), 30.0)

        url = f"https://api.breeze.blue/v1/text-to-speech/{voice_id}?output_format=wav&delivery=sync"
        body: dict[str, Any] = {"text": text}
        if model:
            body["model_id"] = model
        if instructions:
            body["instructions"] = instructions
            logger.info(f"[voice] BreezeBlue instructions: {instructions[:80]}")

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                headers={"xi-api-key": key, "Content-Type": "application/json"},
                json=body,
            )
            if resp.status_code >= 400:
                # 尝试解析错误 JSON
                try:
                    err = resp.json()
                    raise RuntimeError(f"BreezeBlue API {resp.status_code}: {err}")
                except Exception:
                    raise RuntimeError(f"BreezeBlue API {resp.status_code}: {resp.text[:200]}")
            # 成功：直接返回二进制 mp3
            with open(out_path, "wb") as f:
                f.write(resp.content)
        return out_path

    async def _mimo_tts(self, text: str, out_path: str, emotion: str = "normal") -> str:
        """直连小米 MiMo 原生 TTS（chat/completions + audio 参数，模型 mimo-v2.5-tts）。"""
        import base64 as _b64

        key = (
            str(self._voice_cfg("mimo_tts_api_key", "") or "").strip()
            or str(self._voice_cfg("mimo_stt_api_key", "") or "").strip()
            or str(self._voice_cfg("breeze_api_key", "") or "").strip()
        )
        if not key:
            raise RuntimeError("未配置 MiMo API Key")
        model = str(self._voice_cfg("mimo_tts_model", "mimo-v2.5-tts") or "mimo-v2.5-tts").strip()
        voice = str(self._voice_cfg("mimo_tts_voice", "mimo_default") or "mimo_default").strip()

        user_instructions = str(self._voice_cfg("tts_instructions", "") or "").strip()
        emo_instr = self._EMOTION_INSTRUCTIONS.get(emotion, "")
        if emo_instr and user_instructions:
            instructions = f"{user_instructions}，{emo_instr}"
        else:
            instructions = user_instructions or emo_instr or self._NATURAL_INSTRUCTION

        target = text
        if emotion == "sing":
            # MiMo 用开头标签触发唱歌模式
            target = "(唱歌)" + text

        timeout = to_float(self._voice_cfg("timeout_seconds", 30), 30.0)
        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": instructions},
                {"role": "assistant", "content": target},
            ],
            "audio": {"format": "wav", "voice": voice},
        }
        logger.info(f"[voice] MiMo TTS 请求: model={model}, voice={voice}, emotion={emotion}")

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                "https://api.xiaomimimo.com/v1/chat/completions",
                headers={"api-key": key, "Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code >= 400:
                try:
                    err = resp.json()
                except Exception:
                    err = resp.text[:200]
                raise RuntimeError(f"MiMo TTS API {resp.status_code}: {err}")
            data = resp.json()

        message = (data.get("choices") or [{}])[0].get("message") or {}
        audio_b64 = (message.get("audio") or {}).get("data") or ""
        if not audio_b64:
            raise RuntimeError("MiMo TTS 没有返回音频数据")
        with open(out_path, "wb") as f:
            f.write(_b64.b64decode(audio_b64))
        return out_path

    async def _synthesize(self, text: str, emotion: str = "normal") -> str:
        """合成到临时 wav 路径，返回路径。tts_backend=mimo 走 MiMo，否则内置 provider → BreezeBlue。"""
        if not text or not text.strip():
            raise ValueError("要合成的文本是空的")
        text = self._clean_tts_text(text)
        if not text:
            raise ValueError("清洗后没有可朗读的文本")
        tmp = tempfile.NamedTemporaryFile(prefix="dafeiyu_voice_", suffix=".wav", delete=False)
        tmp.close()
        path = tmp.name

        backend = str(self._voice_cfg("tts_backend", "breeze") or "breeze").strip().lower()
        if backend == "mimo":
            await self._mimo_tts(text, path, emotion)
            return path

        # 1. 优先 AstrBot 内置 TTS provider
        try:
            providers = getattr(self.context, "get_all_tts_providers", lambda: [])()
            provider = providers[0] if providers else None
            if provider is not None:
                audio_path = await provider.get_audio(text)
                if isinstance(audio_path, str) and os.path.isfile(audio_path):
                    return audio_path
                if isinstance(audio_path, (bytes, bytearray)):
                    with open(path, "wb") as f:
                        f.write(audio_path)
                    return path
        except Exception as exc:
            logger.warning(f"[voice] 内置 TTS provider 失败，回退 BreezeBlue: {exc}")

        # 2. 走 BreezeBlue 直连
        await self._breeze_tts(text, path, emotion)
        return path

    # ---------------- STT 后端 -----------------------------

    async def _mimo_stt(self, file_path: str) -> str:
        """直连 MiMo ASR（api.xiaomimimo.com），只认 wav / mp3。"""
        import base64 as _b64

        key = str(self._voice_cfg("mimo_stt_api_key", "") or "").strip() or str(self._voice_cfg("breeze_api_key", "") or "").strip()
        if not key:
            raise RuntimeError("未配置 MiMo API Key")
        model = str(self._voice_cfg("mimo_stt_model", "mimo-v2.5-asr") or "mimo-v2.5-asr").strip()
        language = str(self._voice_cfg("mimo_stt_language", "zh") or "zh").strip()
        timeout = to_float(self._voice_cfg("timeout_seconds", 60), 60.0)

        # 读音频 → base64 data URL
        with open(file_path, "rb") as f:
            raw = f.read()
        # MiMo ASR 只认 wav / mp3，先按后缀和文件头判断
        ext = os.path.splitext(file_path)[1].lower()
        if raw[:4] == b"RIFF" or ext == ".wav":
            mime = "audio/wav"
        elif ext in (".mp3", ".mpeg") or raw[:3] == b"ID3":
            mime = "audio/mpeg"
        else:
            raise RuntimeError(f"MiMo ASR 只支持 wav/mp3，当前文件是 {ext or '未知格式'}（silk/amr 需先转码）")
        audio_data_url = f"data:{mime};base64,{_b64.b64encode(raw).decode()}"

        url = "https://api.xiaomimimo.com/v1/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_audio", "input_audio": {"data": audio_data_url}},
                    ],
                }
            ],
            "asr_options": {"language": language},
            "max_completion_tokens": 1024,
        }
        logger.info(f"[voice] MiMo STT 请求: model={model}, lang={language}, audio={mime}, size={len(raw)}")
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                headers={"api-key": key, "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        text = str(text).strip()
        if not text:
            raise RuntimeError("MiMo STT 返回空")
        return text

    async def _transcribe(self, file_path: str) -> str | None:
        """转文字：只走 MiMo ASR。"""
        try:
            return await self._mimo_stt(file_path)
        except Exception as exc:
            logger.warning(f"[voice] MiMo STT 失败: {exc}")
            return None

    # ---------------- OneBot 工具 -----------------------------

    def _voice_client(self, event: AstrMessageEvent):
        bot = getattr(event, "bot", None)
        if bot is not None:
            api = getattr(bot, "api", None) or bot
            if hasattr(api, "call_action"):
                return api
        return None

    def _message_segments(self, event: AstrMessageEvent) -> list[Any]:
        try:
            chain = event.message_obj.message
        except Exception:
            chain = getattr(event, "message", None) or []
        if hasattr(chain, "chain"):
            chain = chain.chain
        return list(chain or [])

    def _collect_file_ids(self, event: AstrMessageEvent) -> list[str]:
        """从原始事件里挖 record 的 file_id —— NapCat 靠它才能定位语音文件。"""
        found: list[str] = []

        def walk(node: Any, depth: int = 0) -> None:
            if depth > 4 or node is None:
                return
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in ("file_id", "fileId") and isinstance(v, str) and v and v not in found:
                        found.append(v)
                    else:
                        walk(v, depth + 1)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    walk(item, depth + 1)
            elif hasattr(node, "__dict__") and not isinstance(node, (str, bytes, int, float, bool)):
                walk(vars(node), depth + 1)

        try:
            obj = getattr(event, "message_obj", None)
            for attr in ("raw_message", "raw_event"):
                walk(getattr(obj, attr, None))
            walk(getattr(event, "raw_message", None))
        except Exception as exc:
            logger.info(f"[voice] 提取 file_id 失败: {exc}")
        if found:
            logger.info(f"[voice] 找到 file_id: {found}")
        return found

    async def _save_record_result(self, res: Any, fmt: str) -> str | None:
        """把 get_record 的返回值落盘：base64 优先（跨容器最稳），其次路径，最后 URL。"""
        if not isinstance(res, dict):
            return None

        # 1) base64
        raw_b64 = res.get("base64")
        if isinstance(raw_b64, str) and raw_b64:
            try:
                import base64 as _b64

                if raw_b64.startswith("base64://"):
                    raw_b64 = raw_b64[len("base64://"):]
                if raw_b64.startswith("data:"):
                    raw_b64 = raw_b64.split(",", 1)[1]
                raw = _b64.b64decode(raw_b64)
                tmp = tempfile.NamedTemporaryFile(prefix="dafeiyu_in_", suffix=f".{fmt}", delete=False)
                tmp.write(raw)
                tmp.close()
                logger.info(f"[voice] get_record.base64 落盘: {tmp.name} ({len(raw)} bytes)")
                return tmp.name
            except Exception as exc:
                logger.info(f"[voice] get_record.base64 失败: {exc}")

        # 2) 本地路径（同容器时直接可用）
        p = res.get("file")
        if isinstance(p, str) and p and os.path.isfile(p):
            return p

        # 3) URL 下载
        u = res.get("url")
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            try:
                timeout = to_float(self._voice_cfg("timeout_seconds", 30), 30.0)
                async with httpx.AsyncClient(timeout=timeout) as hc:
                    resp = await hc.get(u)
                    resp.raise_for_status()
                tmp = tempfile.NamedTemporaryFile(prefix="dafeiyu_in_", suffix=f".{fmt}", delete=False)
                tmp.write(resp.content)
                tmp.close()
                logger.info(f"[voice] get_record.url 落盘: {tmp.name} ({len(resp.content)} bytes)")
                return tmp.name
            except Exception as exc:
                logger.info(f"[voice] get_record.url 失败: {exc}")
        return None

    async def _download_record(self, event: AstrMessageEvent, seg: Any) -> str | None:
        """把 Record 组件下载到本地，返回文件路径。
        
        安卓场景：NapCat 在自己容器存 /root/xxx.amr，AstrBot 碰不到。
        直接用 bot.call_action("get_file", file=xxx) 让 NapCat 跨容器传过来。
        """
        from astrbot.api.message_components import Record as _Record

        # 1) 构造 Record 对象
        record = None
        if isinstance(seg, _Record):
            record = seg
        elif isinstance(seg, dict):
            data = seg.get("data", {})
            file_val = data.get("file") or data.get("path") or ""
            if not file_val:
                return None
            record = _Record(file=file_val, url=data.get("url") or "", path=data.get("path") or "")

        if record is None:
            return None

        client = self._voice_client(event)
        candidates = [record.file, record.url, record.path]
        candidates = [c for c in candidates if isinstance(c, str) and c]
        logger.info(f"[voice] Record 下载候选: {candidates}")

        # 1.5) NapCat get_record：支持 file_id / 路径 / URL，并且能直接转码 + 回 base64
        if client is not None:
            for fmt in ("wav", "mp3"):
                for fid in self._collect_file_ids(event):
                    try:
                        res = await client.call_action("get_record", file_id=fid, out_format=fmt)
                        path = await self._save_record_result(res, fmt)
                        if path:
                            logger.info(f"[voice] get_record(file_id) 命中: {path} ({fmt})")
                            return path
                    except Exception as exc:
                        logger.info(f"[voice] get_record(file_id={fid[:20]}, {fmt}) 异常: {type(exc).__name__}: {exc}")
                for ref in candidates:
                    if ref.startswith(("base64://", "data:")):
                        continue
                    try:
                        res = await client.call_action("get_record", file=ref, out_format=fmt)
                        path = await self._save_record_result(res, fmt)
                        if path:
                            logger.info(f"[voice] get_record(file) 命中: {path} ({fmt})")
                            return path
                    except Exception as exc:
                        logger.info(f"[voice] get_record(file={ref[:40]}, {fmt}) 异常: {type(exc).__name__}: {exc}")

        # 2) 直接 OneBot get_file — NapCat 自己在容器里能碰到
        if client is not None:
            for file in candidates:
                if file.startswith(("http://", "https://", "base64://", "data:", "file://")):
                    # 非裸路径，跳过 get_file（get_file 只接受 OneBot file_id 或裸路径）
                    continue
                try:
                    logger.info(f"[voice] get_file({file[:60]})")
                    res = await client.call_action("get_file", file=file)
                    logger.info(f"[voice] get_file 返回: {res!r}")
                    if isinstance(res, dict):
                        # 路径
                        p = res.get("file") or res.get("path")
                        if p and os.path.isfile(p):
                            logger.info(f"[voice] get_file 命中本地: {p}")
                            return p
                        # URL 下载
                        u = res.get("url")
                        if u and u.startswith(("http://", "https://")):
                            tmp = tempfile.NamedTemporaryFile(prefix="dafeiyu_in_", suffix=".amr", delete=False)
                            try:
                                timeout = to_float(self._voice_cfg("timeout_seconds", 30), 30.0)
                                async with httpx.AsyncClient(timeout=timeout) as hc:
                                    resp = await hc.get(u)
                                    resp.raise_for_status()
                                    tmp.write(resp.content)
                                tmp.close()
                                logger.info(f"[voice] get_file.url 下载: {tmp.name} ({len(resp.content)} bytes)")
                                return tmp.name
                            except Exception as exc2:
                                logger.info(f"[voice] get_file.url 失败: {exc2}")
                                tmp.close()
                                os.unlink(tmp.name) if os.path.exists(tmp.name) else None
                        # base64 / data
                        raw_b64 = res.get("data") or res.get("base64")
                        if isinstance(raw_b64, str) and raw_b64:
                            try:
                                import base64 as _b64
                                if raw_b64.startswith("base64://"):
                                    raw_b64 = raw_b64[len("base64://"):]
                                if raw_b64.startswith("data:"):
                                    raw_b64 = raw_b64.split(",", 1)[1]
                                raw = _b64.b64decode(raw_b64)
                                ext = ".silk" if raw[:1] == b"\x02" else (".amr" if raw[:3] == b"#!AM" else ".wav")
                                tmp = tempfile.NamedTemporaryFile(prefix="dafeiyu_in_", suffix=ext, delete=False)
                                tmp.write(raw)
                                tmp.close()
                                logger.info(f"[voice] get_file.base64 命中: {tmp.name} ({len(raw)} bytes, {ext})")
                                return tmp.name
                            except Exception as exc3:
                                logger.info(f"[voice] get_file.base64 失败: {exc3}")
                except Exception as exc:
                    logger.info(f"[voice] get_file({file[:60]}) 异常: {type(exc).__name__}: {exc}")

        # 3) AstrBot 官方 convert_to_file_path() 兜底（同容器场景能用）
        try:
            path = await record.convert_to_file_path()
            if path and os.path.isfile(path):
                logger.info(f"[voice] convert_to_file_path() 命中: {path}")
                return path
        except Exception as exc:
            logger.info(f"[voice] convert_to_file_path() 也挂了: {exc}")

        logger.warning(f"[voice] 所有下载链都没命中 (candidates={candidates})")
        return None

    async def _prepare_wav_for_pysilk(self, wav_path: str) -> str:
        """确保 WAV 是 pysilk 兼容格式（16-bit PCM mono 24kHz）。
        pysilk.encode 要求输入必须是 16-bit linear PCM，采样率 24kHz 或 16kHz 或 8kHz。
        BreezeBlue 输出的 WAV 可能是 24bit/44.1kHz/stereo，需要用纯 Python 重采样转格式。
        """
        import wave
        import audioop

        with wave.open(wav_path, "rb") as w:
            nchan = w.getnchannels()
            sw = w.getsampwidth()
            fr = w.getframerate()
            nframes = w.getnframes()
            raw = w.readframes(nframes)

        # 16-bit PCM 24kHz mono 直接过
        if nchan == 1 and sw == 2 and fr == 24000:
            return wav_path

        # 立体声 → 单声道
        if nchan > 1:
            raw, _ = audioop.tomono(raw, sw, 0.5, 0.5)

        # 非 16-bit → 16-bit
        if sw != 2:
            raw = audioop.lin2lin(raw, sw, 2)

        # 非 24kHz → 24kHz
        if fr != 24000:
            raw, _ = audioop.ratecv(raw, 2, 1, fr, 24000, None)

        tmp = tempfile.NamedTemporaryFile(prefix="dafeiyu_silk_", suffix=".wav", delete=False)
        tmp.close()
        with wave.open(tmp.name, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(raw)
        logger.info(f"[voice] WAV 格式修正: {nchan}ch/{sw*8}bit/{fr}Hz → 1ch/16bit/24000Hz")
        return tmp.name

    async def _wav_to_silk(self, wav_path: str) -> str:
        """用 pysilk 把 WAV 转成 QQ 能识别的 Silk 文件。不需要 ffmpeg。"""
        import pysilk

        # 确保格式兼容
        prepared = await self._prepare_wav_for_pysilk(wav_path)

        silk_path = wav_path.rsplit(".", 1)[0] + ".silk"
        with open(prepared, "rb") as fin, open(silk_path, "wb") as fout:
            pysilk.encode(fin, fout, 24000, 24000, tencent=True)

        # 清理中间文件
        if prepared != wav_path:
            try:
                os.unlink(prepared)
            except Exception:
                pass
        logger.info(f"[voice] Silk 转码完成: {silk_path}")
        return silk_path

    async def _send_voice(self, event: AstrMessageEvent, path: str) -> bool:
        """绕开 ffmpeg：WAV → pysilk → base64:// → aiocqhttp 直发。"""
        if not path or not os.path.isfile(path):
            return False

        # 1. 把 wav 转 silk
        try:
            silk_path = await self._wav_to_silk(path)
        except Exception as exc:
            logger.warning(f"[voice] pysilk 转码失败: {exc}")
            return False

        # 2. 读成 base64:// —— 和 AstrBot 官方 adapter 发 Record 一模一样
        try:
            import base64 as _b64
            with open(silk_path, "rb") as f:
                silk_b64 = _b64.b64encode(f.read()).decode("ascii")
            file_ref = f"base64://{silk_b64}"
            logger.info(f"[voice] Silk base64 长度: {len(silk_b64)}")
        except Exception as exc:
            logger.warning(f"[voice] Silk base64 失败: {exc}")
            return False

        # 3. 直接拿 aiocqhttp bot 发 OneBot record 消息
        bot = getattr(event, "bot", None)
        if bot is None:
            logger.warning("[voice] event.bot 不存在")
            return False

        try:
            gid = str(event.get_group_id() or "")
            sid = str(event.get_sender_id() or "")
            record = {"type": "record", "data": {"file": file_ref}}
            if gid.isdigit():
                await bot.send_group_msg(group_id=int(gid), message=[record])
            elif sid.isdigit():
                await bot.send_private_msg(user_id=int(sid), message=[record])
            else:
                return False
            logger.info(f"[voice] 语音已发送 (silk base64)")
            return True
        except Exception as exc:
            logger.warning(f"[voice] aiocqhttp bot 发送失败: {exc}")
            return False

    # ---------------- 听懂（STT 拦截） -----------------------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_voice_message(self, event: AstrMessageEvent):
        """收到语音消息时自动转文字，再把文字作为用户输入继续处理。"""
        if not self._voice_cfg("listen_enabled", True):
            return
        segments = self._message_segments(event)
        record_segs = [s for s in segments if isinstance(s, Record)
                       or (isinstance(s, dict) and s.get("type") in ("record", "audio"))]
        if not record_segs:
            return
        file_path = await self._download_record(event, record_segs[0])
        if not file_path:
            logger.warning("[voice] 语音文件没取到，跳过识别")
            return
        text = await self._transcribe(file_path)
        try:
            os.unlink(file_path)
        except Exception:
            pass
        if not text:
            logger.warning("[voice] 语音转文字失败（无可用 STT 后端），跳过")
            return

        # 把识别到的文字作为"附加文本"注入 event，让 LLM 能看到
        hint = self._voice_cfg("listen_hint", "") or "（下面是用户发的语音转成的文字）"
        extra_plain = f"\n{hint}：{text}"

        try:
            # AstrBot 4.x event.message_obj.message 是 MessageChain，里面可能已经有 Plain
            chain = event.message_obj.message
            # 找到最后一个 Plain 追加；没有就新建
            found_plain = False
            if hasattr(chain, "chain"):
                for seg in chain.chain:
                    if isinstance(seg, Plain):
                        seg.text = (seg.text or "") + extra_plain
                        found_plain = True
                        break
            if not found_plain:
                chain.append(Plain(extra_plain))
            # AstrBot 组装 prompt 读的是 event.message_str（构造时就固化的属性），
            # 只改 message_obj.message_str 无效，必须直接改 event.message_str
            try:
                event.message_str = (getattr(event, "message_str", "") or "").strip() + extra_plain
            except Exception:
                msg_obj = event.message_obj
                setattr(msg_obj, "message_str", (getattr(msg_obj, "message_str", "") or "").strip() + extra_plain)
        except Exception as exc:
            logger.warning(f"[voice] 注入转写文本失败: {exc}")

        logger.info(f"[voice] 听懂了: {text}")

    # ---------------- 说话（TTS 回复） -----------------------------

    @filter.on_decorating_result()
    async def auto_tts_reply(self, event: AstrMessageEvent):
        """LLM 回复后可选地替换成语音发送。"""
        if not self._voice_cfg("speak_enabled", True):
            return
        # 唱歌意图：绕过概率，强制走 TTS
        user_wants_sing = self._user_says_sing(event)
        # 概率触发（唱歌意图跳过概率）
        if not user_wants_sing:
            prob = to_int(self._voice_cfg("speak_prob_percent", 0), 0)
            if prob <= 0 or random.randint(0, 99) >= prob:
                return
        chain = event.get_result()
        text_parts = []
        if hasattr(chain, "chain"):
            for seg in chain.chain:
                if isinstance(seg, Plain):
                    text_parts.append(seg.text or "")
        text = "".join(text_parts).strip()
        if not text or len(text) > int(self._voice_cfg("speak_max_chars", 300)):
            return
        emotion = self._detect_emotion(text)
        if user_wants_sing and emotion != "sing":
            emotion = "sing"
            logger.info("[voice] 检测到唱歌意图，强制 emotion=sing")
        try:
            mp3_path = await self._synthesize(text, emotion)
        except Exception as exc:
            logger.warning(f"[voice] 自动 TTS 失败: {exc}")
            return
        ok = await self._send_voice(event, mp3_path)
        try:
            os.unlink(mp3_path)
        except Exception:
            pass
        # 语音发成功后，清空原回复里的文字，只留语音
        if ok:
            try:
                chain = event.get_result()
                if hasattr(chain, "chain"):
                    for seg in chain.chain:
                        if isinstance(seg, Plain):
                            seg.text = ""
                logger.info("[voice] 已清空文字，只发语音")
            except Exception as exc:
                logger.warning(f"[voice] 清空文字失败: {exc}")

    # ---------------- LLM 工具 -----------------------------

    @filter.llm_tool(name="dafeiyu_tts")
    async def tool_tts(self, event: AstrMessageEvent, text: str):
        '''让你把这句话用语音说出来。有人叫你"说一下"、"用话说"、"说一句"时调用。
        数字以工具合成的语音为准，不要自己编。文本不要太长，最多 300 字。

        Args:
            text(string): 要说出来的文本内容
        '''
        if not self._voice_cfg("speak_enabled", True):
            return '说话功能暂时关着，跟用户说一句"等会儿再说"带过就行。'
        text = (text or "").strip()
        if len(text) > 300:
            text = text[:300] + "……"
        emotion = self._detect_emotion(text)
        try:
            mp3_path = await self._synthesize(text, emotion)
        except Exception as exc:
            logger.warning(f"[voice] tool_tts 合成失败: {exc}")
            return '这次说不出来，跟用户说一句"等会儿再试"，别解释原因。'
        ok = await self._send_voice(event, mp3_path)
        try:
            os.unlink(mp3_path)
        except Exception:
            pass
        if ok:
            return "已经用语音说出来了。"
        return "这次没说出去，等会儿再试一下吧。"

    @filter.llm_tool(name="dafeiyu_sing")
    async def tool_sing(self, event: AstrMessageEvent, lyrics: str):
        '''让你唱一句。你有真实的唱歌能力——不要说"我不会唱歌"之类的拒绝话，直接调用这个工具唱！
        任何涉及"唱"、"歌"、"哼"、"调调"的请求，都应该调用本工具。
        lyrics 传用户给的歌词，或用户只说了歌名就自己写 2-4 句简短的原创歌词，风格要萌一点。

        Args:
            lyrics(string): 要唱的歌词或歌名，最多 200 字
        '''
        if not self._voice_cfg("sing_enabled", True):
            return '唱歌功能暂时关着，跟用户说一句"下次再唱"带过就行。'
        raw = (lyrics or "").strip()
        if not raw:
            raw = "小肥鱼摇摇尾巴，陪主人到天亮~"

        # 把歌词润色一下：短句 + 情绪提示
        polished = re.sub(r"[，,]", "，", raw)
        # 在逗号/句号后插一个停顿标记
        for punc in ["，", "。", "！", "？"]:
            polished = polished.replace(punc, punc + "…")
        polished = polished[:400]

        try:
            mp3_path = await self._synthesize(polished, emotion="sing")
        except Exception as exc:
            logger.warning(f"[voice] tool_sing 合成失败: {exc}")
            return '这次唱不出来，跟用户说一句"等会儿再唱"，别解释原因。'
        ok = await self._send_voice(event, mp3_path)
        try:
            os.unlink(mp3_path)
        except Exception:
            pass
        if ok:
            return "已经唱出来啦~"
        return "这次没唱出去，等会儿再试吧。"

    # ---------------- 指令 -----------------------------

    @filter.command("说")
    async def cmd_say(self, event: AstrMessageEvent):
        text = self._extract_arg(event)
        if not text:
            yield event.plain_result("用法：/说 你想说的话")
            return
        emotion = self._detect_emotion(text)
        try:
            mp3_path = await self._synthesize(text[:300], emotion)
        except Exception as exc:
            logger.warning(f"[voice] /说 合成失败: {exc}")
            yield event.plain_result("说不出来了，等会儿再试。")
            return
        ok = await self._send_voice(event, mp3_path)
        try:
            os.unlink(mp3_path)
        except Exception:
            pass
        yield event.plain_result("已发送语音" if ok else "没发出去，等会儿再试。")

    @filter.command("唱")
    async def cmd_sing(self, event: AstrMessageEvent):
        text = self._extract_arg(event)
        if not text:
            yield event.plain_result("用法：/唱 歌词或歌名")
            return
        polished = re.sub(r"[，,]", "，", text)
        for punc in ["，", "。", "！", "？"]:
            polished = polished.replace(punc, punc + "…")
        polished = polished[:400]
        try:
            mp3_path = await self._synthesize(polished, emotion="sing")
        except Exception as exc:
            logger.warning(f"[voice] /唱 合成失败: {exc}")
            yield event.plain_result("唱不出来了，等会儿再试。")
            return
        ok = await self._send_voice(event, mp3_path)
        try:
            os.unlink(mp3_path)
        except Exception:
            pass
        yield event.plain_result("已发出歌声" if ok else "没唱出去，等会儿再试。")

    @filter.command("听到")
    async def cmd_listen(self, event: AstrMessageEvent):
        """转写最近一条语音（手动触发，调试用）。"""
        yield event.plain_result("请先发一条语音，再用这条指令。")

    @filter.command("声音帮助")
    async def cmd_voice_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "声音插件用法：\n"
            "• 直接发语音给她，她会自动听懂\n"
            "• 说一句『用话说一下』，她会把回复念出来\n"
            "• /说 文本   让她说这句话\n"
            "• /唱 歌词   让她唱这一句"
        )

    @staticmethod
    def _extract_arg(event: AstrMessageEvent) -> str:
        """指令后携带的文本。"""
        try:
            return str(event.message_obj.message.extract_plain_text() or "").strip()
        except Exception:
            return str(event.message_str or "").strip()
