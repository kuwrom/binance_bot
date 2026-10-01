"""Pure order-flow / tape computations (docs/TAPE.md). Candles here are flow candles:
[ts, o, h, l, c, vol, taker_buy_vol]. No I/O; unit-tested; used by signals/tape.py and charts."""

from __future__ import annotations

import math
import statistics


def taker_imbalance(bars: list[list[float]]) -> float | None:
    """TI = (Vbuy − Vsell)/(Vbuy + Vsell) over the bars."""
    vol = sum(b[5] for b in bars)
    if not vol:
        return None
    buy = sum(b[6] for b in bars)
    return (2 * buy - vol) / vol


def ti_zscore(bars: list[list[float]], window: int) -> tuple[float | None, float | None]:
    """(TI of the last `window` bars, its z-score vs all earlier non-overlapping windows)."""
    if len(bars) < window * 4:
        return taker_imbalance(bars[-window:]), None
    chunks = [bars[i:i + window] for i in range(len(bars) - window * (len(bars) // window), len(bars), window)]
    tis = [t for t in (taker_imbalance(c) for c in chunks) if t is not None]
    now, hist = tis[-1], tis[:-1]
    sd = statistics.pstdev(hist) if len(hist) > 1 else 0
    return now, ((now - statistics.mean(hist)) / sd if sd else None)


def cvd(bars: list[list[float]]) -> list[float]:
    """Cumulative volume delta: running sum of (taker buy − taker sell)."""
    out, s = [], 0.0
    for b in bars:
        s += 2 * b[6] - b[5]
        out.append(s)
    return out


def cvd_bearish_divergence(bars: list[list[float]], half: int = 8) -> bool:
    """Price makes a higher high in the recent half while CVD makes a lower high (buyers exhausting)."""
    if len(bars) < half * 2:
        return False
    c = cvd(bars[-half * 2:])
    early, late = bars[-half * 2:-half], bars[-half:]
    return max(b[2] for b in late) > max(b[2] for b in early) and max(c[half:]) < max(c[:half])


def round_step(price: float) -> float:
    """Psychological step: half a unit of the second significant digit (1.27 -> 0.05, 259 -> 5, 84000 -> 500)."""
    if price <= 0:
        return 0.0
    return 5 * 10 ** (math.floor(math.log10(price)) - 2)


GRID_CLEAR_PCT = 0.3   # never leave a stop within 0.3% beyond a grid level (the crowd's stop zone, Osler 2003)
GRID_UNDER_PCT = 0.2   # ...nor within 0.2% under one (price is pulled to round numbers)


def round_zone(price: float, min_gap_pct: float = 0.0) -> float | None:
    """The round level whose stop zone holds `price` (0.2% under it to 0.3% past it), else None. `min_gap_pct`
    skips grids denser than that (0.884 has levels every 0.57%: a 0.5% zone there would cover everything), moving
    up to the tier-2 (2 steps) or tier-3 (10 steps) grid."""
    step = round_step(price)
    if not step:
        return None
    step = next((step * m for m in (1, 2, 10) if step * m / price * 100 >= min_gap_pct), step * 10)
    k = math.floor(price / step + 1e-9)
    below, above = k * step, (k + 1) * step
    if price <= below * (1 + GRID_CLEAR_PCT / 100):
        return below
    if price >= above * (1 - GRID_UNDER_PCT / 100):
        return above
    return None


def clear_above_round(stop: float, min_gap_pct: float = 0.0) -> float:
    """Push a short's stop up and out of any round-number zone (past the crowd's stop cluster)."""
    for _ in range(3):
        lvl = round_zone(stop, min_gap_pct)
        if lvl is None:
            break
        stop = lvl * (1 + GRID_CLEAR_PCT / 100) + round_step(stop) * 0.1
    return stop


def clear_below_round(stop: float, min_gap_pct: float = 0.0) -> float:
    """Pull a short's stop down under any round-number zone (tighter; never tagged by a test of the round)."""
    for _ in range(3):
        lvl = round_zone(stop, min_gap_pct)
        if lvl is None:
            break
        stop = lvl * (1 - (GRID_UNDER_PCT + 0.05) / 100)
    return stop


def next_round_above(price: float) -> float:
    step = round_step(price)
    return math.floor(price / step + 1) * step if step else price


def sweep_state(bars: list[list[float]], lookback: int = 32, confirm: int = 3, buffer: float = 0.001) -> dict:
    """Classify the last `confirm` bars against liquidity levels L = prior `lookback`-bar high and the round number
    just above it:
    failed_sweep - a bar pierced L (> L·(1+buffer)) and the latest close is back below L (bearish trigger)
    accepted     - the last 2 closes are above L (breakout accepted: veto)"""
    if len(bars) < lookback + confirm:
        return {"state": None}
    prior = bars[-(lookback + confirm):-confirm]
    recent = bars[-confirm:]
    high = max(b[2] for b in prior)
    out: dict = {"state": "none", "level": high, "round_level": next_round_above(high)}
    for name, lvl in (("prior_high", high), ("round", out["round_level"])):
        pierced = any(b[2] > lvl * (1 + buffer) for b in recent)
        if len(recent) >= 2 and all(b[4] > lvl for b in recent[-2:]):
            return {**out, "state": "accepted", "level_type": name, "swept": lvl}
        if pierced and recent[-1][4] < lvl:
            wick = max(b[2] for b in recent)
            return {**out, "state": "failed_sweep", "level_type": name, "swept": lvl, "sweep_high": wick}
    return out


def pump_state(bars_1h: list[list[float]], base_h: int = 12, price_pct: float = 5.0, vol_mult: float = 4.0) -> dict:
    """Kamps & Kleinberg (2018) style on 1h bars: a pump bar = return > price_pct and volume > vol_mult × the mean
    of the prior base_h bars. Reports whether the pump is in progress (last bar) or decaying (earlier, price now
    below the pump bars' VWAP)."""
    if len(bars_1h) < base_h + 1:
        return {"state": None}
    pumps = []
    for i in range(base_h, len(bars_1h)):
        b, base = bars_1h[i], bars_1h[i - base_h:i]
        avg_vol = sum(x[5] for x in base) / base_h
        ret = (b[4] - b[1]) / b[1] * 100 if b[1] else 0
        if ret > price_pct and avg_vol and b[5] > vol_mult * avg_vol:
            pumps.append(i)
    if not pumps:
        return {"state": "none"}
    last_i = pumps[-1]
    if last_i == len(bars_1h) - 1:
        return {"state": "in_progress", "bars_ago": 0}
    seg = bars_1h[pumps[0]:last_i + 1]
    vw = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in seg) / (sum(b[5] for b in seg) or 1)
    now = bars_1h[-1][4]
    return {"state": "decaying" if now < vw else "holding", "bars_ago": len(bars_1h) - 1 - last_i,
            "pump_vwap": vw}
