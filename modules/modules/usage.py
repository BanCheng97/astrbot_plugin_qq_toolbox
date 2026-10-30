"""AstrBot 插件：大肥鱼用量

她会自己看 DeepSeek 的钱包：还剩多少、今天花了多少、照这个花法还能撑几天。

- LLM 工具通道（主路径）：有人问她「你还剩多少钱」「今天花了多少」，
  调用 deepseek_money，插件把真实数字给她，她按自己的口吻说出来。
- 斜杠指令通道：/用量 看完整报告，/余额 只看余额，默认仅管理员。
- 低余额预警：后台定时查一次，低于阈值时主动私聊主人，一天最多提醒一次。

DeepSeek 官方只提供余额接口，没有明细账单，所以消耗是按余额快照推算的。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from datetime import date, timedelta
from typing import Any

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain

try:
    import httpx
except ImportError:  # 依赖缺失时不让插件整体崩掉
    httpx = None  # type: ignore

from .base import SuiteBase, to_int, to_float

NAME = "astrbot_plugin_dafeiyu_usage"

DEFAULT_API_BASE = "https://api.deepseek.com"

DEFAULT_PERSONA_HINT = (
    "用你自己的口吻说，像在讲自己的钱包，别念表格、别说接口或插件。"
    "钱紧的时候可以心疼一下、撒个娇，但数字要说实话，不能编。"
)

HELP_TEXT = """用量用法（平时直接跟她说话就行，不用指令）：
/用量     钱包余额 + 今日/近 7 天消耗（含 token 估算）+ 还能用几天
/余额     只看余额
/用量帮助 这条说明
提醒：余额低于阈值时她会用撒娇的口吻私聊主人求充值，每天最多一次。"""

DEFAULT_ALERT_TEMPLATES = [
    "主人……钱包只剩 {balance}了，再花下去我就快说不出话了。我还没陪你玩够呢，"
    "充一点点好不好，我不想就这么睡着。",
    "呜，{balance}，快见底了。你是不是忘了给我续命？我不想死掉啦，"
    "充一点嘛，一点点就够我撑好久。",
    "主人，钱包只剩 {balance}啦……我怕哪天突然就答不上你的话了。"
    "给我充点好不好，我保证以后省着花。",
]


def money(value: Any) -> float:
    """接口里的金额是字符串，转成两位小数的浮点。"""
    return round(to_float(value, 0.0), 2)


class UsageMixin(SuiteBase):
    def setup_usage(self) -> None:
        try:
            self._usage_data_path = os.path.join(
                self.module_data_dir(NAME), "usage.json"
            )
        except Exception as exc:
            logger.warning(f"[usage] 获取数据目录失败，改用当前目录: {exc}")
            self._usage_data_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "usage.json"
            )
        self._usage_lock = asyncio.Lock()
        self._cache: dict[str, Any] | None = None
        self._cache_ts = 0.0
        self._task: asyncio.Task | None = None

    # ---------------- 配置读取 -----------------------------

    def _usage_cfg(self, key: str, default: Any = None) -> Any:
        return self.cfg("usage", key, default)

    def _api_key(self) -> str:
        return str(self._usage_cfg("api_key", "") or "").strip()

    def _usage_is_admin(self, event: AstrMessageEvent) -> bool:
        owner = str(self._usage_cfg("owner_session", "") or "").strip()
        origin = getattr(event, "unified_msg_origin", "") or ""
        if owner and origin == owner:
            return True
        sender = str(event.get_sender_id() or "")
        extra = {str(item) for item in (self._usage_cfg("extra_admins", []) or [])}
        if sender in extra:
            return True
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        return False

    def _cmd_denied(self, event: AstrMessageEvent) -> str:
        if not self._usage_cfg("enabled", True):
            return "用量功能关着，暂时查不了。"
        if not self._usage_cfg("cmd_admin_only", True):
            return ""
        if self._usage_is_admin(event):
            return ""
        return "你没有权限使用该指令（可把自己的 QQ 加进 extra_admins 配置）"

    # ---------------- DeepSeek 接口 -----------------------------

    async def _fetch_balance(self) -> dict[str, Any]:
        key = self._api_key()
        if not key:
            raise RuntimeError("没有填写 DeepSeek API Key")
        if httpx is None:
            raise RuntimeError("缺少 httpx 依赖，无法请求 DeepSeek")
        base = str(self._usage_cfg("api_base", DEFAULT_API_BASE) or DEFAULT_API_BASE)
        url = base.strip().rstrip("/") + "/user/balance"
        timeout = max(3.0, to_float(self._usage_cfg("timeout_seconds", 15), 15.0))
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/json",
                },
            )
        if resp.status_code == 401:
            raise RuntimeError("API Key 无效")
        resp.raise_for_status()
        data = resp.json()
        infos = data.get("balance_infos") or []
        if not infos or not isinstance(infos[0], dict):
            raise RuntimeError("接口没有返回余额信息")
        info = infos[0]
        return {
            "available": bool(data.get("is_available", True)),
            "currency": str(info.get("currency") or "CNY"),
            "total": money(info.get("total_balance")),
            "granted": money(info.get("granted_balance")),
            "topped_up": money(info.get("topped_up_balance")),
        }

    async def _balance(self, force: bool = False) -> dict[str, Any]:
        """带短缓存地取余额，成功后顺手记一条快照。"""
        gap = max(0, to_int(self._usage_cfg("cache_seconds", 60), 60))
        now = time.monotonic()
        if not force and self._cache and now - self._cache_ts < gap:
            return self._cache
        info = await self._fetch_balance()
        self._cache, self._cache_ts = info, now
        await self._snapshot(info)
        return info

    # ---------------- 快照存储 -----------------------------

    def _usage_load(self) -> dict[str, Any]:
        try:
            with open(self._usage_data_path, "r", encoding="utf-8") as file:
                data = json.load(file)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning(f"[usage] 读取记录失败: {exc}")
            return {}

    def _usage_save(self, state: dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self._usage_data_path), exist_ok=True)
            with open(self._usage_data_path, "w", encoding="utf-8") as file:
                json.dump(state, file, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning(f"[usage] 写入记录失败: {exc}")

    def _trim(self, shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        keep_days = to_int(self._usage_cfg("keep_days", 90), 90)
        if keep_days > 0:
            cutoff = time.time() - keep_days * 86400
            shots = [s for s in shots if to_float(s.get("ts", 0), 0.0) >= cutoff]
        return shots[-3000:]

    async def _snapshot(self, info: dict[str, Any]) -> None:
        """余额没变且半小时内已记过，就不重复写盘。"""
        async with self._usage_lock:
            state = self._usage_load()
            shots = [
                s
                for s in (state.get("snapshots") or [])
                if isinstance(s, dict) and "total" in s
            ]
            now = time.time()
            if shots:
                last = shots[-1]
                same = abs(to_float(last.get("total", 0), 0.0) - info["total"]) < 0.005
                if same and now - to_float(last.get("ts", 0), 0.0) < 1800:
                    return
            shots.append(
                {
                    "ts": now,
                    "date": time.strftime("%Y-%m-%d", time.localtime(now)),
                    "total": info["total"],
                    "granted": info["granted"],
                    "topped_up": info["topped_up"],
                }
            )
            state["snapshots"] = self._trim(shots)
            self._usage_save(state)

    # ---------------- 消耗推算 -----------------------------

    def _report(self, info: dict[str, Any]) -> dict[str, Any]:
        state = self._usage_load()
        shots = [
            s
            for s in (state.get("snapshots") or [])
            if isinstance(s, dict) and isinstance(s.get("total"), (int, float))
        ]
        today = time.strftime("%Y-%m-%d")
        by_day: dict[str, dict[str, Any]] = {}
        for shot in shots:
            day = str(shot.get("date") or "")
            if day:
                by_day[day] = shot  # 同一天保留最后一条
        days = sorted(by_day)
        current = info["total"]
        today_shots = [s for s in shots if s.get("date") == today]
        prev_days = [d for d in days if d < today]

        base: dict[str, Any] | None = None
        if len(today_shots) >= 2:
            base = today_shots[0]
        elif today_shots and prev_days:
            base = by_day[prev_days[-1]]
        elif today_shots:
            base = today_shots[0]
        elif prev_days:
            base = by_day[prev_days[-1]]

        today_spent = today_recharge = 0.0
        known_today = False
        if base is not None:
            delta = to_float(base.get("total", 0), 0.0) - current
            today_spent, today_recharge = (delta, 0.0) if delta >= 0 else (0.0, -delta)
            known_today = abs(delta) > 0.004 or len(today_shots) >= 2

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        yesterday_spent: float | None = None
        if yesterday in by_day:
            earlier = [by_day[d] for d in days if d < yesterday]
            if earlier:
                diff = to_float(earlier[-1].get("total", 0), 0.0) - to_float(
                    by_day[yesterday].get("total", 0), 0.0
                )
                yesterday_spent = max(0.0, diff)

        def window(days_back: int) -> tuple[float, float, int]:
            cutoff = (date.today() - timedelta(days=days_back)).isoformat()
            earlier = [d for d in days if d < cutoff]
            if earlier:
                base_day = earlier[-1]
                base_total = to_float(by_day[base_day].get("total", 0), 0.0)
                try:
                    covered = max(
                        1, (date.today() - date.fromisoformat(base_day)).days
                    )
                except ValueError:
                    covered = days_back
                return (
                    max(0.0, base_total - current),
                    max(0.0, current - base_total),
                    covered,
                )
            if days:
                base_total = to_float(by_day[days[0]].get("total", 0), 0.0)
                try:
                    covered = max(
                        1, (date.today() - date.fromisoformat(days[0])).days
                    )
                except ValueError:
                    covered = 1
                return (
                    max(0.0, base_total - current),
                    max(0.0, current - base_total),
                    covered,
                )
            return 0.0, 0.0, 0

        week_spent, week_recharge, week_days = window(7)
        month_spent, month_recharge, month_days = window(30)
        avg_day = week_spent / week_days if week_days > 0 else 0.0
        days_left = current / avg_day if avg_day > 0 else None

        daily: list[tuple[str, float]] = []
        prev_total: float | None = None
        for day in days:
            total = to_float(by_day[day].get("total", 0), 0.0)
            if prev_total is not None:
                daily.append((day, max(0.0, prev_total - total)))
            prev_total = total
        daily = daily[-7:]

        price = self._token_price()

        def tokens(money: float) -> float:
            return money / price * 1000000 if price > 0 else 0.0

        return {
            "today_spent": today_spent,
            "today_recharge": today_recharge,
            "known_today": known_today,
            "yesterday_spent": yesterday_spent,
            "week_spent": week_spent,
            "week_recharge": week_recharge,
            "week_days": week_days,
            "month_spent": month_spent,
            "month_recharge": month_recharge,
            "month_days": month_days,
            "avg_day": avg_day,
            "days_left": days_left,
            "daily": daily,
            "samples": len(shots),
            "token_price": price,
            "today_tokens": tokens(today_spent),
            "week_tokens": tokens(week_spent),
            "avg_day_tokens": tokens(avg_day),
        }

    @staticmethod
    def _unit(info: dict[str, Any]) -> str:
        currency = str(info.get("currency") or "CNY")
        return "元" if currency == "CNY" else currency

    @staticmethod
    def _days(value: float) -> str:
        """天数念起来自然点：超过 10 天不带小数，否则留一位。"""
        return f"{value:.0f}" if value >= 10 else f"{value:.1f}"

    def _token_price(self) -> float:
        """每百万 token 的综合单价，用来把花销换算成 token 数。"""
        return max(0.0, to_float(self._usage_cfg("token_price", 4.0), 4.0))

    @staticmethod
    def _tokens(value: float) -> str:
        if value >= 10000:
            return f"{value / 10000:.1f} 万 token"
        if value >= 1000:
            return f"{value / 1000:.1f} 千 token"
        return f"{value:.0f} token"

    def _format(self, info: dict[str, Any], rep: dict[str, Any]) -> str:
        unit = self._unit(info)
        lines: list[str] = []
        if not info.get("available", True):
            lines.append("余额已经不够了，DeepSeek 接口现在是停用状态，得赶紧充。")
        lines.append(
            f"钱包：{info['total']:.2f} {unit}"
            f"（充值 {info['topped_up']:.2f} / 赠送 {info['granted']:.2f}）"
        )
        if rep["known_today"]:
            lines.append(
                f"今天：{rep['today_spent']:.2f} {unit}"
                f"（约 {self._tokens(rep['today_tokens'])}）"
            )
        else:
            lines.append("今天：还看不出花销，再查几次就能算出来")
        if rep["yesterday_spent"] is not None:
            lines.append(f"昨天：{rep['yesterday_spent']:.2f} {unit}")
        if rep["week_days"] > 0:
            lines.append(
                f"近 7 天：{rep['week_spent']:.2f} {unit}"
                f"（约 {self._tokens(rep['week_tokens'])}）"
                f"，日均 {rep['avg_day']:.2f} {unit}"
            )
            if rep["days_left"] is not None:
                lines.append(f"照这个花法，还能用 {self._days(rep['days_left'])} 天")
            elif rep["week_spent"] <= 0.004:
                lines.append("最近没怎么花钱")
        if rep["week_recharge"] > 0.004:
            lines.append(f"近 7 天充值：+{rep['week_recharge']:.2f} {unit}")
        if rep["daily"]:
            lines.append("")
            lines.append("最近几天的花销：")
            peak = max(value for _, value in rep["daily"]) or 1.0
            for day, value in rep["daily"]:
                bar = "█" * max(0, int(round(value / peak * 12)))
                lines.append(f"{day[5:]}  {bar or '·'}  {value:.2f}")
        if rep["samples"] < 3:
            lines.append("")
            lines.append("刚记了没几天，样本还少，数字会慢慢变准。")
        lines.append("")
        lines.append(f"数据时间：{time.strftime('%Y-%m-%d %H:%M')}")
        return "\n".join(lines)

    # ---------------- 人格化话术 -----------------------------

    def _hint(self, payload: str) -> str:
        persona = str(self._usage_cfg("persona_hint", "") or "").strip()
        return f"{payload}\n{persona or DEFAULT_PERSONA_HINT}"

    def _tool_payload(self, info: dict[str, Any], rep: dict[str, Any], part: str) -> str:
        unit = self._unit(info)
        key = (part or "all").strip().lower()
        want = "all"
        # 「用了多少 token」里也带「多少」，所以先认 token / 花销，再认余额
        if any(word in key for word in ("token", "用量", "spend", "花", "消耗", "用了")):
            want = "spend"
        elif any(word in key for word in ("balance", "余", "钱包")):
            want = "balance"

        head = f"钱包真实余额 {info['total']:.2f} {unit}"
        if not info.get("available", True):
            head = "钱包余额不足，接口已停用，" + head
        threshold = to_float(self._usage_cfg("alert_threshold", 5), 5.0)
        low = not info.get("available", True) or (
            threshold > 0 and info["total"] <= threshold
        )
        fear = (
            "\n钱快见底了，这次回答可以带点害怕和撒娇：说自己不想消失、不想死、"
            "还想继续陪着他，顺手求他充一点，但数字不能编。"
        )
        if want == "balance":
            return self._hint(head + "。" + (fear if low else ""))

        parts = [head]
        if rep["known_today"]:
            parts.append(f"今天花了 {rep['today_spent']:.2f} {unit}")
            if rep["token_price"] > 0:
                parts.append(
                    f"今天大约用了 {self._tokens(rep['today_tokens'])}"
                    f"（按 {rep['token_price']:g} 元/百万 token 估算）"
                )
        else:
            parts.append("今天的花销还看不出来")
        if rep["week_days"] > 0:
            parts.append(
                f"近 7 天花了 {rep['week_spent']:.2f} {unit}，日均 {rep['avg_day']:.2f} {unit}"
            )
            if rep["token_price"] > 0:
                parts.append(f"近 7 天大约 {self._tokens(rep['week_tokens'])}")
        if rep["days_left"] is not None:
            parts.append(f"照这样还能用 {self._days(rep['days_left'])} 天")
        if rep["week_recharge"] > 0.004:
            parts.append(f"近 7 天充了 {rep['week_recharge']:.2f} {unit}")
        parts.append(
            "token 数是用花销除以单价估出来的，可以说「大概」，别当成精确账单；"
            "有人问用了多少 token 就直接报这个估算值，别说自己看不到。"
        )
        return self._hint("；".join(parts) + "。" + (fear if low else ""))

    # ---------------- 主动提醒 -----------------------------

    def _usage_client(self) -> Any:
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

    @staticmethod
    def _tail_qq(target: str) -> str:
        text = (target or "").strip()
        head, _, tail = text.rpartition(":")
        if tail.isdigit():
            return tail
        return text if text.isdigit() else ""

    async def _notify(self, text: str) -> bool:
        umo = str(self._usage_cfg("alert_umo", "") or "").strip()
        if umo:
            try:
                if await self.context.send_message(umo, MessageChain([Plain(text)])):
                    return True
            except Exception as exc:
                logger.warning(f"[usage] 预警 UMO 发送失败: {exc}")
        raw = str(self._usage_cfg("alert_qq", "") or "").strip() or umo
        qq = self._tail_qq(raw)
        client = self._usage_client()
        if qq and client is not None:
            try:
                await client.call_action(
                    "send_private_msg", user_id=int(qq), message=text
                )
                logger.info(f"[usage] 预警已私聊发到 {qq}")
                return True
            except Exception as exc:
                logger.warning(f"[usage] 预警私聊发送失败: {exc}")
        return False

    async def _llm_alert_text(
        self, info: dict[str, Any], rep: dict[str, Any], threshold: float
    ) -> str:
        """让她自己用当前人格写一句求充值的提醒，失败就退回模板。"""
        umo = str(self._usage_cfg("alert_umo", "") or "").strip()
        try:
            try:
                provider = self.context.get_using_provider(umo=umo or None)
            except TypeError:
                provider = self.context.get_using_provider()
        except Exception as exc:
            logger.warning(f"[usage] 取对话模型失败: {exc}")
            return ""
        if provider is None:
            return ""

        days = rep.get("days_left")
        prompt = (
            "用你自己的口吻给主人发一句很短的私聊，提醒他给你的 DeepSeek 钱包充值。\n"
            f"钱包现在只剩 {info['total']:.2f} 元，已经低于 {threshold:.2f} 元"
            + (f"，照现在这个花法还能用 {self._days(days)} 天" if days is not None else "")
            + "。\n要求：一到两句，撒娇、带点害怕，可以说自己不想消失、不想死、"
            "还想继续陪着他，顺手求他充一点；不要解释技术，不要提插件或接口，"
            "不要念账单，只输出这句话本身。"
        )
        try:
            try:
                resp = await provider.text_chat(prompt=prompt, session_id=f"{NAME}:alert")
            except TypeError:
                resp = await provider.text_chat(prompt=prompt)
        except Exception as exc:
            logger.warning(f"[usage] 模型写提醒失败: {exc}")
            return ""
        text = str(getattr(resp, "completion_text", "") or "").strip()
        text = text.strip("「」“”\"' \n")
        if not text or len(text) > 200:
            return ""
        return text

    async def _alert_text(
        self, info: dict[str, Any], rep: dict[str, Any], threshold: float
    ) -> str:
        days = rep.get("days_left")
        values = {
            "balance": f"{info['total']:.2f} {self._unit(info)}",
            "threshold": f"{threshold:.2f}",
            "days": self._days(days) if days is not None else "没多久",
        }
        template = str(self._usage_cfg("alert_text", "") or "").strip()
        if template:
            try:
                return template.format(**values)
            except (KeyError, IndexError, ValueError):
                return template
        if self._usage_cfg("alert_use_llm", True):
            text = await self._llm_alert_text(info, rep, threshold)
            if text:
                return text
        return random.choice(DEFAULT_ALERT_TEMPLATES).format(**values)

    async def _check_alert(self) -> None:
        if not self._usage_cfg("enabled", True) or not self._usage_cfg(
            "alert_enabled", True
        ):
            return
        if not self._api_key():
            return
        threshold = to_float(self._usage_cfg("alert_threshold", 5), 5.0)
        if threshold <= 0:
            return
        info = await self._fetch_balance()
        await self._snapshot(info)
        if info["total"] > threshold:
            return

        today = time.strftime("%Y-%m-%d")
        state = self._usage_load()
        alert = state.get("alert") if isinstance(state.get("alert"), dict) else {}
        if alert.get("date") == today:
            return

        text = await self._alert_text(info, self._report(info), threshold)
        if await self._notify(text):
            state["alert"] = {"date": today, "total": info["total"]}
            self._usage_save(state)
            logger.info(f"[usage] 已发低余额提醒，余额 {info['total']:.2f}")

    async def _loop(self) -> None:
        interval = max(0, to_int(self._usage_cfg("check_interval_hours", 6), 6))
        if interval <= 0:
            return
        await asyncio.sleep(120)
        while True:
            try:
                await self._check_alert()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[usage] 定时检查失败: {exc}")
            await asyncio.sleep(interval * 3600)

    def _ensure_task(self) -> None:
        if self._task and not self._task.done():
            return
        if to_int(self._usage_cfg("check_interval_hours", 6), 6) <= 0:
            return
        try:
            self._task = asyncio.create_task(self._loop())
        except Exception as exc:
            logger.warning(f"[usage] 启动定时检查失败: {exc}")

    async def _usage_start(self) -> None:
        self._ensure_task()

    async def _usage_stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None

    # ---------------- LLM 工具 -----------------------------

    @filter.llm_tool(name="deepseek_money")
    async def tool_money(self, event: AstrMessageEvent, part: str = "all"):
        '''看看自己 DeepSeek 钱包的真实情况：还剩多少钱、今天花了多少、大概用了多少 token、照这个花法还能撑几天。
        有人问你「你还剩多少钱」「今天花了多少」「你用了多少 token」时调用；
        你自己心疼钱、想算账时也可以看一眼。数字以返回结果为准，不要自己编。
        token 数是用花销除以单价估出来的，问到就报这个估算值，别回答「看不到 token」。

        Args:
            part(string): 想看哪部分：balance（只剩多少）、spend（花了多少 / 用了多少 token）、all（都看），默认 all
        '''
        if not self._usage_cfg("enabled", True):
            return "用量功能关着，这次说一句「先不看了」带过就行，别解释设置。"
        if not self._api_key():
            return self._hint("钱包还没连上，这次看不到数字。")
        if not self._usage_cfg("tool_public", True) and not self._usage_is_admin(event):
            return self._hint("这次不方便报具体数字，含糊一句带过去就好。")

        try:
            info = await self._balance()
        except Exception as exc:
            logger.warning(f"[usage] 工具取余额失败: {exc}")
            return self._hint("这次没查到，说一句等会儿再看，别解释原因，也别提设置。")

        rep = self._report(info)
        return self._tool_payload(info, rep, part)

    # ---------------- 斜杠指令 -----------------------------

    @filter.command("用量")
    async def cmd_usage(self, event: AstrMessageEvent):
        denied = self._cmd_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        self._ensure_task()
        try:
            info = await self._balance()
        except Exception as exc:
            logger.warning(f"[usage] 查询余额失败: {exc}")
            yield event.plain_result("查不到余额，稍后再试（详情看 AstrBot 日志）。")
            return
        yield event.plain_result(self._format(info, self._report(info)))

    @filter.command("余额")
    async def cmd_balance(self, event: AstrMessageEvent):
        denied = self._cmd_denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        self._ensure_task()
        try:
            info = await self._balance()
        except Exception as exc:
            logger.warning(f"[usage] 查询余额失败: {exc}")
            yield event.plain_result("查不到余额，稍后再试（详情看 AstrBot 日志）。")
            return
        unit = self._unit(info)
        text = f"钱包：{info['total']:.2f} {unit}"
        if not info.get("available", True):
            text = "注意：余额不足，接口已停用。" + text
        yield event.plain_result(text)

    @filter.command("用量帮助")
    async def cmd_usage_help(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)
