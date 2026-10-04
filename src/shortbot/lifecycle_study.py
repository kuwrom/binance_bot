"""Listing-lifecycle study (docs/LIFECYCLE_STUDY.md): how long new Binance USDT perps take to fall, how far, and
what predicts it. Pure functions over one coin's daily series (day 0 = listing day, index k = k days after
listing), then per-coin metrics. Data: lifecycle_data.py (data/lifecycle.db); tables: lifecycle_report.py.

Conventions: prices are closes unless stated; a short's return is 1 − exit/entry (price only: no fees/funding);
MAE is the worst high after entry vs entry (squeeze risk). A delisted coin's last close is carried forward
(it settled there), so horizons past delisting still count - no survivorship bias from dropping the dead."""

from __future__ import annotations

import math
import statistics
from typing import Any

HORIZONS = (7, 14, 30, 60, 90, 180)
DRAWDOWNS = (0.3, 0.5, 0.7, 0.9)
ENTRIES = (0, 1, 3, 7, 14, 30, 60)          # enter short at that day's close
HOLDS = (7, 14, 30, 60, 90)


# ------------------------------------------------------------------ small stats
def quantile(xs: list[float], q: float) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    i = (len(xs) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(xs: list[float | None], ys: list[float | None]) -> tuple[float | None, int]:
    pairs = [(x, y) for x, y in zip(xs, ys, strict=True) if x is not None and y is not None]
    if len(pairs) < 8:
        return None, len(pairs)
    rx, ry = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    try:
        return round(statistics.correlation(rx, ry), 3), len(pairs)
    except statistics.StatisticsError:
        return None, len(pairs)


def km_median(durations: list[tuple[float, bool]]) -> float | None:
    """Kaplan-Meier median time-to-event. durations: (days, event_happened); False = censored at `days`
    (coin too young, or never got there). None when fewer than half ever get there."""
    at_risk, s = len(durations), 1.0
    for t in sorted({d for d, _ in durations}):
        events = sum(1 for d, e in durations if d == t and e)
        if events:
            s *= 1 - events / at_risk
            if s <= 0.5:
                return t
        at_risk -= sum(1 for d, _ in durations if d == t)
    return None


def km_by(durations: list[tuple[float, bool]], h: float) -> float | None:
    """Kaplan-Meier share of coins that had the event by day h."""
    at_risk, s = len(durations), 1.0
    if not at_risk:
        return None
    for t in sorted({d for d, _ in durations if d <= h}):
        events = sum(1 for d, e in durations if d == t and e)
        if events and at_risk:
            s *= 1 - events / at_risk
        at_risk -= sum(1 for d, _ in durations if d == t)
    return 1 - s


# ------------------------------------------------------------------ one series
def value_at(closes: list[float], k: int, complete: bool) -> float | None:
    """Close k days after listing; after delisting (complete=True) the last close is carried forward."""
    if k < len(closes):
        return closes[k]
    return closes[-1] if complete and closes else None


def days_to_drawdown(closes: list[float], level: float, start: int = 0) -> int | None:
    """First day whose close is ≥ level below the running peak close (the listing-period high so far)."""
    peak = -math.inf
    for i in range(start, len(closes)):
        peak = max(peak, closes[i])
        if closes[i] <= peak * (1 - level):
            return i
    return None


def days_below(closes: list[float], anchor: float, level: float) -> int | None:
    """First day whose close is ≥ level below a fixed anchor (e.g. the listing open)."""
    return next((i for i, c in enumerate(closes) if c <= anchor * (1 - level)), None)


def pump_window(opens: list[float], highs: list[float]) -> dict[str, float | int]:
    """Days from listing to the all-time high (wick) and the ATH's size vs the first open."""
    i = max(range(len(highs)), key=lambda k: highs[k])
    return {"ath_day": i, "ath_vs_open": highs[i] / opens[0] - 1}


def dead_cat_bounces(closes: list[float], start: int, rally: float = 0.5) -> int:
    """+rally closes off the lowest close since `start` (the ATH) or since the previous counted bounce."""
    n, trough = 0, math.inf
    for c in closes[start:]:
        trough = min(trough, c)
        if c >= trough * (1 + rally):
            n, trough = n + 1, c
    return n


def volume_ratio(qv: list[float], day: int, width: int = 3) -> float | None:
    """Mean quote volume around `day` (±width) vs the first week."""
    if len(qv) <= day + width or len(qv) < 7:
        return None
    w1 = statistics.fmean(qv[:7])
    return statistics.fmean(qv[day - width: day + width + 1]) / w1 if w1 else None


def short_trade(highs: list[float], closes: list[float], entry_day: int, hold: int, complete: bool,
                entry_price: float | None = None) -> dict[str, float] | None:
    """Short at entry_day's close (or entry_price), exit at the close `hold` days later.
    ret = 1 − exit/entry; mae = worst later high vs entry − 1 (how far it ran against the short first)."""
    if entry_day >= len(closes):
        return None
    exit_day = entry_day + hold
    if exit_day >= len(closes) and not complete:
        return None
    entry = entry_price or closes[entry_day]
    last = min(exit_day, len(closes) - 1)
    exit_px = closes[last]
    window = highs[entry_day + 1: last + 1]
    return {"ret": 1 - exit_px / entry, "mae": max(max(window, default=entry) / entry - 1, 0.0)}


def first_close_below(closes: list[float], level: float, start: int) -> int | None:
    return next((i for i in range(start, len(closes)) if closes[i] < level), None)


# ------------------------------------------------------------------ one coin
def coin_metrics(bars: list[tuple], btc: dict[int, tuple[float, float]], complete: bool) -> dict[str, Any]:
    """bars: (ts, o, h, l, c, qv) daily, oldest first, day 0 = listing day. btc: ts -> (open, close)."""
    ts = [b[0] for b in bars]
    o, h, c, qv = [b[1] for b in bars], [b[2] for b in bars], [b[4] for b in bars], [b[5] for b in bars]
    open0, w1_high = o[0], max(h[:7])
    w1_day = max(range(min(7, len(h))), key=lambda k: h[k])
    pw = pump_window(o, h)
    m: dict[str, Any] = {"age": len(c), "complete": complete, **pw, "w1_high_vs_open": w1_high / open0 - 1,
                         "w1_qv": sum(qv[:7]), "bounces": dead_cat_bounces(c, pw["ath_day"])}
    btc_o = btc.get(ts[0], (None,))[0]
    for hz in HORIZONS:
        px = value_at(c, hz, complete)
        m[f"r_open_{hz}"] = px / open0 - 1 if px else None
        m[f"r_w1h_{hz}"] = px / w1_high - 1 if px and hz >= 7 else None
        k = pw["ath_day"] + hz
        ath_px = value_at(c, k, complete)
        m[f"r_ath_{hz}"] = ath_px / h[pw["ath_day"]] - 1 if ath_px else None
        b = btc.get(ts[0] + hz * 86_400_000) if hz < len(c) else None
        m[f"btc_{hz}"] = b[1] / btc_o - 1 if b and btc_o else None
        m[f"x_open_{hz}"] = (m[f"r_open_{hz}"] - m[f"btc_{hz}"]
                             if m[f"r_open_{hz}"] is not None and m[f"btc_{hz}"] is not None else None)
    for lv in DRAWDOWNS:
        d_peak, d_open = days_to_drawdown(c, lv), days_below(c, open0, lv)
        # (days, event) for Kaplan-Meier: censored at the coin's age when it never got there
        m[f"dd_peak_{int(lv * 100)}"] = (d_peak, True) if d_peak is not None else (len(c) - 1, False)
        m[f"dd_open_{int(lv * 100)}"] = (d_open, True) if d_open is not None else (len(c) - 1, False)
    for day in (30, 90):
        m[f"vol_{day}"] = volume_ratio(qv, day)
    m["trades"] = _trades(h, c, complete, w1_high, w1_day, pw["ath_day"])
    return m


def _trades(h, c, complete, w1_high, w1_day, ath_day) -> dict[str, dict[int, dict | None]]:
    """Short entries: fixed day closes, two tradable triggers, and two hindsight anchors (upper bounds)."""
    out: dict[str, dict[int, dict | None]] = {}
    for e in ENTRIES:
        out[f"d{e}"] = {n: short_trade(h, c, e, n, complete) for n in HOLDS}
    w1_low_break = first_close_below(c, min(c[:7]) if len(c) >= 7 else -1, 7)
    peak_off = days_to_drawdown(c, 0.3, 0)
    for name, day in (("w1_low_break", w1_low_break), ("peak_-30%", peak_off)):
        out[name] = {n: short_trade(h, c, day, n, complete) if day is not None else None for n in HOLDS}
    out["w1_high*"] = {n: short_trade(h, c, w1_day, n, complete, entry_price=w1_high) for n in HOLDS}
    out["ath*"] = {n: short_trade(h, c, ath_day, n, complete, entry_price=h[ath_day]) for n in HOLDS}
    return out


def trade_stats(trades: list[dict], stop: float = 0.3) -> dict[str, Any]:
    """Aggregate short trades: median return, win rate, MAE distribution, and the mean return if every trade
    had a hard stop `stop` above entry (a squeeze that runs +stop first is stopped out at −stop)."""
    ts = [t for t in trades if t]
    if not ts:
        return {"n": 0}
    rets, maes = [t["ret"] for t in ts], [t["mae"] for t in ts]
    stopped = [-stop if t["mae"] >= stop else t["ret"] for t in ts]
    wide = [-0.5 if t["mae"] >= 0.5 else t["ret"] for t in ts]
    return {"mean": statistics.fmean(rets), "mean_stop50": statistics.fmean(wide),"n": len(ts), "med_ret": statistics.median(rets), "win": sum(r > 0 for r in rets) / len(ts),
            "med_mae": statistics.median(maes), "p90_mae": quantile(maes, 0.9),
            "mae50": sum(m >= 0.5 for m in maes) / len(ts), "mae100": sum(m >= 1.0 for m in maes) / len(ts),
            "mean_stop": statistics.fmean(stopped)}


def consistent(groups: dict[str, list[float]], min_n: int = 5) -> tuple[int, int]:
    """(groups whose median has the pooled median's sign, groups with ≥ min_n) - sign stability by month."""
    pooled = [x for g in groups.values() for x in g if x is not None]
    if not pooled:
        return 0, 0
    sign = statistics.median(pooled) > 0
    usable: list[float] = []
    for g in groups.values():
        v = [x for x in g if x is not None]
        if len(v) >= min_n:
            usable.append(statistics.median(v))
    return sum((u > 0) == sign for u in usable), len(usable)
