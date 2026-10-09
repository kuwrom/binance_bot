"""Probability engine: one entry point for "what are the odds of level X before level Y within T?".

Everything it returns is labelled NO-EDGE BASELINE: it prices the bracket geometry with realistic volatility
(closed form + fat-tailed Monte Carlo) but assumes no drift. Its jobs (docs/PROBABILITY.md):
- honesty check: EV from R:R alone is ≈ 0; a plan only has edge if our thesis beats these odds
- fill odds for limit entries, stop-noise risk, time-to-target realism
- shadow mode: every published p (model and, if given, the analyst's) is logged and later labelled, so
  calibration and the measured edge per setup/tape/regime accumulate
It must NOT feed Kelly sizing until the gate in docs/PROBABILITY.md is passed.
"""

from __future__ import annotations

import logging
from typing import Any

from ..market import DataHub, MarketContext
from . import baseline as bl
from .montecarlo import simulate_short
from .vol import clamp_sigma, ewma_sigma_hourly, gaps, parkinson_sigma_hourly
from .vol_har import H1_BARS, M15_BARS, horizon_sigma

log = logging.getLogger("shortbot.probability")

MC_HISTORY_5M = 1000   # ≈ 3.5 days of 5m bars for residuals


async def sigma_hourly(ctx: MarketContext, hours: float = 24.0, scale: float = 1.0) -> dict[str, Any]:
    """Hourly σ for a window of `hours`: `used` = the horizon-matched HAR forecast (vol_har.py) times the
    out-of-sample σ scale (recal.py; 1.0 until one passes), floored and capped; the legacy 5m EWMA when the 1h
    history is short. `ewma` and `parkinson` stay for comparison (QLIKE in evaluate.py scores each)."""
    from .. import clock
    flow = await ctx.flow_candles("5m", 288)
    c1h = await ctx.candles("1h", 60)
    ew = ewma_sigma_hourly([c[4] for c in flow], [c[0] for c in flow])
    pk = parkinson_sigma_hourly(c1h)
    hz = None
    try:
        hz = horizon_sigma(await ctx.candles("15m", M15_BARS), await ctx.candles("1h", H1_BARS), hours,
                           int(clock.now_dt().timestamp() * 1000))
    except Exception as e:  # noqa: BLE001 - the legacy forecast covers it
        log.warning("horizon σ failed for %s (%s: %s): EWMA used", getattr(ctx, "symbol", "?"), type(e).__name__, e)
    har = (hz or {}).get("sigma_h")
    base, model = (har, "har") if har else (ew, "ewma") if ew else (pk, "parkinson")
    return {"ewma": ew, "parkinson": pk, "har": har, "har_parts": hz, "model": model, "scale": scale,
            "hours": hours, "gaps_5m": gaps([c[0] for c in flow]) if flow else None,
            "used": clamp_sigma(base * scale) if base else None}


def sigma_context(sig: dict[str, Any]) -> dict[str, Any]:
    """The σ inputs a logged prediction carries, so QLIKE can score each estimator on the same labels."""
    return {"sigma_ewma": sig.get("ewma"), "sigma_har": sig.get("har"), "sigma_model": sig.get("model"),
            "sigma_scale": sig.get("scale")}


def vol_scaled_bracket(price: float, sigma_h: float, hours: float = 24.0, k_stop: float = 1.0,
                       reward: float = 2.0) -> tuple[float, float]:
    """Standard research bracket for scan candidates, in LOG space (always positive, even at extreme vol):
    stop = P·e^{kσ√T}, target = P·e^{−reward·kσ√T}."""
    import math
    move = sigma_h * hours**0.5
    return price * math.exp(k_stop * move), price * math.exp(-reward * k_stop * move)


class ProbabilityEngine:
    def __init__(self, ex, journal=None, hub: DataHub | None = None):
        self.ex, self.j = ex, journal
        self.hub = hub or DataHub(ex)

    async def short_bracket(self, symbol: str, stop: float, targets: list[float], hours: float,
                            entry: float | None = None, fill_hours: float | None = None, mc: bool = True,
                            log_source: str | None = None, analyst_p: float | None = None,
                            context: dict | None = None, ref_id: int | None = None) -> dict[str, Any]:
        ctx = self.hub.context(symbol)
        last = await ctx.last()
        entry = entry or last
        from .recal import scale_for
        sig = await sigma_hourly(ctx, hours, scale_for(self.j))
        s = sig["used"]
        if not s:
            return {"ok": False, "error": "no volatility estimate (not enough candles)"}
        out: dict[str, Any] = {"ok": True, "symbol": ctx.symbol, "price_now": last, "entry": entry, "stop": stop,
                               "hours": hours, "sigma_hourly": sig, "label": "NO-EDGE BASELINE (drift = 0)",
                               "stop_touch_prob": round(bl.touch_up(entry, stop, s, hours), 4), "targets": []}
        for t in sorted(targets, reverse=True):
            g = bl.short_geometry(entry, stop, t, s, hours)
            out["targets"].append({"target": t, **{k: round(v, 4) for k, v in g.items()}})
        if mc and targets:
            flow = await ctx.flow_candles("5m", MC_HISTORY_5M)
            sim = simulate_short([c[4] for c in flow], entry, stop, targets, hours, s, ts=[c[0] for c in flow])
            if sim.get("ok"):
                out["mc_hours_simulated"] = sim["hours_simulated"]   # < hours when the memory cap cut the horizon
                by_t = {x["target"]: x for x in sim["targets"]}
                for row in out["targets"]:
                    m = by_t.get(row["target"])
                    if m:
                        row["mc_p_target"], row["mc_p_stop"], row["mc_p_neither"] = (
                            round(m["p_target_first"], 4), round(m["p_stop_first"], 4), round(m["p_neither"], 4))
                        row["mc_ev_R"], row["mc_ev_R_se"] = round(m["ev_R"], 3), round(m["ev_R_se"], 3)
                        row["mc_avg_stop_loss_R"] = round(m["avg_stop_loss_R"], 3) if m["avg_stop_loss_R"] else None
        if entry > last and fill_hours:
            out["fill_prob"] = round(bl.touch_up(last, entry, s, fill_hours), 4)
            out["fill_note"] = "touch ≠ fill (queue) and fills are adversely selected: treat as an upper bound"
        if self.j is not None and log_source and out["targets"]:
            first = out["targets"][0]
            out["prediction_id"] = self.j.log_prediction(
                log_source, ctx.symbol, entry, stop, first["target"], hours, "bracket", first["p_target"],
                first.get("mc_p_target"), analyst_p, s, ref_id, {**(context or {}), **sigma_context(sig),
                                                                 "p_stop": first["p_stop"], "p_time": first["p_time"]})
            if "fill_prob" in out:
                out["fill_prediction_id"] = self.j.log_prediction(
                    f"{log_source}_fill", ctx.symbol, last, None, entry, fill_hours, "touch", out["fill_prob"],
                    None, None, s, ref_id, context)
        return out
