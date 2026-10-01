"""Telegram alerts with per-key cooldown; falls back to the log when Telegram isn't configured.
The cooldown is only armed after a successful delivery, so a Telegram outage doesn't swallow alerts."""

from __future__ import annotations

import html
import logging
import time

import httpx

from .config import Settings
from .journal import Journal

log = logging.getLogger("shortbot.notify")


class Notifier:
    def __init__(self, settings: Settings, journal: Journal):
        self.s = settings
        self.j = journal

    def _cooling(self, key: str, cooldown_s: float) -> bool:
        last = self.j.get(f"alert_ts:{key}")
        return bool(last) and time.time() - float(last) < cooldown_s

    async def send(self, kind: str, message: str, symbol: str | None = None,
                   cooldown_s: float = 3600, urgent: bool = False) -> bool:
        """Send once per (kind, symbol) per cooldown. Returns True if delivered (or logged, without Telegram)."""
        key = f"{kind}:{symbol}"
        if cooldown_s and self._cooling(key, cooldown_s):
            return False
        self.j.log_alert(kind, message, symbol)
        log.warning("ALERT %s %s: %s", kind, symbol or "", message)
        if self.s.telegram_token and self.s.telegram_chat_id and not await self._telegram(kind, message, symbol, urgent):
            return False
        self.j.set(f"alert_ts:{key}", str(time.time()))
        return True

    async def _telegram(self, kind: str, message: str, symbol: str | None, urgent: bool) -> bool:
        text = f"{'🚨' if urgent else '🔔'} <b>[{self.s.mode}] {html.escape(kind)}</b>"
        text += f" {html.escape(symbol)}" if symbol else ""
        text += f"\n{html.escape(message)}"
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(
                    f"https://api.telegram.org/bot{self.s.telegram_token}/sendMessage",
                    json={"chat_id": self.s.telegram_chat_id, "text": text[:4000], "parse_mode": "HTML",
                          "link_preview_options": {"is_disabled": True}})
                r.raise_for_status()
            return True
        except Exception as e:  # alerts must never crash the guardian
            log.error("telegram send failed: %s", e)
            return False

    async def as_trader_alert(self, kind: str, message: str, symbol: str | None, urgent: bool) -> None:
        """Adapter for Trader(alert=...)."""
        await self.send(kind, message, symbol, cooldown_s=0 if urgent else 600, urgent=urgent)
