"""Manipulation-resistant signals (docs/SOCIAL_AND_ROBUST_PRICES.md §1): trust time/volume and the mark price,
not the last print. Features for research + a painted-close warning; the sweep signal uses robust_math too."""

from __future__ import annotations

from .. import indicators as ind
from .. import robust_math as rm
from ..market import MarketContext
from .base import SignalOut, signal


@signal("close_quality", "tape")
async def close_quality(ctx: MarketContext) -> SignalOut:
    """Is the latest 15m close real? close vs its bar VWAP (in ATR), tail-volume share (painted close),
    last-vs-mark gap z, and this coin's close-engineering score (push-then-reverse habit)."""
    one = await ctx.flow_candles("1m", 60)
    five = await ctx.flow_candles("5m", 288)
    mark5 = await ctx.mark_candles("5m", 288)
    c1h = await ctx.candles("1h", 60)
    atr = ind.atr(c1h) or 0.0
    last15 = one[-15:] if len(one) >= 15 else one
    p = rm.painted_close(last15, atr) if last15 else {"painted": False}
    gz = rm.mark_gap_z([b[4] for b in five], [m[4] for m in mark5]) if five and mark5 else None
    ces = rm.close_engineering_score(five) if five else None
    out = SignalOut(features={"painted_close": p.get("painted"), "close_dev_atr": p.get("close_dev"),
                              "tail_vol_share": p.get("tail_vol_share"), "mark_gap_z": gz,
                              "close_engineering": ces})
    if p.get("painted") and (p.get("close_dev") or 0) < 0:
        out.vetoes.append("latest close painted DOWN on thin tail volume: the bearish print isn't backed by volume")
    return out
