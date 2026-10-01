"""Manipulation-resistant price features (docs/SOCIAL_AND_ROBUST_PRICES.md §1). Judge a level by where TIME and
VOLUME actually traded, not by the last print. Bars are flow bars [ts, o, h, l, c, vol, taker_buy_vol]."""

from __future__ import annotations

import statistics


def bar_vwap(bars: list[list[float]]) -> float | None:
    vol = sum(b[5] for b in bars)
    if not vol:
        return None
    return sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in bars) / vol


def acceptance(bars_1m: list[list[float]], level: float) -> dict[str, float | None]:
    """Share of 1m bars (time) and of volume that traded ABOVE `level`."""
    if not bars_1m:
        return {"time": None, "volume": None}
    above = [b for b in bars_1m if b[4] > level]
    vol = sum(b[5] for b in bars_1m)
    return {"time": len(above) / len(bars_1m),
            "volume": (sum(b[5] for b in above) / vol) if vol else None}


def close_dev(bars_1m: list[list[float]], atr: float) -> float | None:
    """(close − VWAP of the same bars) / ATR: how far the print sits from where volume traded."""
    vw = bar_vwap(bars_1m)
    if vw is None or not atr:
        return None
    return (bars_1m[-1][4] - vw) / atr


def tail_vol_share(bars_1m: list[list[float]], tail: int = 2) -> float | None:
    vol = sum(b[5] for b in bars_1m)
    return sum(b[5] for b in bars_1m[-tail:]) / vol if vol and len(bars_1m) > tail else None


def painted_close(bars_1m: list[list[float]], atr: float, tail: int = 2) -> dict:
    """A close far from the bar's VWAP (|dev| > 0.5 ATR), moved there in the last `tail` minutes on no more
    than its fair share of volume: a print that paints the chart without real participation."""
    dev = close_dev(bars_1m, atr)
    share = tail_vol_share(bars_1m, tail)
    if dev is None or share is None or len(bars_1m) <= tail:
        return {"painted": False, "close_dev": dev, "tail_vol_share": share}
    tail_move = bars_1m[-1][4] - bars_1m[-tail - 1][4]
    fair = tail / len(bars_1m)
    painted = abs(dev) > 0.5 and tail_move * dev > 0 and share <= fair * 1.2
    return {"painted": painted, "close_dev": round(dev, 3), "tail_vol_share": round(share, 3)}


def mark_gap_z(last_closes: list[float], mark_closes: list[float]) -> float | None:
    """z-score of the latest (last − mark)/mark gap vs its own history: a spike = a last-price print the index
    didn't confirm."""
    n = min(len(last_closes), len(mark_closes))
    if n < 12:
        return None
    gaps = [(l - m) / m for l, m in zip(last_closes[-n:], mark_closes[-n:]) if m]
    hist = gaps[:-1]
    sd = statistics.pstdev(hist)
    return (gaps[-1] - statistics.mean(hist)) / sd if sd else None


def close_engineering_score(bars_5m: list[list[float]]) -> float | None:
    """corr(return of the last 5m of each 15m block, return of the next 5m). Strongly negative = this coin's
    bar closes tend to be pushed and then reversed (Comerton-Forde & Putniņš style)."""
    xs, ys = [], []
    for i in range(2, len(bars_5m) - 1, 3):
        a, b = bars_5m[i], bars_5m[i + 1]
        if a[1] and b[1]:
            xs.append((a[4] - a[1]) / a[1])
            ys.append((b[4] - b[1]) / b[1])
    if len(xs) < 10:
        return None
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return None


def sweep_rejection(bars_1m: list[list[float]], level: float, mark_high: float | None,
                    max_time: float = 0.20, max_vol: float = 0.25) -> dict:
    """Robust failed-sweep test: the level was pierced (on last AND, if given, mark), but price did NOT
    accept above it: < 20% of time and < 25% of volume above, and the VWAP of the window is below the level."""
    acc = acceptance(bars_1m, level)
    vw = bar_vwap(bars_1m)
    pierced_last = any(b[2] > level for b in bars_1m)
    pierced_mark = mark_high is None or mark_high > level
    rejected = (pierced_last and pierced_mark and acc["time"] is not None and acc["time"] < max_time
                and (acc["volume"] or 0) < max_vol and vw is not None and vw < level)
    return {"rejected": rejected, "pierced": pierced_last and pierced_mark, "acceptance_time": acc["time"],
            "acceptance_volume": acc["volume"], "window_vwap": vw}
