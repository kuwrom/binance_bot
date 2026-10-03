"""Multi-timeframe state per coin (task #41): 5m, 15m, 1h, 4h and 1d seen at once, so rules can chain across them.

One vectorised function per timeframe, used unchanged for live bars (the stream store) and for history (the replay's
1m bars aggregated exactly, backtest/chain_study.py). A chained rule therefore means the same thing in research and
live. Every column at bar i uses bars up to i only, and live readers take the last CLOSED bar of each timeframe.

Per timeframe, on its own bars (LOOK bars = 1h on 5m, 4h on 15m, 1d on 1h, 1w on 4h and 1d):
- ret: the return over LOOK bars;
- move_z: the LOOK-bar log return over the coin's own volatility (std of the 96 returns before it) x sqrt(LOOK);
- rsi: Wilder RSI(14) (exponential smoothing, alpha 1/14);
- ema_gap: close / EMA20 - 1; ema_slope: the EMA20's change over 4 bars (fraction);
- lower_high / higher_low: the last 4 bars' high below the 4 before (indicators.lower_high(window=4)), and the
  mirror for lows;
- off_high: close / the highest high of the last 24 bars - 1 (<= 0);
- vol_z: robust z (median / MAD) of the last 4 bars' volume against the 24 non-overlapping 4-bar sums before;
- ti: taker imbalance (2 x taker-buy - volume) / volume over the last 4 bars (tape_math), NaN without taker volume.

    series(bars, tf) -> {column: np.ndarray}     bars: rows [ts, o, h, l, c, v] or [..., taker_buy_v]
    latest(bars_by_tf) -> {tf: {column: value}}  the last bar of each timeframe (pass closed bars only)
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view as sw

TFS = ("5m", "15m", "1h", "4h", "1d")
LOOK = {"5m": 12, "15m": 16, "1h": 24, "4h": 42, "1d": 7}
VOL_N, RSI_N, EMA_N, STRUCT_N, HIGH_N, FLOW_N, VOLZ_HIST = 96, 14, 20, 4, 24, 4, 24
COLUMNS = ("ret", "move_z", "rsi", "ema_gap", "ema_slope", "lower_high", "higher_low", "off_high", "vol_z", "ti")
MIN_BARS = 2 * STRUCT_N + HIGH_N      # fewer bars: every column NaN


def _shift_max(x: np.ndarray, n: int) -> np.ndarray:
    """out[i] = max(x[i-n+1 .. i]) (NaN before n bars)."""
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = sw(x, n).max(1)
    return out


def _shift_min(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = sw(x, n).min(1)
    return out


def series(bars: list[list[float]] | np.ndarray, tf: str) -> dict[str, np.ndarray]:
    a = np.asarray(bars, dtype=float)
    n = len(a)
    if n < MIN_BARS:
        return {k: np.full(n, np.nan) for k in COLUMNS}
    h, lo, c, v = a[:, 2], a[:, 3], a[:, 4], a[:, 5]
    k = LOOK[tf]
    out: dict[str, np.ndarray] = {}
    ret = np.full(n, np.nan)
    ret[k:] = c[k:] / c[:-k] - 1
    out["ret"] = ret
    lr = np.diff(np.log(c), prepend=np.nan)            # lr[i] = log return of bar i
    mz = np.full(n, np.nan)
    if n > k + VOL_N:
        move = np.log(c[k:]) - np.log(c[:-k])             # move ending at bar i = k..n-1
        sd = np.full(n, np.nan)
        sd[VOL_N:] = sw(lr[1:], VOL_N).std(1)             # sd[j] = std(lr[j-95 .. j]) for j >= VOL_N
        i = np.arange(k + VOL_N, n)
        vol = sd[i - k]                                   # the 96 returns ending where the move starts
        with np.errstate(divide="ignore", invalid="ignore"):
            mz[i] = np.where(vol > 0, move[i - k] / (vol * np.sqrt(k)), np.nan)
    out["move_z"] = mz
    cs = pd.Series(c)
    d = cs.diff()
    up = d.clip(lower=0).ewm(alpha=1 / RSI_N, adjust=False).mean()
    dn = (-d).clip(lower=0).ewm(alpha=1 / RSI_N, adjust=False).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = np.where(dn.to_numpy() > 0, 100 - 100 / (1 + up.to_numpy() / dn.to_numpy()),
                       np.where(up.to_numpy() > 0, 100.0, 50.0))
    rsi[:RSI_N] = np.nan
    out["rsi"] = rsi
    ema = cs.ewm(span=EMA_N, adjust=False).mean().to_numpy()
    out["ema_gap"] = c / ema - 1
    slope = np.full(n, np.nan)
    slope[STRUCT_N:] = ema[STRUCT_N:] / ema[:-STRUCT_N] - 1
    out["ema_slope"] = slope
    hi4, lo4 = _shift_max(h, STRUCT_N), _shift_min(lo, STRUCT_N)
    lh, hl = np.zeros(n), np.zeros(n)
    lh[2 * STRUCT_N - 1:] = hi4[2 * STRUCT_N - 1:] < hi4[STRUCT_N - 1:-STRUCT_N]
    hl[2 * STRUCT_N - 1:] = lo4[2 * STRUCT_N - 1:] > lo4[STRUCT_N - 1:-STRUCT_N]
    out["lower_high"], out["higher_low"] = lh, hl
    out["off_high"] = c / _shift_max(h, HIGH_N) - 1
    out["vol_z"] = _vol_z(v)
    out["ti"] = _ti(a, v) if a.shape[1] > 6 else np.full(n, np.nan)
    return out


def _vol_z(v: np.ndarray) -> np.ndarray:
    n = len(v)
    out = np.full(n, np.nan)
    cs = np.concatenate([[0.0], np.cumsum(v)])
    s4 = cs[FLOW_N:] - cs[:-FLOW_N]                   # s4[i] = volume of bars i .. i+3
    need = FLOW_N * (VOLZ_HIST + 1)
    if n < need:
        return out
    i = np.arange(need - 1, n)                         # bar i: last block = bars i-3 .. i
    last = s4[i - FLOW_N + 1]
    prior = np.stack([s4[i - FLOW_N + 1 - FLOW_N * j] for j in range(1, VOLZ_HIST + 1)], 1)
    med = np.median(prior, 1)
    mad = np.median(np.abs(prior - med[:, None]), 1) * 1.4826
    with np.errstate(divide="ignore", invalid="ignore"):
        out[i] = np.where(mad > 0, (last - med) / mad, np.nan)
    return out


def _ti(a: np.ndarray, v: np.ndarray) -> np.ndarray:
    tb = a[:, 6]
    cv, ct = np.concatenate([[0.0], np.cumsum(v)]), np.concatenate([[0.0], np.cumsum(tb)])
    out = np.full(len(v), np.nan)
    if len(v) >= FLOW_N:
        vol = cv[FLOW_N:] - cv[:-FLOW_N]
        buy = ct[FLOW_N:] - ct[:-FLOW_N]
        with np.errstate(divide="ignore", invalid="ignore"):
            out[FLOW_N - 1:] = np.where(vol > 0, (2 * buy - vol) / vol, np.nan)
    return out


def latest(bars_by_tf: dict[str, list[list[float]] | None]) -> dict[str, dict[str, Any]]:
    """The last bar's state for each timeframe given (pass closed bars only; None / short -> NaNs omitted)."""
    out: dict[str, dict[str, Any]] = {}
    for tf, bars in bars_by_tf.items():
        if not bars or tf not in LOOK:
            continue
        s = series(bars, tf)
        out[tf] = {k: (None if np.isnan(s[k][-1]) else round(float(s[k][-1]), 5)) for k in COLUMNS}
    return out
