"""Lifecycle stage (docs/COIN_PROFILE.md, core philosophy): Binance lists coins every week and most decay, so
WHERE a coin is in its life says which scripts are likely: fresh listings get distributed into, pumps retrace,
bleeding coins bounce weakly, delisting/Monitoring coins dump. The stage is a feature on every logged
candidate, so the research report measures which stage x tape combinations actually fall. Thresholds are
starting assumptions."""

from __future__ import annotations

from ..market import MarketContext
from ..market.events import BEARISH_RECIPIENTS
from .base import SignalOut, signal

STAGES = ("delisting_risk", "new_listing", "unlock_ahead", "pumping", "post_pump", "bleeding", "young", "mature")


def stage_of(age_d: float | None, delisting: bool, monitoring: bool, unlock_soon: bool, d1: list[list[float]]
             ) -> tuple[str, list[str], dict[str, float | None]]:
    """(primary stage, all matching stages, numbers). d1 = daily candles since listing (up to ~200)."""
    closes = [c[4] for c in d1]
    last = closes[-1] if closes else None
    m30 = d1[-30:]
    hi30, lo30 = (max(c[2] for c in m30), min(c[3] for c in m30)) if m30 else (None, None)
    ath = max((c[2] for c in d1), default=None)   # high since listing, or over the last 200 days
    ret = lambda n: (last / closes[-n - 1] - 1) * 100 if last and len(closes) > n and closes[-n - 1] else None  # noqa: E731
    nums = {"lc_dd_from_high_pct": round((1 - last / ath) * 100, 1) if last and ath else None,
            "lc_ret_1d_pct": _r(ret(1)), "lc_ret_7d_pct": _r(ret(7)), "lc_ret_30d_pct": _r(ret(30)),
            "lc_pump_multiple_30d": round(hi30 / lo30, 2) if hi30 and lo30 else None,
            "lc_off_30d_high_pct": round((1 - last / hi30) * 100, 1) if last and hi30 else None}
    tags = []
    if delisting or monitoring:
        tags.append("delisting_risk")
    if age_d is not None and age_d < 30:
        tags.append("new_listing")
    if unlock_soon:
        tags.append("unlock_ahead")
    pm, off = nums["lc_pump_multiple_30d"] or 0, nums["lc_off_30d_high_pct"]
    # a +25% day counts too: a one-day pump in an old coin (HBAR +35% on 2026-09-28 read as "mature")
    if (nums["lc_ret_7d_pct"] or 0) > 40 or (nums["lc_ret_1d_pct"] or 0) > 25 or (pm >= 2 and off is not None and off < 15):
        tags.append("pumping")
    elif pm >= 1.8 and off is not None and 15 <= off <= 60:
        tags.append("post_pump")
    if (nums["lc_dd_from_high_pct"] or 0) > 60 and (nums["lc_ret_30d_pct"] or 0) < 0:
        tags.append("bleeding")
    if age_d is not None and age_d < 180:
        tags.append("young")
    tags.append("mature")
    primary = next(s for s in STAGES if s in tags)
    return primary, [t for t in tags if t != "mature" or primary == "mature"], nums


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


@signal("lifecycle", "event")
async def lifecycle(ctx: MarketContext) -> SignalOut:
    """Stage in the coin's life: delisting_risk > new_listing > unlock_ahead > pumping > post_pump > bleeding >
    young > mature (first match is primary; all matches are kept)."""
    info = await ctx.market_info()
    d1 = await ctx.candles("1d", 200)
    tags = await ctx.tags()
    unlocks = [e for e in await ctx.unlocks() if 3 <= e["days_to_unlock"] <= 30
               and e["recipient"] in BEARISH_RECIPIENTS and (e.get("pct_supply") or 0) >= 2]
    primary, all_tags, nums = stage_of(info.get("listing_age_days"), bool(info.get("delisting_scheduled")),
                                       "Monitoring" in tags, bool(unlocks), d1)
    out = SignalOut(features={"lifecycle_stage": primary, "lifecycle_tags": all_tags,
                              "listing_age_days": info.get("listing_age_days"), **nums})
    if primary == "post_pump":
        out.points.append((4, f"post-pump: {nums['lc_pump_multiple_30d']}x in 30d, now "
                              f"{nums['lc_off_30d_high_pct']}% off the high"))
    elif primary == "bleeding":
        out.points.append((2, f"bleeding: {nums['lc_dd_from_high_pct']}% below its high since listing"))
    return out
