"""Guardian coin-memory lane (`coins`; coin_memory.py, docs/COIN_MEMORY.md): every 15 min one pass over the focus coins
(coin_pass.refresh: public data at RESEARCH weight, states saved in one journal unit). No LLM runs here: the job
measures and alerts, and a session reads the alert, rewrites the coin's read (update_coin_read) and decides.
Alerts never exit a trade:
- coin_event (routine, once an hour per coin): new events on a focus coin;
- break_watch (urgent): an open short's break score reached 2 of #81's 4 machine checks (beyond the noise band,
  accepted, participation, source). One check alone is noise and isn't alerted."""

from __future__ import annotations

import logging
from typing import Any

from . import coin_pass
from .coin_stats import BREAK_REAL

log = logging.getLogger("shortbot.guardian")
COIN_EVENT_COOLDOWN_S = 3600
BREAK_WATCH_COOLDOWN_S = 1800
QUIET_EVENTS = ("btc_impulse",)   # market-wide: listed on every coin; market_map's market_pause alerts it once


class CoinJobs:
    """Guardian mixin: needs ex, j, n, entries."""

    app: Any
    ex: Any
    j: Any
    n: Any
    entries: Any

    async def coin_memory(self) -> None:
        r = await coin_pass.refresh(self.ex, self.j, self.entries, book_only=not self.app.settings.guardian_market)
        for sym, st in r["coins"].items():
            new = [e for e in st.get("events") or () if e.get("new") and e["name"] not in QUIET_EVENTS]
            if new:
                await self.n.send("coin_event", "; ".join(f"{e['name']}: {e['detail']}" for e in new)
                                  + ". Re-read the coin and rewrite its read (update_coin_read) if this changes it.",
                                  sym, cooldown_s=COIN_EVENT_COOLDOWN_S)
            b = st.get("break")
            if b and b["score"] >= BREAK_REAL:
                await self.n.send("break_watch", break_text(b), sym, urgent=True, cooldown_s=BREAK_WATCH_COOLDOWN_S)
        log.info("coin memory: %d coins, %d failed, %d reads scored", len(r["coins"]), len(r["errors"]), r["scored"])


def break_text(b: dict[str, Any]) -> str:
    """The break_watch message: which of #81's checks are true, with their numbers. Our own text only."""
    c, why = b["checks"], []
    if c["beyond_noise"]:
        why.append(f"move {b['move_pct']}% > noise band {b['band_pct']}%")
    if c["accepted"]:
        why.append(f"2 closes above {b['level']}")
    if c["participation"]:
        why.append(f"OI {b['oi_1h_pct']:+}% in 1h with taker buyers (TI {b['ti_1h']})")
    if c["source"]:
        why.append(f"BTC {b['btc_15m_pct']:+}% in 15m")
    return (f"trade #{b.get('trade_id')}: break score {b['score']}/4, a real break by #81 ({'; '.join(why)}). "
            "Nothing was closed: run manage-position (judge the 5th check, new information, yourself).")
