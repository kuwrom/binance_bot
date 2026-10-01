"""Controller label (docs/COIN_PROFILE.md): who probably controls this coin's supply and liquidity, inferred
from free footprints. We can't know what the owners think, but we can see their constraints (float, FDV, unlocks,
listing age) and footprints (OI vs market cap, funding, volume regularity). Labels are PRIORS: they shift odds and
size, they never trigger a trade. Thresholds are assumptions, fixed before testing; every candidate logs them."""

from __future__ import annotations

import statistics

from ..market import MarketContext
from ..market.supply import supply_facts
from .base import SignalOut, signal

LABELS = ("thin_float", "insider_overhang", "mm_range", "organic", "unlabelled")


def controller_labels(float_pct: float | None, fdv_mc: float | None, age_d: float | None, oi_mc: float | None,
                      funding_7d: float | None, unlock_soon: bool, vol_cv: float | None,
                      daily_sigma_pct: float | None) -> list[str]:
    out = []
    if (float_pct is not None and float_pct < 20) or (oi_mc or 0) > 0.3 or (funding_7d is not None and funding_7d < 0):
        out.append("thin_float")
    if (float_pct is not None and float_pct < 35 and (fdv_mc or 0) > 2.5
            and (unlock_soon or (age_d is not None and age_d < 180))):
        out.append("insider_overhang")
    if vol_cv is not None and vol_cv < 0.3 and daily_sigma_pct is not None and daily_sigma_pct < 2.5:
        out.append("mm_range")
    if not out and (float_pct or 0) > 60 and (age_d or 0) > 365:
        out.append("organic")
    return out or ["unlabelled"]


@signal("controller", "guard")
async def controller(ctx: MarketContext) -> SignalOut:
    """Float %, FDV/MC, OI/MC, 7d funding, volume regularity -> thin_float | insider_overhang | mm_range |
    organic | unlabelled (first is primary). Monitoring tag is flagged for the delisting playbook."""
    f = ctx.features or {}
    sf = supply_facts(await ctx.supply())
    oi = await ctx.oi_history("1h", 2)
    oi_mc = round(oi[-1]["oi_value"] / sf["mcap_usd"], 3) if oi and sf["mcap_usd"] else None
    fh = await ctx.funding_history(21)
    f7 = statistics.fmean(x["fundingRate"] for x in fh) if fh else None
    d1 = (await ctx.candles("1d", 200))[-30:]
    qv = [c[5] * c[4] for c in d1]
    vol_cv = statistics.pstdev(qv) / statistics.fmean(qv) if len(qv) >= 10 and statistics.fmean(qv) else None
    rets = [(b[4] / a[4] - 1) * 100 for a, b in zip(d1, d1[1:]) if a[4]]
    sigma = statistics.pstdev(rets) if len(rets) >= 10 else None
    unlock_soon = "unlock_ahead" in (f.get("lifecycle_tags") or [])
    labels = controller_labels(sf["float_pct"], sf["fdv_mc"], f.get("listing_age_days"), oi_mc, f7, unlock_soon,
                               vol_cv, sigma)
    out = SignalOut(features={"controller_label": labels[0], "controller_labels": labels, **sf, "oi_mc": oi_mc,
                              "funding_7d_avg": f7, "daily_vol_cv": round(vol_cv, 2) if vol_cv else None,
                              "daily_sigma_pct": round(sigma, 2) if sigma else None,
                              "monitoring_tag": "Monitoring" in (await ctx.tags())})
    if "insider_overhang" in labels:
        out.points.append((5, f"insider supply overhang: float {sf['float_pct']}%, FDV/MC {sf['fdv_mc']}"))
    return out
