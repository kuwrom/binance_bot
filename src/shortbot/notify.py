"""Telegram alerts with per-key cooldown; falls back to the log when Telegram isn't configured.
Every alert is an alerts row first (posted: queued in order while another process holds the journal's write lock, #79).
The cooldown is per (kind, symbol) and its stamp is marked (Journal.mark): this process sees its own sends at once, also
while their writes are queued behind a held lock (#81), and other processes' sends in the journal. It is armed on the
attempt, so a dead Telegram (a revoked token) never re-alerts every pass; a failed send shortens it to
TELEGRAM_RETRY_S, so an outage doesn't swallow an alert for its whole cooldown either. A send is time-boxed, and no
row, log line or message carries a secret (redact.py).
`forward` is the one Telegram-only path: for a row some other process journalled (setup_ladder_stop, #69)."""

from __future__ import annotations

import asyncio
import html
import logging
import time

import httpx

from .config import Settings
from .journal import Journal
from .redact import redact

log = logging.getLogger("shortbot.notify")
logging.getLogger("httpx").setLevel(logging.WARNING)   # it logs each request URL at INFO: ours carry the bot token
TELEGRAM_TIMEOUT_S = 5.0   # one send never holds its lane longer than this
TELEGRAM_RETRY_S = 900     # a failed send is retried after this, not after its full cooldown
TRADER_REPEAT_S = 600      # the guardian Trader's non-urgent repeats (same kind and symbol) go to the journal only


class Notifier:
    def __init__(self, settings: Settings, journal: Journal):
        self.s = settings
        self.j = journal

    def _cooling(self, key: str, cooldown_s: float) -> bool:
        last = self.j.get(self.j.mode_key(f"alert_ts:{key}"))
        return time.time() - float(last) < cooldown_s if last else False

    async def send(self, kind: str, message: str, symbol: str | None = None,
                   cooldown_s: float = 3600, urgent: bool = False) -> bool:
        """Send once per (kind, symbol) per cooldown. Returns True if delivered (or logged, without Telegram)."""
        key = f"{kind}:{symbol}"
        if cooldown_s and self._cooling(key, cooldown_s):
            return False
        message = redact(message)
        self.j.log_alert(kind, message, symbol)   # posted, like its cooldown stamp: a locked journal never holds it
        now = time.time()
        self.j.mark(self.j.mode_key(f"alert_ts:{key}"), str(now))
        log.warning("ALERT %s %s: %s", kind, symbol or "", message)
        if not (self.s.telegram_token and self.s.telegram_chat_id):
            return True
        if await self._telegram(kind, message, symbol, urgent):
            return True
        if cooldown_s > TELEGRAM_RETRY_S:
            self.j.mark(self.j.mode_key(f"alert_ts:{key}"), str(now - (cooldown_s - TELEGRAM_RETRY_S)))
        return False

    async def forward(self, kind: str, message: str, symbol: str | None = None, urgent: bool = False) -> bool:
        """Telegram only, for an alert row another process already journalled: no second row and no cooldown (the
        caller sends each row once). Without Telegram it is logged. True if delivered (or logged)."""
        message = redact(message)
        log.warning("ALERT %s %s: %s", kind, symbol or "", message)
        if not (self.s.telegram_token and self.s.telegram_chat_id):
            return True
        return await self._telegram(kind, message, symbol, urgent)

    async def _telegram(self, kind: str, message: str, symbol: str | None, urgent: bool) -> bool:
        text = f"{'🚨' if urgent else '🔔'} <b>[{self.s.mode}] {html.escape(kind)}</b>"
        text += f" {html.escape(symbol)}" if symbol else ""
        text += f"\n{html.escape(message)}"
        try:
            await asyncio.wait_for(self._post(text), TELEGRAM_TIMEOUT_S)
            return True
        except Exception as e:  # alerts must never crash the guardian (incl. TimeoutError)
            why = f"timed out after {TELEGRAM_TIMEOUT_S:g}s" if isinstance(e, TimeoutError) else \
                f"{type(e).__name__}: {e}"
            log.error("telegram send failed: %s", redact(why.replace(self.s.telegram_token, "<redacted>")))
            return False

    async def _post(self, text: str) -> None:
        async with httpx.AsyncClient(timeout=TELEGRAM_TIMEOUT_S) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{self.s.telegram_token}/sendMessage",
                json={"chat_id": self.s.telegram_chat_id, "text": text[:4000], "parse_mode": "HTML",
                      "link_preview_options": {"is_disabled": True}})
            r.raise_for_status()

    async def as_trader_alert(self, kind: str, message: str, symbol: str | None, urgent: bool) -> None:
        """Adapter for the guardian's Trader(alert=...): every alert is a row (two action_fill rows for a split entry's
        legs are both the audit trail, #66); a cooldown only spares Telegram a non-urgent repeat of the same kind and
        symbol within TRADER_REPEAT_S."""
        if not urgent and self._cooling(f"{kind}:{symbol}", TRADER_REPEAT_S):
            self.j.log_alert(kind, message, symbol)   # redacts
            return
        await self.send(kind, message, symbol, cooldown_s=0 if urgent else TRADER_REPEAT_S, urgent=urgent)

    async def as_session_alert(self, kind: str, message: str, symbol: str | None, urgent: bool) -> None:
        """Adapter for the MCP/CLI Trader: every alert is a row (action_propose, action_fill... are the audit trail,
        never dropped by a cooldown); only urgent ones (NAKED_POSITION, stop_failed...) also go to Telegram."""
        if urgent:
            await self.send(kind, message, symbol, cooldown_s=0, urgent=True)
        else:
            self.j.log_alert(kind, message, symbol)
