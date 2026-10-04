"""Positioning cards from the strategy library (task #16): who is adding risk (OI vs price vs funding), retail vs
top-trader long/short ratios, taker flow. Uses the hourly OI / long-short / taker history in data/hist.db (from the
data.binance.vision daily `metrics` archive). Research only; run with `run --days60 --flow`.

Grids fixed before any result was seen (10 trials). Exits are the short book's defaults unless stated.

- long_trap_short (lib.oi_price_funding_regime, "long trap"): OI up >= `oi_up` over 24h while price is down >= 3%,
  with the last 2 funding prints > 0 (longs averaging down, still paying). Short at the close of a red bar; stop =
  4h high + 1 ATR; targets 1R / 2R; 24h time stop.
- crowded_unwind_short (lib.crowded_long_unwind_short): the last 3 funding prints >= 0.05% per 8h (normalised by the
  interval), OI up >= `oi7d` over 7 days, a 4h lower high; enter on a close below the prior 4 bars' low. Stop = 8h
  high + 0.5 ATR; targets 1R / 2R; 72h time stop.
- ls_contrarian_short (lib.ls_ratio_taker_contrarian, short side): the global account long/short ratio is >= `z`
  sigma above its 7-day mean (retail long), the top-trader position ratio is below its level 24h ago, and the taker
  buy/sell ratio is <= 0.9 for 3 bars. Stop = 4h high + 1 ATR; targets 1R / 2R; 24h time stop.
- squeeze_fuel_long (lib.oi_price_funding_regime, "squeeze fuel"): OI up >= `oi_up` over 24h, price down >= 3%, and
  the last 2 funding prints < 0 (late shorts piling in). Long on the first close above the prior 12 bars' high within
  12 bars. Stop = 24h low - 0.5 ATR; targets 1.5R / 3R; 48h time stop.
- ls_contrarian_long (lib.ls_ratio_taker_contrarian, long side): the mirror. Global ratio <= `z` sigma (retail short),
  top-trader ratio above its level 24h ago, taker ratio >= 1.1 for 3 bars, close above the 4h VWAP. Stop = 4h low -
  1 ATR; targets 1R / 2R; 24h time stop.
One signal per coin per 24h for every strategy."""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Iterable

from .features import Coin, Market, _extra_at
from .sim import H, Plan
from .strategies import Trial, atr_abs, grid, plan, r_targets, tradable
from .strategies_long import long_plan, low

Z_WINDOW = 168


def _prints(coin: Coin, i: int, k: int) -> list[tuple[int, float]]:
    """The last k funding prints known at bar i's close, oldest first."""
    times = [t for t, _ in coin.funding]
    j = bisect.bisect_right(times, coin.rows[i]["ts"] + H)
    return [tuple(x) for x in coin.funding[max(0, j - k):j]]


def per8h(coin: Coin, i: int, k: int) -> list[float] | None:
    """The last k prints normalised to an 8h interval (an hourly-funding print of 0.1% = 0.8% per 8h)."""
    p = _prints(coin, i, k + 1)
    if len(p) < k + 1:
        return None
    return [r * 8 * H / max(H, t - p[n][0]) for n, (t, r) in enumerate(p[1:])]


def series(m: Market, coin: Coin, key: str) -> list[float | None]:
    """The extra series aligned to bars (value known at each bar's close), cached per coin."""
    ck = ("flow_series", coin.symbol, key)
    if ck not in m.cache:
        m.cache[ck] = [_extra_at(coin, key, r["ts"] + H) for r in coin.rows]
    return m.cache[ck]


def zscore(xs: list[float | None], i: int, n: int = Z_WINDOW) -> float | None:
    """z of xs[i] against the previous n values (strictly earlier)."""
    cur = xs[i]
    hist = [x for x in xs[max(0, i - n):i] if x is not None]
    if cur is None or len(hist) < n // 2:
        return None
    sd = statistics.pstdev(hist)
    return (cur - statistics.fmean(hist)) / sd if sd else None


def oi_chg(coin: Coin, i: int, back: int) -> float | None:
    if i < back:
        return None
    a, b = coin.rows[i - back].get("oi"), coin.rows[i].get("oi")
    return b / a - 1 if a and b else None


def _short(coin: Coin, i: int, stop: float, time_stop: int) -> Plan | None:
    return plan(coin, i, stop, time_stop, targets=r_targets(coin.rows[i]["c"], stop, ((1.0, 0.5), (2.0, 0.5))))


def long_trap_short(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    r, busy = coin.rows, -1
    for i in range(24, len(r)):
        if i <= busy or not tradable(coin, i) or r[i]["c"] >= r[i]["o"] or r[i]["c"] / r[i - 24]["c"] - 1 > -0.03:
            continue
        o = oi_chg(coin, i, 24)
        if o is None or o < p["oi_up"]:
            continue
        f = per8h(coin, i, 2)
        if f and all(x > 0 for x in f):
            pl = _short(coin, i, coin.high(i - 3, i + 1) + atr_abs(coin, i), 24)
            if pl:
                yield i, pl
                busy = i + 24


def crowded_unwind_short(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    r, busy = coin.rows, -1
    for i in range(168, len(r)):
        if i <= busy or not tradable(coin, i):
            continue
        if not (coin.high(i - 3, i + 1) < coin.high(i - 7, i - 3) and r[i]["c"] < low(coin, i - 4, i)):
            continue
        o = oi_chg(coin, i, 168)
        if o is None or o < p["oi7d"]:
            continue
        f = per8h(coin, i, 3)
        if f and all(x >= 0.0005 for x in f):
            pl = _short(coin, i, coin.high(i - 7, i + 1) + 0.5 * atr_abs(coin, i), 72)
            if pl:
                yield i, pl
                busy = i + 24


def _ls_state(m: Market, coin: Coin, i: int) -> tuple[float | None, float | None, list[float | None]]:
    g, top, tk = series(m, coin, "ls_global"), series(m, coin, "ls_top"), series(m, coin, "taker_ratio")
    now, before = (top[i], top[i - 24]) if i >= 24 else (None, None)
    top_chg = now - before if now is not None and before is not None else None
    return zscore(g, i), top_chg, tk[max(0, i - 2):i + 1]


def ls_contrarian_short(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    r, busy = coin.rows, -1
    for i in range(Z_WINDOW, len(r)):
        if i <= busy or not tradable(coin, i):
            continue
        z, top_chg, tk = _ls_state(m, coin, i)
        if z is not None and z >= p["z"] and top_chg is not None and top_chg < 0 and len(tk) == 3 \
                and all(x is not None and x <= 0.9 for x in tk):
            pl = _short(coin, i, coin.high(i - 3, i + 1) + atr_abs(coin, i), 24)
            if pl:
                yield i, pl
                busy = i + 24


def squeeze_fuel_long(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    r, busy = coin.rows, -1
    for i in range(24, len(r)):
        if i <= busy or r[i]["c"] / r[i - 24]["c"] - 1 > -0.03:
            continue
        o = oi_chg(coin, i, 24)
        if o is None or o < p["oi_up"]:
            continue
        f = per8h(coin, i, 2)
        if not (f and all(x < 0 for x in f)):
            continue
        for j in range(i + 1, min(len(r), i + 13)):
            if r[j]["c"] > coin.high(j - 12, j) and tradable(coin, j):
                pl = long_plan(coin, j, low(coin, j - 23, j + 1) - 0.5 * atr_abs(coin, j), 48)
                if pl:
                    yield j, pl
                busy = j + 24
                break


def ls_contrarian_long(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    r, busy = coin.rows, -1
    for i in range(Z_WINDOW, len(r)):
        if i <= busy or not tradable(coin, i):
            continue
        z, top_chg, tk = _ls_state(m, coin, i)
        if not (z is not None and z <= p["z"] and top_chg is not None and top_chg > 0 and len(tk) == 3
                and all(x is not None and x >= 1.1 for x in tk)):
            continue
        q = sum(x["qv"] for x in r[i - 3:i + 1])
        vwap = sum((x["h"] + x["l"] + x["c"]) / 3 * x["qv"] for x in r[i - 3:i + 1]) / q if q else None
        if vwap and r[i]["c"] > vwap:
            pl = long_plan(coin, i, low(coin, i - 3, i + 1) - atr_abs(coin, i), 24,
                           targets_r=((1.0, 0.5), (2.0, 0.5)))
            if pl:
                yield i, pl
                busy = i + 24


def trials_flow() -> list[Trial]:
    return (grid("long_trap_short", long_trap_short, {"oi_up": [0.05, 0.10]})
            + grid("crowded_unwind_short", crowded_unwind_short, {"oi7d": [0.20, 0.40]})
            + grid("ls_contrarian_short", ls_contrarian_short, {"z": [2.0, 3.0]})
            + grid("squeeze_fuel_long", squeeze_fuel_long, {"oi_up": [0.05, 0.10]})
            + grid("ls_contrarian_long", ls_contrarian_long, {"z": [-2.0, -3.0]}))
