"""Failure-mode classifier for losing backtest trades (docs/FAILURES.md). Pure functions over one simulated trade
(sim.simulate output + symbol/strategy) and the coin's bars; used for the IN-SAMPLE diagnosis only.

Primary mode (first match wins, losers only, r < 0):
  f_funding            gross R >= 0 and funding paid > fees + slippage
  e_cost               gross R >= 0 otherwise (the idea broke even; costs made it a loss)
  b_stop_then_worked   stopped, then price reached the first target within 24h after the stop (stop too tight)
  a_instant_squeeze    stopped within 2 bars of the entry (MAE >= 1R immediately)
  h_stop_thesis_wrong  stopped later, and the target was never reached
  d_flat_time          time stop, never moved: max(MAE, MFE) < 0.5R (dead coin / wrong timing)
  h_time_drift         time stop after drifting against the short"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

STOP_REASONS = ("stop", "stop_gap")


def worked_after(rows: list[dict], exit_i: int, t1: float, hours: int = 24) -> bool:
    """Did any bar in the `hours` after the exit trade at or below the first target?"""
    return any(r["l"] <= t1 for r in rows[exit_i + 1:exit_i + 1 + hours])


def failure_mode(t: dict[str, Any], worked: bool = False) -> str:
    if t["r"] >= 0:
        return "win"
    if t["r_gross"] >= 0:
        return "f_funding" if -t["funding_pct"] > t["fees_pct"] + t["slip_pct"] else "e_cost"
    if t["reason"] in STOP_REASONS:
        if worked:
            return "b_stop_then_worked"
        return "a_instant_squeeze" if t["bars"] <= 2 else "h_stop_thesis_wrong"
    if t["reason"] in ("time", "eod"):
        return "d_flat_time" if max(t["mae_r"], t["mfe_r"]) < 0.5 else "h_time_drift"
    return f"h_{t['reason']}"


def mode_table(trades: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """{mode: {n, share of losing R (%), avg R}} for trades that carry a 'mode' key."""
    lose = [t for t in trades if t["r"] < 0]
    tot = -sum(t["r"] for t in lose) or 1.0
    by: dict[str, list[float]] = defaultdict(list)
    for t in lose:
        by[t["mode"]].append(t["r"])
    return {k: {"n": len(v), "share_pct": round(-sum(v) / tot * 100, 1), "avg_r": round(statistics.fmean(v), 2)}
            for k, v in sorted(by.items())}


def auc(a: list[float], b: list[float]) -> float | None:
    """P(x_a > x_b) with ties counted half (Mann-Whitney AUC): 0.5 = the feature does not separate the groups."""
    a, b = [x for x in a if x is not None], [x for x in b if x is not None]
    if not a or not b:
        return None
    allv = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ranks, i = [0.0] * len(allv), 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2 + 1
        i = j + 1
    ra = sum(r for r, (_, g) in zip(ranks, allv) if g == 0)
    return (ra - len(a) * (len(a) + 1) / 2) / (len(a) * len(b))
