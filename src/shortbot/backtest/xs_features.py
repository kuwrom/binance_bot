"""Daily technical indicators for the cross-sectional factor studies (task #19, backtest/xs_factors.py).

CTREND (Fieberg, Liedtke, Metko, Zaremba; JFQA 2025) combines 28 daily price/volume indicators. This is the
pre-registered subset we compute from 1d klines (21 features), each known at the day's close:
- price / SMA(n) - 1 for n in 3, 5, 10, 20, 50, 100, 200;
- volume / SMA_volume(n) - 1 for n in 5, 20, 50;
- MACD(12, 26) / price and (MACD - signal(9)) / price;
- RSI(14);
- stochastic %K(14) and %D(3);
- Bollinger %B(20, 2) and bandwidth;
- 1, 2 and 4-week returns;
- realised volatility (20d).
Missing history gives None; the factor layer ranks within the cross-section and maps None to the neutral rank."""

from __future__ import annotations

import math
from typing import Any

SMA_N = (3, 5, 10, 20, 50, 100, 200)
VOL_N = (5, 20, 50)
FEATURES = ([f"sma{n}" for n in SMA_N] + [f"vsma{n}" for n in VOL_N]
            + ["macd", "macd_sig", "rsi14", "stoch_k", "stoch_d", "boll_b", "boll_w", "ret7", "ret14", "ret28", "rv20"])


def _prefix(xs: list[float]) -> list[float]:
    out = [0.0]
    for x in xs:
        out.append(out[-1] + x)
    return out


def _sma(cs: list[float], i: int, n: int) -> float | None:
    """SMA of the n values ending at i, from the prefix sums cs (O(1))."""
    return (cs[i + 1] - cs[i + 1 - n]) / n if i >= n - 1 else None


def _ema_series(xs: list[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out: list[float] = []
    for x in xs:
        out.append(x if not out else out[-1] + k * (x - out[-1]))
    return out


def indicators(bars: list[tuple]) -> list[dict[str, float | None]]:
    """bars = [(ts, o, h, l, c, qv), ...] sorted -> one feature dict per bar (index-aligned)."""
    c = [b[4] for b in bars]
    h = [b[2] for b in bars]
    lo = [b[3] for b in bars]
    v = [b[5] for b in bars]
    cc, cv = _prefix(c), _prefix(v)
    ema12, ema26 = _ema_series(c, 12), _ema_series(c, 26)
    macd = [a - b for a, b in zip(ema12, ema26, strict=True)]
    sig = _ema_series(macd, 9)
    gains = [0.0] + [max(0.0, c[i] - c[i - 1]) for i in range(1, len(c))]
    losses = [0.0] + [max(0.0, c[i - 1] - c[i]) for i in range(1, len(c))]
    out: list[dict[str, float | None]] = []
    ks: list[float | None] = []
    for i in range(len(bars)):
        f: dict[str, float | None] = {k: None for k in FEATURES}
        for n in SMA_N:
            s = _sma(cc, i, n)
            f[f"sma{n}"] = c[i] / s - 1 if s else None
        for n in VOL_N:
            s = _sma(cv, i, n)
            f[f"vsma{n}"] = v[i] / s - 1 if s else None
        if i >= 34:                               # 26 + 9 bars of EMA warm-up
            f["macd"], f["macd_sig"] = macd[i] / c[i], (macd[i] - sig[i]) / c[i]
        if i >= 14:
            g, l_ = sum(gains[i - 13:i + 1]) / 14, sum(losses[i - 13:i + 1]) / 14
            f["rsi14"] = 100.0 if l_ == 0 else 100 - 100 / (1 + g / l_)
        k = None
        if i >= 13:
            hh, ll = max(h[i - 13:i + 1]), min(lo[i - 13:i + 1])
            k = (c[i] - ll) / (hh - ll) * 100 if hh > ll else 50.0
        ks.append(k)
        f["stoch_k"] = k
        last3 = [x for x in ks[-3:] if x is not None]
        if i >= 15 and len(last3) == 3:   # (ks has a value per bar: the last three, all known)
            f["stoch_d"] = sum(last3) / 3
        if i >= 19:
            w = c[i - 19:i + 1]
            mu = sum(w) / 20
            sd = math.sqrt(sum((x - mu) ** 2 for x in w) / 20)
            f["boll_b"] = (c[i] - (mu - 2 * sd)) / (4 * sd) if sd else 0.5
            f["boll_w"] = 4 * sd / mu if mu else None
            rets = [c[j] / c[j - 1] - 1 for j in range(i - 18, i + 1)]
            m = sum(rets) / len(rets)
            f["rv20"] = math.sqrt(sum((r - m) ** 2 for r in rets) / len(rets))
        for n, key in ((7, "ret7"), (14, "ret14"), (28, "ret28")):
            f[key] = c[i] / c[i - n] - 1 if i >= n else None
        out.append(f)
    return out


def rank_normalise(values: dict[str, float | None]) -> dict[str, float]:
    """Cross-sectional ranks mapped to [-0.5, 0.5]; None -> 0 (neutral). Ties get the average rank."""
    known = sorted((v, s) for s, v in values.items() if v is not None and not math.isnan(v))
    out = {s: 0.0 for s in values}
    n = len(known)
    if n < 2:
        return out
    i = 0
    while i < n:
        j = i
        while j + 1 < n and known[j + 1][0] == known[i][0]:
            j += 1
        r = (i + j) / 2 / (n - 1) - 0.5
        for k in range(i, j + 1):
            out[known[k][1]] = r
        i = j + 1
    return out


def by_day(data: dict[str, Any]) -> dict[str, dict[int, dict[str, float | None]]]:
    """{symbol: {day_ts: features}} for every coin in histdaily.load() output."""
    return {s: {b[0]: f for b, f in zip(rows, indicators(rows), strict=True)} for s, rows in data["bars"].items()}
