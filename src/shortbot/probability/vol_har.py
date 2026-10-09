"""Horizon-matched σ forecast (research note 2026-10-09; docs/PROBABILITY.md "Volatility"):

- **Sampling matched to the horizon.** 5m returns overstate 24h variance by ~15-30% on our coins (15m reversal,
  Kitron & Wengrowicz 2026; our VR(24h/5m) 0.70-0.84). Components are built from 15m bars for horizons under 4h and
  from 1h bars otherwise.
- **Range estimator.** Yang-Zhang 2000 (drift-independent, ~8x close-to-close efficiency) on those bars; returns and
  the open-vs-previous-close term never span a timestamp gap.
- **HAR blend** (Corsi 2009) of a daily (24h), weekly (168h) and monthly (the 1h history fetched, ~20 days)
  component. Weights are fixed by horizon (FIXED, an assumption scored by QLIKE in evaluate.py; never fitted per coin):
  short horizons lean on the daily component, multi-day ones on the weekly and monthly.
- **Hour-of-day profile** (Hansen-Kim-Kimbrough 2021): each UTC hour's share of variance from the 1h history, shrunk
  to 1 and integrated over the forward window, so a 4h window over 07-11 UTC is wider than one over 21-01.

No drift anywhere: this is the σ of the no-edge baseline, and the legacy EWMA stays next to it for comparison."""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from typing import Any

import numpy as np

from .vol import clamp_sigma, gap_mask

H1_BARS = 499           # < 500: the klines weight-2 tier; ~20.8 days
M15_BARS = 96           # 24h of 15m bars (a limit other signals already fetch: the same weight-1 tier)
SHORT_H = 4.0           # horizons under this use 15m bars for the daily component
SHRINK_N = 20           # hour-of-day factor shrinkage toward 1: weight n/(n + SHRINK_N)
MIN_BARS = {"d": 12, "w": 72, "m": 240}
# (hours, (w_daily, w_weekly, w_monthly)): interpolated in log(hours) between anchors, clamped at the ends
ANCHORS = ((1.0, (0.6, 0.3, 0.1)), (24.0, (0.45, 0.35, 0.2)), (168.0, (0.2, 0.4, 0.4)))
HOUR_MS = 3_600_000


def har_weights(hours: float) -> tuple[float, float, float]:
    h = max(hours, 1e-6)
    if h <= ANCHORS[0][0]:
        return ANCHORS[0][1]
    for (h0, w0), (h1, w1) in itertools.pairwise(ANCHORS):
        if h <= h1:
            x = (math.log(h) - math.log(h0)) / (math.log(h1) - math.log(h0))
            return (w0[0] + x * (w1[0] - w0[0]), w0[1] + x * (w1[1] - w0[1]), w0[2] + x * (w1[2] - w0[2]))
    return ANCHORS[-1][1]


def closed(bars: list[list[float]]) -> list[list[float]]:
    """The candles minus the forming one (the hub returns it last)."""
    return bars[:-1] if len(bars) > 1 else []


def yang_zhang_var(bars: Sequence[Sequence[float]]) -> float | None:
    """Yang-Zhang variance PER BAR of [ts, o, h, l, c, ...] bars: var(open vs previous close, gap-free steps only)
    + k var(close vs open) + (1-k) mean(Rogers-Satchell), k = 0.34 / (1.34 + (n+1)/(n-1))."""
    b = np.asarray([x[:5] for x in bars if min(x[1:5]) > 0], dtype=float)
    n = len(b)
    if n < 3:
        return None
    o, h, lo, c = b[:, 1], b[:, 2], b[:, 3], b[:, 4]
    jump = np.log(o[1:] / c[:-1])[gap_mask(b[:, 0])]
    oc = np.log(c / o)
    rs = np.log(h / c) * np.log(h / o) + np.log(lo / c) * np.log(lo / o)
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    v_jump = float(np.var(jump, ddof=1)) if len(jump) > 1 else 0.0
    return max(0.0, v_jump + k * float(np.var(oc, ddof=1)) + (1 - k) * float(np.mean(rs)))


def hour_profile(bars_1h: Sequence[Sequence[float]]) -> list[float]:
    """24 factors (mean 1): each UTC hour's mean squared gap-free 1h return over the overall mean, shrunk to 1."""
    b = [x for x in bars_1h if x[4] > 0]
    if len(b) < 48:
        return [1.0] * 24
    ts = [x[0] for x in b]
    ok = gap_mask(ts)
    r2 = np.log(np.asarray([x[4] for x in b[1:]]) / np.asarray([x[4] for x in b[:-1]])) ** 2
    hours = (np.asarray(ts[1:], dtype=np.int64) // HOUR_MS) % 24
    r2, hours = r2[ok], hours[ok]
    mean = float(r2.mean()) if len(r2) else 0.0
    if mean <= 0:
        return [1.0] * 24
    f = []
    for hr in range(24):
        x = r2[hours == hr]
        raw = float(x.mean()) / mean if len(x) else 1.0
        f.append(1 + (raw - 1) * len(x) / (len(x) + SHRINK_N))
    m = sum(f) / 24
    return [v / m for v in f]


def window_factor(profile: Sequence[float], start_ms: int, hours: float) -> float:
    """Mean hour-of-day factor over [start, start + hours]: the forward window's share of a day's variance."""
    if hours <= 0:
        return 1.0
    end, t, acc = start_ms + hours * HOUR_MS, float(start_ms), 0.0
    while t < end - 1e-6:
        nxt = min(end, (math.floor(t / HOUR_MS) + 1) * HOUR_MS)
        acc += profile[int(t // HOUR_MS) % 24] * (nxt - t)
        t = nxt
    return acc / (hours * HOUR_MS)


def horizon_sigma(c15: list[list[float]], c1h: list[list[float]], hours: float, now_ms: int,
                  drop_forming: bool = True) -> dict[str, Any] | None:
    """The hourly σ for a window of `hours` from now, with its parts (per-hour σ of each component, weights, the
    seasonal factor). None when the 1h history is too short for even the daily component. drop_forming=False: the
    bars given are closed already (closed_history)."""
    h1, m15 = (closed(c1h), closed(c15)) if drop_forming else (c1h, c15)
    use15 = hours < SHORT_H and len(m15) >= 2 * MIN_BARS["d"]
    v_d = yang_zhang_var(m15[-M15_BARS:]) if use15 else (yang_zhang_var(h1[-24:]) if len(h1) >= MIN_BARS["d"] else None)
    var_h = {"d": v_d * 4 if use15 and v_d is not None else v_d,
             "w": yang_zhang_var(h1[-168:]) if len(h1) >= MIN_BARS["w"] else None,
             "m": yang_zhang_var(h1) if len(h1) >= MIN_BARS["m"] else None}
    if var_h["d"] is None:
        return None
    w = dict(zip("dwm", har_weights(hours), strict=True))
    have = {k: v for k, v in var_h.items() if v is not None}
    total_w = sum(w[k] for k in have)
    base = sum(w[k] * v for k, v in have.items()) / total_w
    season = window_factor(hour_profile(h1), now_ms, hours)
    sig = clamp_sigma(math.sqrt(base * season)) if base > 0 else None
    return {"sigma_h": sig, "parts": {k: round(math.sqrt(v), 6) for k, v in have.items()},
            "weights": {k: round(w[k] / total_w, 3) for k in have}, "season": round(season, 3),
            "sampling": "15m" if use15 else "1h", "bars_1h": len(h1)}


# Closed bars change only when a bar closes: each (symbol, timeframe) history is fetched at most once per bar of that
# timeframe in this process, whatever the number of contexts, passes or callers (a cold stream store means REST).
_HIST: dict[tuple[str, str, int], tuple[int, list[list[float]]]] = {}
HIST_MAX = 1024
TF_MS = {"15m": HOUR_MS // 4, "1h": HOUR_MS}


async def closed_history(ctx: Any, tf: str, n: int) -> list[list[float]]:
    """The last n bars of `tf` minus the forming one, through ctx.candles (the stream store first, then the guarded
    REST client: weight.py admits every call), cached until the next `tf` bar closes. A failed read is not cached."""
    from .. import clock
    step = TF_MS[tf]
    bar_end = clock.now_ms() // step * step + step
    key = (ctx.symbol, tf, n)
    hit = _HIST.get(key)
    if hit and hit[0] == bar_end:
        return hit[1]
    bars = closed(await ctx.candles(tf, n + 1))
    if bars:
        _HIST.pop(key, None)
        _HIST[key] = (bar_end, bars)
        while len(_HIST) > HIST_MAX:
            _HIST.pop(next(iter(_HIST)))
    return bars


async def forecast(ctx: Any, hours: float, now_ms: int) -> tuple[dict[str, Any] | None, list[list[float]]]:
    """(horizon_sigma, the closed 1h history it used). 15m bars are read only for windows under SHORT_H."""
    h1 = await closed_history(ctx, "1h", H1_BARS - 1)
    m15 = await closed_history(ctx, "15m", M15_BARS - 1) if hours < SHORT_H else []
    return horizon_sigma(m15, h1, hours, now_ms, drop_forming=False), h1
