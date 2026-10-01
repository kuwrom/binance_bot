"""The first LONG sleeve (task #18): pre-registered long strategies from the strategy library (docs/strategies/
long_side.md), research only. The desk trades shorts; these run through the same simulator (Plan(side="long")),
matched placebos (mirrored, placebo.placebo_plan) and report as every short strategy.

Grids fixed before any result was seen (12 trials):
- flush_reclaim (lib.long_liq_flush_reclaim_long): a 24h drop >= `drop` with OI down >= 8% over 24h and the last
  funding print <= 0; enter on the first close back above EMA20 within 12h; stop under the 24h low - 0.5 ATR; targets
  1.5R / 3R; 72h time stop. `liquid` = only coins with >= $100M 24h volume (the card says majors).
- oversold_bounce (lib.oversold_bounce_majors): 24h return <= -drop, OI down >= 8%, funding <= 0; enter on the first
  1h higher low that closes green within 12h; stop 24h low - 0.5 ATR; targets at 38.2% and 50% retracement of the 24h
  range; 72h time stop.
- capitulation (lib.capitulation_forced_selling_long): 7-day return <= -drop7d; enter on the first close above the
  prior 24h high; stop 7d low - 0.5 ATR; targets 1.5R / 3R; time stop `time` hours (the card: days, not weeks).
- premium_discount (lib.premium_index_reversion, long side): perp premium z-score vs the prior 168h <= z; enter at
  the close; stop 24h low - 1 ATR; targets 1R / 2R; 24h time stop.
Breakeven at 1R for all (the short book's default)."""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from typing import Any

from .features import Coin, Market
from .sim import Plan
from .strategies import Trial, atr_abs, grid, tradable
from .strategies_v4 import last_funding

OI_DROP = -0.08
LIQUID_VOL = 100e6
CONFIRM_BARS = 12


def long_plan(coin: Coin, i: int, stop: float, time_stop: int, targets_r=((1.5, 0.5), (3.0, 0.5)),
              targets: tuple | None = None, **kw: Any) -> Plan | None:
    """Market long at bar i's close; stop BELOW, targets above (R multiples or absolute prices). meta keeps the
    exits in ATR / R units for the mirrored placebo."""
    ref = coin.rows[i]["c"]
    if stop >= ref or stop <= 0:
        return None
    risk = ref - stop
    tg = targets if targets is not None else tuple((ref + r * risk, f) for r, f in targets_r)
    tg = tuple((px, f) for px, f in tg if px > ref)
    a = atr_abs(coin, i)
    kw.setdefault("be_r", 1.0)
    meta = {"side": "long", "stop_atr": (ref - stop) / a if a else None,
            "targets_r": tuple(((px - ref) / risk, f) for px, f in tg)}
    return Plan(stop=stop, targets=tg, time_stop=time_stop, meta=meta, side="long", **kw)


def low(coin: Coin, a: int, b: int) -> float:
    return min(r["l"] for r in coin.rows[max(0, a):b])


def vol24(coin: Coin, i: int) -> float:
    return sum(r["qv"] for r in coin.rows[max(0, i - 23):i + 1])


def flushed(coin: Coin, i: int, drop: float) -> bool:
    """24h drop >= drop (fraction), OI down >= 8% over 24h, last funding print <= 0 (all known at bar i's close)."""
    r = coin.rows
    if i < 24 or r[i]["c"] / r[i - 24]["c"] - 1 > -drop:
        return False
    oi0, oi1 = r[i - 24].get("oi"), r[i].get("oi")
    if not (oi0 and oi1) or oi1 / oi0 - 1 > OI_DROP:
        return False
    f = last_funding(coin, i)                      # the slow lookup last
    return f is not None and f <= 0


def flush_reclaim(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    r, e, busy = coin.rows, coin.ema20, -1
    for i in range(24, len(r)):
        if i <= busy or not flushed(coin, i, p["drop"]):
            continue
        for j in range(i, min(len(r), i + CONFIRM_BARS + 1)):
            if r[j]["c"] > e[j] and r[j - 1]["c"] <= e[j - 1] and tradable(coin, j):
                if p["liquid"] and vol24(coin, j) < LIQUID_VOL:
                    break
                pl = long_plan(coin, j, low(coin, j - 23, j + 1) - 0.5 * atr_abs(coin, j), 72)
                if pl:
                    yield j, pl
                busy = j + 24
                break


def oversold_bounce(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    r, busy = coin.rows, -1
    for i in range(24, len(r)):
        if i <= busy or not flushed(coin, i, p["drop"]):
            continue
        hi24, lo24 = coin.high(i - 23, i + 1), low(coin, i - 23, i + 1)
        for j in range(i + 1, min(len(r), i + CONFIRM_BARS + 1)):
            if r[j]["l"] > r[j - 1]["l"] and r[j]["c"] > r[j]["o"] and tradable(coin, j):
                lo = min(lo24, low(coin, i, j + 1))
                tg = ((lo + 0.382 * (hi24 - lo), 0.5), (lo + 0.5 * (hi24 - lo), 0.5))
                pl = long_plan(coin, j, lo - 0.5 * atr_abs(coin, j), 72, targets=tg)
                if pl:
                    yield j, pl
                busy = j + 24
                break


def capitulation(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    r, busy = coin.rows, -1
    for i in range(168, len(r)):
        if i <= busy or r[i]["c"] / r[i - 168]["c"] - 1 > -p["drop7d"]:
            continue
        for j in range(i + 1, min(len(r), i + 24 + 1)):
            if r[j]["c"] > coin.high(j - 24, j) and tradable(coin, j):
                pl = long_plan(coin, j, low(coin, j - 167, j + 1) - 0.5 * atr_abs(coin, j), p["time"])
                if pl:
                    yield j, pl
                busy = j + p["time"]
                break


def premium_discount(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    r, busy = coin.rows, -1
    for i in range(168, len(r)):
        pr = r[i].get("prem")
        if i <= busy or pr is None or not tradable(coin, i):
            continue
        hist = [x["prem"] for x in r[i - 168:i] if x.get("prem") is not None]
        if len(hist) < 100:
            continue
        sd = statistics.pstdev(hist)
        if sd and (pr - statistics.fmean(hist)) / sd <= p["z"]:
            pl = long_plan(coin, i, low(coin, i - 23, i + 1) - atr_abs(coin, i), 24,
                           targets_r=((1.0, 0.5), (2.0, 0.5)))
            if pl:
                yield i, pl
            busy = i + 24


def trials_long() -> list[Trial]:
    return (grid("flush_reclaim", flush_reclaim, {"drop": [0.06, 0.10], "liquid": [False, True]})
            + grid("oversold_bounce", oversold_bounce, {"drop": [0.07, 0.12]})
            + grid("capitulation", capitulation, {"drop7d": [0.30, 0.40], "time": [72, 168]})
            + grid("premium_discount", premium_discount, {"z": [-3.0, -4.0]}))


LIB_IDS = {"flush_reclaim": "lib.long_liq_flush_reclaim_long", "oversold_bounce": "lib.oversold_bounce_majors",
           "capitulation": "lib.capitulation_forced_selling_long", "premium_discount": "lib.premium_index_reversion"}
