"""Core short-setup signals (docs/PLAYBOOK.md), ported 1:1 from the original scoring: same thresholds,
same points, same vetoes. Feature names are stable because research data is keyed by them."""

from __future__ import annotations

from .. import indicators as ind
from ..market import MarketContext
from ..risk import liquidity_tier
from .base import SignalOut, signal

MAJORS = {"BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "LINK", "AVAX", "TON", "LTC", "BCH"}
RISKY_TAGS = {"Alpha"}
CRITICAL_INPUTS = {"funding", "open_interest", "market_info", "candles_1h"}


async def change_24h(ctx: MarketContext) -> float:
    return float((await ctx.ticker()).get("percentage") or 0)


@signal("market", "core")
async def market(ctx: MarketContext) -> SignalOut:
    """24h move, liquidity, listing age / delisting, tags, and whether the account can trade it."""
    t = await ctx.ticker()
    chg, qv = float(t.get("percentage") or 0), float(t.get("quoteVolume") or 0)
    info = await ctx.market_info()
    on_acct = await ctx.account_tradable()
    out = SignalOut(features={
        "change_24h": chg, "quote_volume": qv, "last": await ctx.last(),
        "listing_age_days": info.get("listing_age_days"), "delisting_scheduled": bool(info.get("delisting_scheduled")),
        "tags": info.get("tags", []), "account_tradable": on_acct if isinstance(on_acct, bool) else None})
    if chg >= 0:
        out.points.append((round(min(chg, 100) / 100 * 25), f"24h {chg:+.1f}%"))
    else:  # 24h loser: breakdown continuation (experimental; research decides its weight)
        out.points.append((round(min(-chg, 40) / 40 * 15), f"24h {chg:+.1f}% (breakdown momentum)"))
    age = info.get("listing_age_days")
    if age is not None and age < 30:
        out.points.append((10, f"new listing ({age:.0f}d)"))
    if info.get("delisting_scheduled"):
        out.points.append((15, "delisting scheduled"))
    if on_acct is False:
        out.vetoes.append("not listed on the connected account (demo has fewer perps than live)")
    if ctx.base in MAJORS:
        out.vetoes.append("major coin: momentum dominates, don't fade")
    if qv < ctx.ex.limits.min_quote_volume_24h:
        out.vetoes.append(f"24h volume ${qv / 1e6:.1f}M below floor")
    if RISKY_TAGS & set(info.get("tags") or []):
        out.vetoes.append("Binance Alpha / low-float token: squeeze-prone")
    return out


@signal("funding", "froth")
async def funding(ctx: MarketContext) -> SignalOut:
    """Funding normalised to 8h: elevated/extreme = longs paying (froth). Negative / 1h interval = shorts pay: a FLAG
    with half size + a funding budget, not a veto (round 3, learning #22: blocked trades did better, funding cost
    was small; the human chose 'soften to a flag' 2026-09-29)."""
    f = await ctx.funding()
    rate, iv = f.get("rate"), int(f.get("interval_h") or 8)
    f8 = rate * 8 / max(iv, 1) if rate is not None else None
    shorts_pay = (rate is not None and rate < 0) or iv == 1
    out = SignalOut(features={"funding_rate": rate, "funding_interval_h": iv, "funding_8h": f8,
                              "funding_flag": ("1h_interval" if iv == 1 else "negative") if shorts_pay else None,
                              "funding_size_mult": 0.5 if shorts_pay else 1.0})
    if f8 is not None and f8 >= 0.001:
        out.points.append((15, f"funding extreme {f8 * 100:.3f}%/8h"))
    elif f8 is not None and f8 >= 0.0005:
        out.points.append((10, f"funding elevated {f8 * 100:.3f}%/8h"))
    return out


@signal("open_interest", "froth")
async def open_interest(ctx: MarketContext) -> SignalOut:
    """OI size (liquidity floor) and 24h OI change; OI up with price = late longs."""
    oi = await ctx.oi_history("1h", 25)
    val = oi[-1]["oi_value"] if oi else None
    chg = ind.pct_change(oi[0]["oi"], oi[-1]["oi"]) if len(oi) > 1 else None
    qv = float((await ctx.ticker()).get("quoteVolume") or 0)
    tier, mult = liquidity_tier(qv, val)
    out = SignalOut(features={"oi_value": val, "oi_change_24h": chg, "liq_tier": tier, "liq_size_mult": mult})
    if chg is not None and chg >= 30 and await change_24h(ctx) > 0:
        out.points.append((10, f"OI +{chg:.0f}% with price up (late longs)"))
    if tier is None:
        out.vetoes.append(f"too thin: vol ${qv / 1e6:.1f}M / OI ${(val or 0) / 1e6:.1f}M below tier B "
                          f"(vol >= $5M and OI >= $10M)")
    return out


@signal("stretch", "froth")
async def stretch(ctx: MarketContext) -> SignalOut:
    """Overextension on 1h: RSI, distance above 24h VWAP (z), relative volume."""
    c1h = await ctx.candles("1h", 60)
    vw = ind.vwap_bands(c1h[-24:]) if c1h else None
    rsi, z, rv = ind.rsi(ind.closes(c1h)), (vw["z"] if vw else None), ind.relative_volume(c1h)
    out = SignalOut(features={"rsi_1h": rsi, "vwap_z": z, "relvol_1h": rv})
    if rsi is not None and rsi >= 85:
        out.points.append((15, f"1h RSI {rsi:.0f}"))
    elif rsi is not None and rsi >= 80:
        out.points.append((10, f"1h RSI {rsi:.0f}"))
    if z is not None and z >= 3:
        out.points.append((15, f"{z:.1f} SD above 24h VWAP"))
    elif z is not None and z >= 2:
        out.points.append((10, f"{z:.1f} SD above 24h VWAP"))
    if rv is not None and rv >= 3:
        out.points.append((5, f"1h volume {rv:.1f}x avg"))
    return out


@signal("structure", "trigger")
async def structure(ctx: MarketContext) -> SignalOut:
    """Confirmation that the move is turning: 1h lower high, price rolling off the 24h high."""
    c1h = await ctx.candles("1h", 60)
    last = await ctx.last()
    high24 = max((c[2] for c in c1h[-24:]), default=None)
    off = (high24 - last) / high24 * 100 if high24 and last else None
    lh = ind.lower_high(c1h, 4)
    out = SignalOut(features={"lower_high": lh, "off_high_pct": off})
    if lh:
        out.points.append((10, "lower high on 1h"))
    if off is not None and 5 <= off <= 20:
        out.points.append((5, f"{off:.0f}% off 24h high (rolling over)"))
    return out


@signal("taker_flow", "trigger")
async def taker_flow(ctx: MarketContext) -> SignalOut:
    """Aggressive buy/sell ratio: flipping below 1 after buyers dominated = sellers taking over."""
    tk = await ctx.taker_ratio("1h", 6)
    now = tk[-1] if tk else None
    prev = sum(tk[:-1]) / len(tk[:-1]) if len(tk) > 1 else None
    out = SignalOut(features={"taker_ratio_now": now, "taker_ratio_prev": prev})
    if now is not None and prev is not None and now < 1 <= prev:
        out.points.append((5, "taker flow flipped to sellers"))
    return out


@signal("data_completeness", "guard")
async def data_completeness(ctx: MarketContext) -> SignalOut:
    """Fail closed: never call a coin tradable when the inputs behind the vetoes failed to load."""
    critical = sorted(CRITICAL_INPUTS & ctx.missing)
    if critical:
        return SignalOut(vetoes=[f"data unavailable ({', '.join(critical)}): can't verify vetoes, don't trade blind"])
    return SignalOut()


@signal("volatility", "core")
async def volatility(ctx: MarketContext) -> SignalOut:
    """Per-hour volatility for a 24h window: the gated model (probability/recal.py: the legacy gap-free EWMA of 5m
    returns until the horizon-matched HAR wins on QLIKE), Parkinson on 1h ranges beside it. HAR is computed here only
    once it is the model; before that the scanner adds it for the coins reaching the probability stage
    (scanner.add_har). Feeds the probability engine and vol-scaled brackets. Features only."""
    from ..probability.engine import sigma_hourly
    s = await sigma_hourly(ctx)
    return SignalOut(features={"sigma_h": s["used"], "sigma_h_ewma": s["ewma"], "sigma_h_parkinson": s["parkinson"],
                               "sigma_h_har": s["har"], "sigma_model": s["model"]})
