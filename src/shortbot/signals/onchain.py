"""On-chain shadow features (market/onchain.py; owner decision 2026-10-06): the Binance Skills Hub's smart-money
inflow and signals, token-rank presence, holder structure and DEX buy/sell flow of the perp's token, as the guardian
last snapshotted them. Research features only: no points, no vetoes, never a data_missing input (the critical inputs
are core.CRITICAL_INPUTS); a research review decides whether any of them earns a place in the score."""

from __future__ import annotations

from .. import clock
from ..market import MarketContext
from .base import SignalOut, signal


@signal("onchain", "social")
async def onchain(ctx: MarketContext) -> SignalOut:
    """oc_* features of this coin's on-chain token (oc_status: matched | no_match | none = not snapshotted) and
    oc_age_min, the snapshot's age. Shadow only: no points, no vetoes."""
    snap = await ctx.onchain()
    if not snap:
        return SignalOut(features={"oc_status": "none"})
    f = {k: v for k, v in snap.items() if k != "oc_ts"}
    ts = snap.get("oc_ts")
    f["oc_age_min"] = round((clock.now_ms() - ts) / 60_000, 1) if isinstance(ts, (int, float)) else None
    return SignalOut(features=f)
