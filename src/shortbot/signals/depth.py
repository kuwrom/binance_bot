"""Order-book depth (task #97, research-only). Book depth within +-1% of mid, per side, in USDT.

Measured 2026-10-01: the alts we trade show $0.08-0.7M per side (STX 0.14, MON 0.35, CRV 0.68, SOON 0.09) while
ENA/NEAR show $1.3-2.4M, so the proposed ~$1M floor would remove nearly every candidate. It is therefore logged
first (no points, no veto): once the 72h labels arrive, research_report tests whether thin asks predict squeezes.
"""

from __future__ import annotations

from ..market import MarketContext
from .base import SignalOut, signal

BAND = 0.01


def depth_within(ob: dict, band: float = BAND) -> dict[str, float | None]:
    bids, asks = ob.get("bids") or [], ob.get("asks") or []
    if not bids or not asks:
        return {"depth_bid_1pct_usd": None, "depth_ask_1pct_usd": None, "spread_bp": None}
    mid = (bids[0][0] + asks[0][0]) / 2
    bid = sum(p * q for p, q, *_ in bids if p >= mid * (1 - band))
    ask = sum(p * q for p, q, *_ in asks if p <= mid * (1 + band))
    return {"depth_bid_1pct_usd": round(bid), "depth_ask_1pct_usd": round(ask),
            "spread_bp": round((asks[0][0] - bids[0][0]) / mid * 1e4, 2)}


@signal("depth", "tape")
async def depth(ctx: MarketContext) -> SignalOut:
    """Book depth within +-1% of mid per side (USDT) and the spread: logged for research, no points or vetoes yet."""
    f = depth_within(await ctx.order_book())
    vol = float((await ctx.ticker()).get("quoteVolume") or 0)
    ask = f["depth_ask_1pct_usd"]
    f["ask_depth_per_vol_bp"] = round(ask / vol * 1e4, 2) if ask is not None and vol else None
    return SignalOut(features=f)
