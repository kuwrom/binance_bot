"""Tape / order-flow signals (docs/TAPE.md). Evidence for short-horizon reversal is weak, so these are mostly
VETOES (don't fight real demand, accepted breakouts, pumps or liquidation cascades) plus small points for
fade-supporting footprints. Research decides whether any deserve more weight.
Missing tape data never vetoes (only the core inputs are critical)."""

from __future__ import annotations

import time

from .. import robust_math as rm
from .. import tape_math as tm
from ..decay import decayed_points, heat
from ..market import MarketContext
from .base import SignalOut, signal

SWEEP_TF, SWEEP_BARS, SWEEP_LOOKBACK = "15m", 96, 32   # L = prior 8h high on 15m bars
TI_TF, TI_BARS, TI_WINDOW = "5m", 288, 12              # 1h taker imbalance vs the last 24h
PERP_LED_GAP_PP, BASIS_WIDEN_BP, SPOT_TI_BUY = 0.3, 10.0, 0.10
LIQ_BURST_MULT, LIQ_BURST_MIN_USD = 3.0, 50_000


@signal("sweep", "tape")
async def sweep(ctx: MarketContext) -> SignalOut:
    """Failed sweep of the prior-8h high / round number (bearish trigger) vs accepted breakout (veto)."""
    bars = await ctx.flow_candles(SWEEP_TF, SWEEP_BARS)
    s = tm.sweep_state(bars, SWEEP_LOOKBACK)
    out = SignalOut(features={"sweep_state": s.get("state"), "sweep_level": s.get("swept") or s.get("level"),
                              "sweep_level_type": s.get("level_type"), "sweep_high": s.get("sweep_high"),
                              "round_level": s.get("round_level")})
    if s.get("state") == "failed_sweep":
        # robust confirmation: judge the rejection by 1m TIME and VOLUME above the level (+ mark pierce), not the print
        one = await ctx.flow_candles("1m", 60)
        mark1 = await ctx.mark_candles("1m", 60)
        mhigh = max((m[2] for m in mark1), default=None) if mark1 else None
        rej = rm.sweep_rejection(one, s["swept"], mhigh) if one else {"rejected": None}
        out.features.update({"sweep_rejected_robust": rej.get("rejected"),
                             "sweep_acceptance_time": rej.get("acceptance_time"),
                             "sweep_acceptance_volume": rej.get("acceptance_volume")})
        if rej.get("rejected") is False:
            out.features["sweep_state"] = "unconfirmed"
            out.vetoes.append(f"sweep of {s['swept']:.6g} closed back below, but time/volume stayed above the level "
                              f"({(rej.get('acceptance_time') or 0):.0%} time): a painted rejection, not a real one")
            return out
        wick_i = max(range(len(bars) - 3, len(bars)), key=lambda k: bars[k][2])
        age_h = (len(bars) - 1 - wick_i) * 0.25
        out.features["sweep_age_h"] = age_h
        out.points.append((decayed_points(10, "sweep", age_h),
                           f"failed sweep of {s['level_type']} {s['swept']:.6g} (wick {s['sweep_high']:.6g}, {age_h:.2g}h ago)"))
        liqs = await ctx.liquidations(int(time.time() * 1000) - 3 * 900_000)
        shorts = sum(x["notional"] for x in liqs if x["side"] == "short_liquidated")
        out.features["sweep_short_liq_usd"] = shorts
        if shorts > 0:
            out.points.append((5, f"short liquidations into the sweep (${shorts:,.0f})"))
    elif s.get("state") == "accepted":
        out.vetoes.append(f"breakout accepted above {s['level_type']} {s['swept']:.6g} (2 closes): stops "
                          "cascaded, trend likely continues")
    return out


@signal("taker_imbalance", "tape")
async def taker_imbalance(ctx: MarketContext) -> SignalOut:
    """1h taker imbalance and its z-score vs the last 24h: a strong aggressive-buying burst is a veto
    (order flow continues; Cont-Kukanov-Stoikov, crypto OFI studies)."""
    bars = await ctx.flow_candles(TI_TF, TI_BARS)
    ti, z = tm.ti_zscore(bars, TI_WINDOW)
    rising = len(bars) > TI_WINDOW and bars[-1][4] > bars[-TI_WINDOW][1]
    out = SignalOut(features={"ti_1h": ti, "ti_z_1h": z})
    if z is not None and z > 2 and rising:
        out.vetoes.append(f"aggressive buying burst (1h taker imbalance z={z:.1f}) with price rising")
    return out


@signal("cvd_divergence", "tape")
async def cvd_divergence(ctx: MarketContext) -> SignalOut:
    """Price makes a higher high while CVD makes a lower high on 15m (buyers exhausting)."""
    bars = await ctx.flow_candles(SWEEP_TF, SWEEP_BARS)
    div = tm.cvd_bearish_divergence(bars, 8)
    out = SignalOut(features={"cvd_divergence": div})
    if div:
        out.points.append((5, "CVD bearish divergence on 15m (higher high, weaker buying)"))
    return out


@signal("perp_vs_spot", "tape")
async def perp_vs_spot(ctx: MarketContext) -> SignalOut:
    """Perp-led move (perp outruns spot, basis widening: leverage, fade-prone) vs spot-led rally (spot buyers,
    basis ≤ 0: real demand, veto)."""
    perp = await ctx.flow_candles("1h", 7)
    spot = await ctx.spot_flow("1h", 7)
    prem = await ctx.premium("1h", 7)
    out = SignalOut(features={"spot_available": bool(spot.get("raw"))})
    if not (perp and spot.get("candles") and len(perp) >= 7 and len(spot["candles"]) >= 7):
        return out
    sc = spot["candles"]
    perp_ret = (perp[-1][4] - perp[0][1]) / perp[0][1] * 100
    spot_ret = (sc[-1][4] - sc[0][1]) / sc[0][1] * 100
    spot_ti = tm.taker_imbalance(sc)
    basis_now = prem[-1][4] * 1e4 if prem else None
    basis_chg = (prem[-1][4] - prem[0][4]) * 1e4 if len(prem) > 1 else None
    out.features.update({"perp_ret_6h": perp_ret, "spot_ret_6h": spot_ret, "spot_ti_6h": spot_ti,
                         "basis_bp": basis_now, "basis_chg_6h_bp": basis_chg})
    if perp_ret - spot_ret > PERP_LED_GAP_PP and basis_chg is not None and basis_chg > BASIS_WIDEN_BP:
        out.points.append((5, f"perp-led move: perp {perp_ret:+.1f}% vs spot {spot_ret:+.1f}%, basis +{basis_chg:.0f}bp"))
    if (perp_ret > 0 and spot_ret >= perp_ret and spot_ti is not None and spot_ti > SPOT_TI_BUY
            and basis_now is not None and basis_now <= 0):
        out.vetoes.append(f"spot-led rally: spot {spot_ret:+.1f}% with spot buyers (TI {spot_ti:+.2f}), basis "
                          f"{basis_now:.0f}bp: real demand, don't fade")
    return out


@signal("crowding", "tape")
async def crowding(ctx: MarketContext) -> SignalOut:
    """Crowded longs: price up, OI +10%/24h, funding ≥ 0.03%/8h (fade only after a confirmed turn)."""
    chg = float((await ctx.ticker()).get("percentage") or 0)
    oi = await ctx.oi_history("1h", 25)
    f = await ctx.funding()
    oi_chg = (oi[-1]["oi"] - oi[0]["oi"]) / oi[0]["oi"] * 100 if len(oi) > 1 and oi[0]["oi"] else None
    rate, iv = f.get("rate"), int(f.get("interval_h") or 8)
    f8 = rate * 8 / iv if rate is not None else None
    crowded = chg > 0 and oi_chg is not None and oi_chg >= 10 and f8 is not None and f8 >= 0.0003
    out = SignalOut(features={"crowded_longs": crowded})
    if crowded:
        out.points.append((5, f"crowded longs: OI +{oi_chg:.0f}%, funding {f8 * 100:.3f}%/8h"))
    return out


@signal("pump", "tape")
async def pump(ctx: MarketContext) -> SignalOut:
    """Kamps & Kleinberg pump detector on 1h: in progress = veto; decaying below the pump VWAP = short bounces."""
    bars = await ctx.flow_candles("1h", 48)
    p = tm.pump_state(bars)
    out = SignalOut(features={"pump_state": p.get("state"), "pump_bars_ago": p.get("bars_ago")})
    if p.get("state") == "in_progress":
        out.vetoes.append("pump in progress (1h return > 5% on > 4x volume): wait for rush orders to stop")
    elif p.get("state") == "decaying" and p.get("bars_ago", 99) <= 48:
        pts = decayed_points(5, "pump", p["bars_ago"])
        if pts:
            out.points.append((pts, f"post-pump decay below the pump VWAP ({p['bars_ago']}h after)"))
    return out


@signal("liquidations", "tape")
async def liquidations(ctx: MarketContext) -> SignalOut:
    """Recorded forced orders: a long-liquidation cascade overshoots then gets bought back (Brunnermeier &
    Pedersen): veto fresh shorts into it. Empty if the guardian's recorder isn't running."""
    now = int(time.time() * 1000)
    rows = await ctx.liquidations(now - 3_600_000)
    last15 = [r for r in rows if r["ts_ms"] >= now - 900_000]
    prior = [r for r in rows if r["ts_ms"] < now - 900_000]
    long15 = sum(r["notional"] for r in last15 if r["side"] == "long_liquidated")
    long_prior = sum(r["notional"] for r in prior if r["side"] == "long_liquidated") / 3
    short1h = sum(r["notional"] for r in rows if r["side"] == "short_liquidated")
    out = SignalOut(features={"liq_long_15m_usd": long15, "liq_short_1h_usd": short1h,
                              "liq_long_prior_15m_avg_usd": long_prior})
    if long15 > LIQ_BURST_MIN_USD and long15 > LIQ_BURST_MULT * max(long_prior, 1):
        out.vetoes.append(f"long-liquidation cascade (${long15:,.0f} in 15m): forced selling overshoots, don't chase")
    return out


@signal("volume_heat", "tape")
async def volume_heat(ctx: MarketContext) -> SignalOut:
    """Is activity HOT (accelerating) or COOLING? Hourly volume over 24h: robust z of the last hour, fast/slow
    EWMA ratio, acceleration. Accelerating volume with price rising = don't short yet (attention continues);
    cooling after a pump = fade support."""
    bars = await ctx.flow_candles("5m", 288)
    hourly = [sum(b[5] for b in bars[i:i + 12]) for i in range(len(bars) % 12, len(bars), 12)]
    h = heat(hourly, fast_hl=2, slow_hl=12)
    rising = len(bars) >= 12 and bars[-1][4] > bars[-12][1]
    chg = float((await ctx.ticker()).get("percentage") or 0)
    out = SignalOut(features={"vol_heat_z": h["z"], "vol_heat_ratio": h["ratio"], "vol_hot": h["hot"],
                              "vol_cooling": h["cooling"]})
    if h["hot"] and rising:
        out.vetoes.append(f"volume still accelerating (z={h['z']}) with price rising: attention continues, wait")
    elif h["cooling"] and chg >= 10:
        out.points.append((3, f"volume cooling after a +{chg:.0f}% move (fast/slow {h['ratio']})"))
    return out
