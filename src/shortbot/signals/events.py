"""Event-clock signals (not age-decayed: they are anchored to the event date). Unlock window per Keyrock:
pressure builds from ~30 days before a team/investor/cliff unlock; exit around the unlock."""

from __future__ import annotations

from ..market import MarketContext
from ..market.events import BEARISH_RECIPIENTS
from .base import SignalOut, signal

MIN_PCT = 2.0
WINDOW_DAYS = (3.0, 30.0)


@signal("unlock", "event")
async def unlock(ctx: MarketContext) -> SignalOut:
    """Next recorded unlock: days to it, % of supply, recipient. Scores bearish recipients (team/investor/cliff)
    of ≥ 2% supply inside the 3-30 day pre-unlock window; flags 'exit window' within 2 days of the unlock."""
    ev = [e for e in await ctx.unlocks() if e["days_to_unlock"] >= -2]
    if not ev:
        return SignalOut(features={"unlock_days": None})
    nxt = ev[0]
    d, pct, who = nxt["days_to_unlock"], nxt.get("pct_supply"), nxt["recipient"]
    out = SignalOut(features={"unlock_days": d, "unlock_pct_supply": pct, "unlock_recipient": who,
                              "unlock_source": nxt["source"], "unlock_exit_window": -2 <= d <= 2})
    if who in BEARISH_RECIPIENTS and (pct or 0) >= MIN_PCT and WINDOW_DAYS[0] <= d <= WINDOW_DAYS[1]:
        pts = 15 if who == "team" or who == "cliff" else 8
        if (pct or 0) >= 5:
            pts += 5
        out.points.append((pts, f"{who} unlock of {pct:.1f}% supply in {d:.0f}d (pre-unlock pressure window)"))
    return out
