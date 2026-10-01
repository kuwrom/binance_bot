"""Session clock + macro calendar (docs/SESSIONS.md). Time predicts volatility, not direction: this signal
scores nothing. It vetoes new entries inside scheduled macro windows and exposes the clock as features
(research: does any of it matter for OUR fills?). `execution_guard` blocks market orders only (trading_open.py)."""

from __future__ import annotations

from ..market import MarketContext
from ..market.calendar import blocking
from ..sessions import clock
from .base import SignalOut, signal


@signal("session", "event")
async def session(ctx: MarketContext) -> SignalOut:
    """UTC hour/session/weekday, minutes to funding (this symbol's interval), day/week close, US open, macro
    proximity. Vetoes: CPI/NFP/PCE -15/+30 min, FOMC -30/+60, maintenance ±30."""
    interval = (await ctx.funding()).get("interval_h") or 8
    c = clock(interval_h=int(interval))
    macro = await ctx.macro()
    upcoming = [e for e in macro if e["minutes_to"] >= -60]
    nxt = upcoming[0] if upcoming else None
    out = SignalOut(features={
        **{f"clock_{k}" if k in ("utc", "weekday", "weekend") else k: v for k, v in c.items()},
        "macro_next": f"{nxt['name']} in {nxt['minutes_to']:.0f} min" if nxt else None,
        "macro_next_min": nxt["minutes_to"] if nxt else None, "macro_next_kind": nxt["kind"] if nxt else None})
    out.vetoes += blocking(macro)
    return out
