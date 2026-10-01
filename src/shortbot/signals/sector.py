"""Market-map signal (docs/MARKET_MAP.md): judge a coin against the whole tape, not alone. Vetoes shorting
into a market squeeze, a BTC/ETH up-impulse, a sector-wide rally or a rotation INTO the coin's sector.
Context: the coin's move net of BTC beta and of its sector. Laggards in weak sectors are the preferred shorts
(momentum evidence); extreme outliers are the pump-fade setup's job, not this signal's."""

from __future__ import annotations

from ..market import MarketContext
from .base import SignalOut, signal


@signal("sector", "guard")
async def sector(ctx: MarketContext) -> SignalOut:
    """Sector, BTC beta, residual moves (vs BTC and vs sector), sector rank and rotation, market regime."""
    m = await ctx.market_map()
    if not m:
        return SignalOut(features={"market_map": None})
    coin = m["coins"].get(ctx.symbol) or {}
    sec = coin.get("sector")
    sv = m["vetoes"]["sectors"].get(sec, {}) if sec else {}
    base = ctx.symbol.split("/")[0]
    out = SignalOut(features={
        "market_map": m["ts"], "map_regime": m["header"]["regime"], "breadth_pct": m["header"]["breadth_pct"],
        "sector": sec, "sector_rank4h": sv.get("rank4h"), "sector_resid24h": sv.get("resid24h"),
        "sector_leader": bool(sec) and sv.get("leader") == base, "sector_rotation_out": sv.get("rotation_out"),
        "beta_btc": coin.get("beta"), "resid4h": coin.get("resid4h"), "resid24h": coin.get("resid24h"),
        "resid_sector24h": coin.get("resid_sector24h"), "resid_sector_z": coin.get("resid_sector_z")})
    out.vetoes += m["vetoes"]["pause"] + sv.get("vetoes", [])
    z, s24 = coin.get("resid_sector_z"), sv.get("resid24h")
    if z is not None and s24 is not None and z < -1 and s24 < 0:
        out.points.append((4, f"laggard (z {z}) in a weak sector ({sec} {s24:+.1f}% vs BTC 24h)"))
    if sv.get("rotation_out"):
        out.points.append((3, f"money rotating OUT of {sec}"))
    return out
