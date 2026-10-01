"""Volatility estimates in PER-HOUR units (the probability layer's time unit).
EWMA on 5m log returns (half-life ≈ 1 day) is the forecast; Parkinson on 1h ranges is the cross-check."""

from __future__ import annotations

import math

import numpy as np

BARS_PER_HOUR_5M = 12
HALF_LIFE_BARS = 288          # ≈ 1 day of 5m bars


def log_returns(closes: list[float]) -> np.ndarray:
    c = np.asarray(closes, dtype=float)
    c = c[c > 0]
    return np.diff(np.log(c)) if len(c) > 1 else np.zeros(0)


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


def ewma_sigma_hourly(closes_5m: list[float]) -> float | None:
    """Current EWMA vol of 5m returns, scaled to per hour."""
    r = log_returns(closes_5m)
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
