"""Volatility estimates in PER-HOUR units (the probability layer's time unit).
EWMA on 5m log returns (half-life ≈ 1 day) is the legacy forecast; Parkinson on 1h ranges is the cross-check. The
horizon-matched forecast (HAR blend of range estimators with an hour-of-day profile) is vol_har.py.

Returns never span a timestamp gap: a missing bar (a stream outage, a file join) would fold several bars' move into
one return and inflate σ (research note 2026-10-09: 46 joins in hist5m made a fake 2.65x hour-0 spike)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

BARS_PER_HOUR_5M = 12
HALF_LIFE_BARS = 288          # ≈ 1 day of 5m bars
GAP_TOL = 1.5                 # a step longer than 1.5 bars is a gap
SIGMA_FLOOR_H = 0.0005        # 0.05%/h (≈ 2.4%/day): below it the bars are stale or the coin is pegged
SIGMA_CAP_H = 0.25            # 25%/h: above it the data is broken, not the market


def clamp_sigma(s: float | None) -> float | None:
    """The sanity band for an hourly σ forecast; None stays None."""
    if s is None or not math.isfinite(s) or s <= 0:
        return None
    return min(SIGMA_CAP_H, max(SIGMA_FLOOR_H, s))


def bar_ms_of(ts: Sequence[float] | np.ndarray[Any, Any]) -> float:
    """The series' bar length: the median step between timestamps."""
    d = np.diff(np.asarray(ts, dtype=float))
    d = d[d > 0]
    return float(np.median(d)) if len(d) else 0.0


def gap_mask(ts: Sequence[float] | np.ndarray[Any, Any], bar_ms: float | None = None) -> np.ndarray:
    """For each step i -> i+1: True when it spans no gap (at most GAP_TOL bars)."""
    t = np.asarray(ts, dtype=float)
    if len(t) < 2:
        return np.zeros(0, dtype=bool)
    bar = bar_ms or bar_ms_of(t)
    d = np.diff(t)
    return (d > 0) & (d <= GAP_TOL * bar) if bar > 0 else np.ones(len(d), dtype=bool)


def log_returns(closes: Sequence[float], ts: Sequence[float] | None = None, bar_ms: float | None = None) -> np.ndarray:
    """Log returns of consecutive positive closes. With timestamps, a return across a gap (or a non-positive close)
    is dropped, never stretched over the missing bars."""
    c = np.asarray(closes, dtype=float)
    if ts is None:
        c = c[c > 0]
        return np.diff(np.log(c)) if len(c) > 1 else np.zeros(0)
    if len(c) < 2:
        return np.zeros(0)
    ok = gap_mask(ts, bar_ms) & (c[:-1] > 0) & (c[1:] > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.log(np.where(c[1:] > 0, c[1:], 1.0) / np.where(c[:-1] > 0, c[:-1], 1.0))
    return r[ok]


def gaps(ts: Sequence[float], bar_ms: float | None = None) -> int:
    """How many steps of the series span a gap (flagged next to every σ built from it)."""
    return int((~gap_mask(ts, bar_ms)).sum())


def ewma_sigma_series(rets: np.ndarray, half_life: int = HALF_LIFE_BARS) -> np.ndarray:
    """Per-bar EWMA volatility; seeded with the sample variance of the first 50 bars."""
    if len(rets) == 0:
        return np.zeros(0)
    lam = 0.5 ** (1 / half_life)
    var = float(np.var(rets[:50])) if len(rets) >= 5 else float(rets[0] ** 2)
    out = np.empty(len(rets))
    for i, r in enumerate(rets):
        out[i] = math.sqrt(max(var, 1e-18))
        var = lam * var + (1 - lam) * r * r
    return out


def ewma_sigma_hourly(closes_5m: Sequence[float], ts: Sequence[float] | None = None) -> float | None:
    """Current EWMA vol of 5m returns, scaled to per hour (the legacy forecast). With timestamps, gap-free returns."""
    r = log_returns(closes_5m, ts)
    if len(r) < 30:
        return None
    lam = 0.5 ** (1 / HALF_LIFE_BARS)
    var = float(np.var(r[:50]))
    for x in r:
        var = lam * var + (1 - lam) * x * x
    return math.sqrt(var * BARS_PER_HOUR_5M)


def parkinson_sigma_hourly(candles_1h: list[list[float]]) -> float | None:
    """σ² = mean((ln H/L)²)/(4 ln 2) on 1h bars: uses the intrabar range, robust with few bars."""
    rng = [math.log(c[2] / c[3]) ** 2 for c in candles_1h if c[3] > 0 and c[2] >= c[3]]
    if len(rng) < 5:
        return None
    return math.sqrt(sum(rng) / len(rng) / (4 * math.log(2)))
