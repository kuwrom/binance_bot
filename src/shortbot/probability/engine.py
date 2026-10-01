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

from typing import Any

from ..market import DataHub, MarketContext
from . import baseline as bl
from .montecarlo import simulate_short
from .vol import ewma_sigma_hourly, parkinson_sigma_hourly

MC_HISTORY_5M = 1000   # ≈ 3.5 days of 5m bars for residuals


async def sigma_hourly(ctx: MarketContext) -> dict[str, float | None]:
    flow = await ctx.flow_candles("5m", 288)
    c1h = await ctx.candles("1h", 60)
    ew = ewma_sigma_hourly([c[4] for c in flow])
    pk = parkinson_sigma_hourly(c1h)
    return {"ewma": ew, "parkinson": pk, "used": ew or pk}


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
        sig = await sigma_hourly(ctx)
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
            sim = simulate_short([c[4] for c in flow], entry, stop, targets, hours, s)
            if sim.get("ok"):
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
                first.get("mc_p_target"), analyst_p, s, ref_id, context)
            if "fill_prob" in out:
                out["fill_prediction_id"] = self.j.log_prediction(
                    f"{log_source}_fill", ctx.symbol, last, None, entry, fill_hours, "touch", out["fill_prob"],
                    None, None, s, ref_id, context)
        return out
