"""
notifier/telegram.py — Telegram message sender
Tất cả format message ở đây.
"""

import asyncio
from typing import Optional
import httpx

from config.settings import TELEGRAM_TOKEN, CHANNEL_ID, MY_CHAT_ID
from strategy.grid_pyramid_v9_optimized import Signal
from utils.logger import log


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _price(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    elif value >= 1:
        return f"{value:.4f}"
    else:
        return f"{value:.6f}"


def _rr(value: float) -> str:
    return f"1:{value:.1f}"


class TelegramNotifier:

    BASE = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self):
        self.token   = TELEGRAM_TOKEN
        self.channel = CHANNEL_ID
        self.me      = MY_CHAT_ID

    async def _send(self, chat_id: str, text: str,
                    reply_markup: Optional[dict] = None) -> Optional[int]:
        """Send message. Returns message_id on success."""
        url     = self.BASE.format(token=self.token, method="sendMessage")
        payload = {
            "chat_id"   : chat_id,
            "text"      : text,
            "parse_mode": "HTML",
        }
        if reply_markup:
            import json
            payload["reply_markup"] = json.dumps(reply_markup)

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                data = resp.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            log.warning("telegram.send_failed", error=data.get("description"))
        except Exception as e:
            log.error("telegram.exception", error=str(e))
        return None

    async def _edit(self, chat_id: str, message_id: int, text: str) -> bool:
        url     = self.BASE.format(token=self.token, method="editMessageText")
        payload = {
            "chat_id"   : chat_id,
            "message_id": message_id,
            "text"      : text,
            "parse_mode": "HTML",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                return resp.json().get("ok", False)
        except Exception as e:
            log.error("telegram.edit_exception", error=str(e))
            return False

    # ── Signal messages ───────────────────────────────────────────────────

    def _conf_bar(self, confidence: float) -> str:
        """Visual confidence bar."""
        filled = round(confidence * 10)
        bar    = "█" * filled + "░" * (10 - filled)
        return f"[{bar}] {_pct(confidence)}"

    async def send_signal(self, signal: Signal, display_name: str,
                          executed: bool = False, order_id: str = "",
                          strategy_name: str = "") -> None:
        """Send new LONG/SHORT signal to channel."""
        # [FILTER] Only send if confidence >= 58%
        if signal.confidence < 0.58:
            log.info("telegram.signal_filtered", symbol=display_name, confidence=signal.confidence)
            return

        d     = signal.direction
        emoji = "🟢" if d == "LONG" else "🔴"
        exec_str = "✅ Đã vào lệnh tự động" if executed else "⚡ Signal mới"

        text = (
            f"{emoji} <b>{d} — {display_name}</b>\n"
            f"{'━' * 28}\n"
            f"📊 Khung      : M15\n"
            f"💰 Giá vào    : <b>{_price(signal.price)}</b>\n"
            f"🎯 TP1        : {_price(signal.tp1_price)}  "
            f"<i>(+{abs(signal.tp1_price - signal.price) / signal.price * 100:.2f}%)</i>\n"
            f"🎯 TP2        : {_price(signal.tp2_price)}  "
            f"<i>(+{abs(signal.tp2_price - signal.price) / signal.price * 100:.2f}%)</i>\n"
            f"🛑 SL         : {_price(signal.sl_price)}  "
            f"<i>(-{abs(signal.sl_price - signal.price) / signal.price * 100:.2f}%)</i>\n"
            f"📈 R:R        : {_rr(signal.rr_ratio)}\n"
            f"{'━' * 28}\n"
            f"🔥 Độ tự tin  : {self._conf_bar(signal.confidence)}\n"
            f"📉 ATR ratio  : {signal.atr_ratio:.2f}x\n"
            f"⚡ Momentum   : {'↑↑ Mạnh' if signal.momentum_score > 1 else '↑ Bình thường' if signal.momentum_score > 0 else '↓ Yếu'}\n"
            f"{'━' * 28}\n"
            f"🤖 {exec_str}"
            + (f"\n🆔 Order: <code>{order_id}</code>" if order_id else "")
            + (f"\n🧠 Strat: <code>{strategy_name}</code>" if strategy_name else "")
        )
        await self._send(self.channel, text)

    async def send_add_layer(self, signal: Signal, display_name: str,
                              executed: bool = False, strategy_name: str = "") -> None:
        """[FILTERED] Skip add layer messages."""
        return

    async def send_reduce(self, signal: Signal, display_name: str,
                           executed: bool = False, strategy_name: str = "") -> None:
        """[FILTERED] Skip reduce messages."""
        return

    async def send_close(self, display_name: str, direction: str,
                          reason: str, pnl_pct: float,
                          executed: bool = False, strategy_name: str = "") -> None:
        emoji  = "✅" if pnl_pct >= 0 else "❌"
        d_emoji= "🟢" if direction == "LONG" else "🔴"
        text   = (
            f"{emoji} <b>ĐÓNG LỆNH — {display_name}</b>\n"
            f"{'━' * 28}\n"
            f"{d_emoji} Hướng   : {direction}\n"
            f"📋 Lý do  : {reason}\n"
            f"💰 P&L    : <b>{'%.2f' % pnl_pct}%</b>\n"
            f"{'━' * 28}\n"
            f"🤖 {'✅ Đã đóng tự động' if executed else '⚡ Tín hiệu đóng'}"
            + (f"\n🧠 Strat: <code>{strategy_name}</code>" if strategy_name else "")
        )
        await self._send(self.channel, text)

    async def send_weak_trend(self, signal: Signal,
                               display_name: str) -> None:
        d     = signal.direction
        emoji = "⚠️"
        text  = (
            f"{emoji} <b>TREND ĐANG YẾU — {display_name}</b>\n"
            f"{'━' * 28}\n"
            f"📊 Hướng hiện tại : {d}\n"
            f"💰 Avg Entry      : {_price(signal.avg_entry)}\n"
            f"{'━' * 28}\n"
            f"🔍 <b>Dấu hiệu exhaustion:</b>\n"
            f"  • Candle body đang nhỏ dần\n"
            f"  • ATR spike rồi co lại\n"
            f"  • Wick rejection xuất hiện\n"
            f"{'━' * 28}\n"
            f"💡 <b>Gợi ý:</b> Cân nhắc chốt 50% vị thế\n"
            f"   Trailing SL đang bảo vệ phần còn lại"
        )
        await self._send(self.channel, text)

    async def send_trail_activated(self, display_name: str,
                                    direction: str,
                                    trail_sl: float) -> None:
        text = (
            f"🎯 <b>TRAILING SL ACTIVE — {display_name}</b>\n"
            f"Hướng    : {direction}\n"
            f"Trail SL : {_price(trail_sl)}\n"
            f"✅ Lãi đang được bảo vệ tự động"
        )
        await self._send(self.channel, text)

    # ── Daily summary review flow ─────────────────────────────────────────

    async def send_summary_for_review(self, summary_text: str) -> Optional[int]:
        """
        Gửi draft summary vào private chat của mày để review.
        Returns message_id để track.
        """
        text = (
            f"📋 <b>DRAFT DAILY SUMMARY</b>\n"
            f"{'━' * 28}\n"
            f"{summary_text}\n"
            f"{'━' * 28}\n"
            f"Reply <b>send</b> để forward lên channel\n"
            f"Reply <b>skip</b> để bỏ qua\n"
            f"<i>(Tự động bỏ qua sau 30 phút nếu không reply)</i>"
        )
        return await self._send(self.me, text)

    async def forward_summary_to_channel(self, summary_text: str) -> None:
        """Forward approved summary lên channel."""
        await self._send(self.channel, summary_text)

    async def check_reply(self, after_message_id: int) -> Optional[str]:
        """
        Check xem mày đã reply 'send' hay 'skip' chưa.
        Returns "send" | "skip" | None
        """
        url     = self.BASE.format(token=self.token, method="getUpdates")
        params  = {"offset": -10, "limit": 20}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params)
                data = resp.json()
            for update in reversed(data.get("result", [])):
                msg = update.get("message", {})
                if str(msg.get("chat", {}).get("id", "")) == str(self.me):
                    text = msg.get("text", "").strip().lower()
                    if text in ("send", "skip"):
                        return text
        except Exception as e:
            log.error("telegram.check_reply_error", error=str(e))
        return None

    # ── System alerts → private chat ─────────────────────────────────────

    async def system_alert(self, message: str) -> None:
        await self._send(self.me, f"⚠️ <b>SYSTEM</b>\n{message}")

    async def bot_started(self, symbols: list[str]) -> None:
        text = (
            f"🚀 <b>Signal Bot STARTED</b>\n"
            f"Assets: {', '.join(symbols)}\n"
            f"Interval: 15m\n"
            f"Mode: Auto-execute BingX Futures"
        )
        await self._send(self.me, text)
        await self._send(self.channel, text)


# Singleton
notifier = TelegramNotifier()
