"""Attention signals (docs/SOCIAL_AND_ROBUST_PRICES.md §3). Evidence: attention means CONTINUATION for days
(Liu & Tsyvinski) while pumps reliably reverse once they fade (Dhawan & Putniņš), so for a short book:
never short still-rising attention; the fade AFTER an attention peak is the setup (hype_fade)."""

from __future__ import annotations


from ..decay import decayed_points, heat
from ..market import MarketContext
from .base import SignalOut, signal

HOUR_MS = 3_600_000


def hourly_increments(series: list[tuple[int, float]]) -> list[float]:
    """Cumulative Square post counts -> new posts per hour (bucketed; gaps carry zero)."""
    if len(series) < 2:
        return []
    start, end = series[0][0], series[-1][0]
    buckets = [0.0] * (int((end - start) // HOUR_MS) + 1)
    for (t0, v0), (t1, v1) in zip(series, series[1:]):
        buckets[int((t1 - start) // HOUR_MS)] += max(v1 - v0, 0)
    return buckets


@signal("social", "social")
async def social(ctx: MarketContext) -> SignalOut:
    """CoinGecko trending, Binance hype (+ sentiment, wash-trading tag), Binance Square mention velocity/heat
    from the guardian's hourly snapshots. hype_fade = mentions spiked (z ≥ 3 in 48h) and are now cooling while
    price is still ≥ 20% above its 3-day low and funding is positive."""
    s = await ctx.social()
    posts = s.get("square_posts_series") or []
    inc = hourly_increments(posts)
    h = heat(inc, fast_hl=2, slow_hl=12) if len(inc) >= 6 else {"z": None, "hot": False, "cooling": False}
    peak_z = max((heat(inc[:i], 2, 12)["z"] or 0 for i in range(max(6, len(inc) - 48), len(inc) + 1)), default=0) \
        if len(inc) >= 6 else 0
    out = SignalOut(features={
        "cg_trending_rank": s.get("cg_rank"), "binance_hype": s.get("hype"), "hype_sentiment": s.get("sentiment"),
        "hype_wash_tag": s.get("wash_trading_tag"), "square_posts_7d": s.get("square_posts_7d"),
        "mention_heat_z": h["z"], "mention_hot": h["hot"], "mention_cooling": h["cooling"],
        "mention_peak_z_48h": peak_z, "social_history_h": len(inc)})
    c4h = await ctx.candles("4h", 18)
    last = await ctx.last()
    low3d = min((c[3] for c in c4h), default=None)
    extended = bool(low3d) and last >= low3d * 1.2
    fr = (await ctx.funding()).get("rate")
    rising = False
    c1h = await ctx.candles("1h", 60)
    if len(c1h) >= 2:
        rising = c1h[-1][4] > c1h[-2][4]
    if h["hot"] and rising:
        out.vetoes.append(f"mentions still accelerating (z={h['z']}) with price rising: attention continues, wait")
    elif peak_z >= 3 and h["cooling"] and extended and (fr or 0) > 0:
        peak_age = next((len(inc) - i for i in range(len(inc), max(5, len(inc) - 48), -1)
                         if (heat(inc[:i], 2, 12)["z"] or 0) >= 3), 0)
        pts = decayed_points(8, "social", peak_age)
        out.features["hype_fade"] = True
        if pts:
            out.points.append((pts, f"hype fading: mentions peaked (z {peak_z:.1f}) {peak_age}h ago, price still extended"))
    if s.get("cg_rank") and s["cg_rank"] <= 5 and rising:
        out.features["cg_hot"] = True
    return out
