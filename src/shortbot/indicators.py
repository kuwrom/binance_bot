"""Dependency-free indicators on OHLCV rows: [ts, open, high, low, close, volume]."""

from __future__ import annotations

import math

Candle = list[float]


def closes(c: list[Candle]) -> list[float]:
    return [x[4] for x in c]


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values: list[float], period: int = 14) -> float | None:
    """Wilder RSI of the last value."""
    if len(values) <= period:
        return None
    gains = [max(values[i] - values[i - 1], 0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0) for i in range(1, len(values))]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 50.0 if avg_g == 0 else 100.0  # flat series: neutral, not overbought
    return 100 - 100 / (1 + avg_g / avg_l)


def atr(c: list[Candle], period: int = 14) -> float | None:
    if len(c) <= period:
        return None
    trs = [max(c[i][2] - c[i][3], abs(c[i][2] - c[i - 1][4]), abs(c[i][3] - c[i - 1][4]))
           for i in range(1, len(c))]
    val = sum(trs[:period]) / period
    for tr in trs[period:]:
        val = (val * (period - 1) + tr) / period
    return val


def vwap_bands(c: list[Candle]) -> dict[str, float] | None:
    """Volume-weighted average price over the window plus the last close's z-score vs it."""
    vol = sum(x[5] for x in c)
    if not c or vol == 0:
        return None
    typical = [(x[2] + x[3] + x[4]) / 3 for x in c]
    vwap = sum(t * x[5] for t, x in zip(typical, c)) / vol
    var = sum(x[5] * (t - vwap) ** 2 for t, x in zip(typical, c)) / vol
    sd = math.sqrt(var)
    last = c[-1][4]
    return {"vwap": vwap, "sd": sd, "z": (last - vwap) / sd if sd else 0.0,
            "pct_above": (last - vwap) / vwap * 100}


def relative_volume(c: list[Candle], lookback: int = 20) -> float | None:
    """Last candle's volume divided by the mean of the prior `lookback` candles."""
    if len(c) < lookback + 1:
        return None
    base = sum(x[5] for x in c[-lookback - 1:-1]) / lookback
    return c[-1][5] / base if base else None


def swing_high(c: list[Candle], lookback: int = 24) -> float:
    return max(x[2] for x in c[-lookback:])


def pct_change(a: float, b: float) -> float:
    return (b - a) / a * 100 if a else 0.0


def lower_high(c: list[Candle], window: int = 6) -> bool:
    """True if the highest high of the last `window` candles is below the previous window's high."""
    if len(c) < window * 2:
        return False
    return max(x[2] for x in c[-window:]) < max(x[2] for x in c[-2 * window:-window])
