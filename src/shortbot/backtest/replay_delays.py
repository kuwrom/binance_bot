"""Entry-delay sensitivity of the replayed setups (task #73): the live desk scans every ~30 minutes and acts minutes
later, while the backtest fills at the signal bar's close. Each replayed candidate that was tradable with a setup is
entered at the scan time + 0 / 5 / 15 / 30 minutes through sim.simulate (fills, fees, slippage, funding), with a
plan fixed AT THE SCAN, so a later entry inherits a stop and target the market may have moved toward:
- stop / target: the scan's own research bracket (scanner._log_research_brackets: stop = P·e^{σ√24h},
  target = P·e^{-2σ√24h}, one target for the full size);
- time stop: the setup's default (setups.SETUPS), capped at 72 h (the label horizon); breakeven at the setup's
  breakeven_at_pct, expressed in R.
One trade per setup-episode (research_stats.episodes over the time stop), so 30-minute rescans count once.
Inference: cluster_se.mean_inference (day, coin and two-way clustered t of the pooled mean R)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .. import cluster_se as CS
from .. import research_stats as rs
from ..probability.engine import vol_scaled_bracket
from ..setups import SETUPS
from .sim import COSTS, Costs, Plan, simulate

FIVE = 300_000
DELAYS_MIN = (0, 5, 15, 30)
MAX_HOLD_H = 72


def hold_hours(setup: str) -> float:
    s = SETUPS.get(setup)
    h = (s.default_plan.get("time_stop_hours") if s else None) or 24
    return min(float(h), MAX_HOLD_H)


def plan_for(setup: str, price: float, sigma_h: float, entry_px: float) -> Plan | None:
    """The scan's bracket around the SCAN price, the setup's time stop and breakeven; None if already invalid."""
    stop, target = vol_scaled_bracket(price, sigma_h)
    if entry_px >= stop or entry_px <= target:
        return None
    be_pct = (SETUPS[setup].default_plan.get("breakeven_at_pct") if setup in SETUPS else None)
    risk_pct = (stop - entry_px) / entry_px * 100
    return Plan(stop=stop, targets=((target, 1.0),), time_stop=int(hold_hours(setup) * 12),
                be_r=be_pct / risk_pct if be_pct else None, meta={"setup": setup})


def rows_of(bars: Sequence[tuple]) -> list[dict[str, Any]]:
    """replay_bars tuples -> sim rows."""
    return [{"ts": b[0], "o": b[1], "h": b[2], "l": b[3], "c": b[4], "qv": b[6]} for b in bars]


def simulate_delays(cand: dict[str, Any], setup: str, rows: list[dict[str, Any]], funding: list,
                    delays: Sequence[int] = DELAYS_MIN, costs: Costs = COSTS) -> dict[int, dict[str, Any] | None]:
    """{delay: simulate() result or None} for one candidate. rows: 5m bars around the scan (opens at t)."""
    f, t = cand["features"], cand["ts_ms"]
    price, sigma = f.get("last") or cand.get("price"), f.get("sigma_h")
    pos = {r["ts"]: k for k, r in enumerate(rows)}
    out: dict[int, dict[str, Any] | None] = {}
    for d in delays:
        i = pos.get((t + d * 60_000) // FIVE * FIVE - FIVE)       # the 5m bar that closes at (or before) t + d
        if not price or not sigma or i is None:
            out[d] = None
            continue
        plan = plan_for(setup, price, sigma, rows[i]["c"])
        if plan is None or i + 1 + plan.time_stop >= len(rows):   # invalid at entry, or the exit is censored
            out[d] = None
            continue
        out[d] = simulate(rows, i, plan, funding=funding, costs=costs, vol24_usd=f.get("quote_volume") or None)
    return out


def setup_episodes(cands: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Tradable candidates by setup, first scan of each symbol-episode (the setup's hold) only."""
    by: dict[str, list[dict[str, Any]]] = {}
    for c in cands:
        if c["tradable"]:
            for s in c["setups"] or []:
                by.setdefault(s, []).append(c)
    return {s: rs.episodes(rows, hold_hours(s)) for s, rows in by.items()}


def table(results: dict[str, list[tuple[dict[str, Any], dict[int, dict | None]]]],
          delays: Sequence[int] = DELAYS_MIN) -> list[dict[str, Any]]:
    """One row per setup x delay: n, mean R and clustered t's (filled trades only)."""
    out = []
    for setup, items in sorted(results.items()):
        for d in delays:
            done = [(c, r[d]) for c, r in items if r.get(d) and r[d].get("filled")]
            vals = [x["r"] for _, x in done]
            days = [rs.day_of(c["ts_ms"]) for c, _ in done]
            coins = [c["symbol"] for c, _ in done]
            inf = CS.mean_inference(vals, days, coins)
            daily: dict[str, float] = {}
            for v, dd in zip(vals, days):
                daily[dd] = daily.get(dd, 0.0) + v
            out.append({"setup": setup, "delay_min": d, "episodes": len(items), "n": inf["n"], "mean_r": inf["mean"],
                        "t_day": inf["t_day"], "t_coin": inf["t_coin"], "t_2way": inf["t_2way"],
                        "se_2way": inf["se_2way"], "days": inf["days"], "coins": inf["coins"],
                        "win": sum(v > 0 for v in vals) / len(vals) if vals else None,
                        "stopped": sum(str(x.get("reason", "")).startswith("stop") for _, x in done),
                        "daily": daily})
    return out


def register_rows(tab: list[dict[str, Any]], cadence_min: int, scan: str) -> list[dict[str, Any]]:
    """trial_register rows: one trial per setup x delay (family replay.<setup>)."""
    return [{"family": f"replay.{r['setup']}", "name": f"replay.{r['setup']}.d{r['delay_min']}",
             "params": {"setup": r["setup"], "delay_min": r["delay_min"], "plan": "bracket_1x2_24h",
                        "cadence_min": cadence_min, "scan": scan},
             "metric": "R", "n": r["n"], "days": r["days"], "coins": r["coins"], "mean": r["mean_r"],
             "se": r["se_2way"], "t": r["t_2way"], "t_kind": "2way", "daily": r["daily"]} for r in tab if r["n"]]
