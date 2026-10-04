"""Pre-registered strategies (docs/STRATEGY, TAPE, PLAYBOOK, MARKET_MAP, PATTERNS), each with a SMALL parameter grid
fixed before any result was seen. Every param set is one TRIAL in the ledger (report.py counts them all).

A strategy is `fn(m, coin, p) -> iterable of (i, Plan)`: signals known at the close of bar i, using bars <= i only.
Cross-coin strategies (laggard) and controls live in strategies_more.py. Thresholds come from the docs:
pump = Kamps & Kleinberg (vol > 4x 12h mean, high > 5% over the 12h mean close); sweep-proof stop = p90 fake-out
past the 24h high (2.5% / 1.25 ATR) or a round number (4.1% / 1.4 ATR) from docs/MARKET_MAP.md."""

from __future__ import annotations

import itertools
import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from ..tape_math import clear_above_round, next_round_above
from .features import Coin, Market
from .sim import Plan

WARMUP = 72                  # bars of history before any entry (72h beta / features)
MIN_VOL24 = 1e6              # tradability floor at the signal ($ 24h quote volume); run.py --min-vol24 sets it (#73)
FADE_TARGETS = ((1.0, 0.5), (2.0, 0.5))   # (R multiple, fraction): half at 1R, rest at 2R
ASIA_LATE_WKND = ("asia", "late")         # + every *_wknd bucket


@dataclass(frozen=True)
class Trial:
    strategy: str
    params: dict[str, Any]
    fn: Callable[..., Iterable]
    cross: bool = False            # fn(m, p) -> (coin, i, plan) instead of per-coin
    control: bool = False          # placebos are controls, not trials
    notes: str = ""
    grid_idx: int = 0

    @property
    def name(self) -> str:
        return f"{self.strategy}[{','.join(f'{k}={v}' for k, v in self.params.items() if k != 'session')}]" + (
            "" if "session" not in self.params else f"[{self.params['session']}]")


def grid(strategy: str, fn: Callable, space: dict[str, list], **kw: Any) -> list[Trial]:
    keys = list(space)
    return [Trial(strategy, dict(zip(keys, vals, strict=True)), fn, grid_idx=g, **kw)
            for g, vals in enumerate(itertools.product(*(space[k] for k in keys)))]


def atr_abs(coin: Coin, i: int) -> float:
    return coin.atr[i] / 100 * coin.rows[i]["c"]


def r_targets(entry: float, stop: float, spec=FADE_TARGETS) -> tuple[tuple[float, float], ...]:
    risk = stop - entry
    return tuple((entry - r * risk, f) for r, f in spec)


def plan(coin: Coin, i: int, stop: float, time_stop: int, targets=None, limit_px: float | None = None,
         **kw: Any) -> Plan | None:
    """A plan with R-targets by default (market entry at the close, or a resting sell at `limit_px`); meta keeps
    the exits in portable units (ATR, R) for the placebo."""
    ref = limit_px if limit_px is not None else coin.rows[i]["c"]
    if limit_px is not None:
        kw.update(entry="limit", limit_px=limit_px)
    if stop <= ref:
        return None
    tg = targets if targets is not None else r_targets(ref, stop)
    tg = tuple((px, f) for px, f in tg if 0 < px < ref)
    a = atr_abs(coin, i)
    kw.setdefault("be_r", 1.0)
    meta = {"stop_atr": (stop - ref) / a if a else None,
            "targets_r": tuple(((ref - px) / (stop - ref), f) for px, f in tg)}
    return Plan(stop=stop, targets=tg, time_stop=time_stop, meta=meta, **kw)


def set_min_vol24(v: float) -> None:
    """The floor every strategy reads at signal time (the live default is RiskLimits().min_quote_volume_24h)."""
    global MIN_VOL24
    MIN_VOL24 = float(v)


def tradable(coin: Coin, i: int) -> bool:
    return i >= WARMUP and sum(r["qv"] for r in coin.rows[i - 23:i + 1]) >= MIN_VOL24


def in_session(coin: Coin, i: int, which: str | None) -> bool:
    if not which:
        return True
    from datetime import datetime

    from ..sessions import session_bucket
    b = session_bucket(datetime.fromtimestamp((coin.rows[i]["ts"] + 3_600_000) / 1000, UTC))
    return b.endswith("_wknd") or b in ASIA_LATE_WKND


def pumps(m: Market, coin: Coin) -> list[int]:
    """Kamps & Kleinberg pump bars (vol > 4x the 12h mean AND high > 5% over the 12h mean close)."""
    key = ("pumps", coin.symbol)
    if key not in m.cache:
        r, out = coin.rows, []
        for i in range(12, len(r)):
            vm = statistics.fmean(x["qv"] for x in r[i - 12:i])
            cm = statistics.fmean(x["c"] for x in r[i - 12:i])
            if vm and r[i]["qv"] > 4 * vm and r[i]["h"] > 1.05 * cm:
                out.append(i)
        m.cache[key] = out
    return m.cache[key]


# ---------------------------------------------------------------- pump_fade: failed sweep of the pump high
def pump_fade(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    r, last_p = coin.rows, -10_000
    for pi in pumps(m, coin):
        if pi - last_p < 12:          # one episode: consecutive pump bars belong to the same pump
            continue
        last_p = pi
        base = statistics.fmean(x["c"] for x in r[pi - 12:pi])
        ph = r[pi]["h"]
        for j in range(pi + 1, min(len(r), pi + 13)):
            if r[j]["h"] > ph * 1.001 and r[j]["c"] < ph and tradable(coin, j) and in_session(coin, j, p.get("session")):
                top = r[j]["h"]
                stop = top + p["buf_atr"] * atr_abs(coin, j)
                tg = ((top - 0.5 * (top - base), 0.5), (top - 0.618 * (top - base), 0.5))
                pl = plan(coin, j, stop, p["time"], targets=tg)
                if pl:
                    yield j, pl
                break
            ph = max(ph, r[j]["h"])


# ---------------------------------------------------------------- exp_level_fade: 24h-high / round-number sweeps
P90 = {"high24": (2.5, 1.25), "round": (4.1, 1.4)}   # p90 fake-out (% of level, ATR multiple), docs/MARKET_MAP.md


def sweep_proof(level: float, kind: str, atr: float, wick: float = 0.0) -> float:
    pct, k = P90[kind]
    return clear_above_round(max(wick * 1.001, level + max(level * pct / 100, k * atr)))


def level_fade(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    r = coin.rows
    for i in range(WARMUP, len(r)):
        hi = coin.high(i - 26, i - 2)                   # prior 24h high, before the last 3 bars
        lvl = hi if p["level"] == "high24" else next_round_above(hi)
        a = atr_abs(coin, i)
        if not tradable(coin, i) or not in_session(coin, i, p.get("session")):
            continue
        if p["entry"] == "market":
            def failed(k: int, lvl: float = lvl) -> bool:   # pierced within bars k-2..k and closed back below at k
                return r[k]["c"] < lvl and any(b["h"] > lvl * 1.001 for b in r[k - 2:k + 1])
            accepted = r[i - 1]["c"] > lvl and r[i - 2]["c"] > lvl
            if failed(i) and not failed(i - 1) and not accepted:
                wick = max(b["h"] for b in r[i - 2:i + 1])
                pl = plan(coin, i, sweep_proof(lvl, p["level"], a, wick), p["time"])
                if pl:
                    yield i, pl
        else:   # resting sell just past the level (fills after the sweep starts), placed when price is near it
            c = r[i]["c"]
            if coin.high(i - 2, i + 1) < lvl and c >= lvl - 1.5 * a:
                lim = lvl * (1 + P90[p["level"]][0] / 200)            # halfway into the typical fake-out
                stop = sweep_proof(lvl, p["level"], a)
                pl = plan(coin, i, stop, p["time"], limit_px=lim)
                if pl:
                    yield i, pl


# ---------------------------------------------------------------- bounce-fade in a breakdown
def bounce_fade(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    r, e = coin.rows, coin.ema20
    for i in range(WARMUP, len(r)):
        r24 = coin.ret(i, 24)
        if r24 is None or r24 > p["drop"] or not tradable(coin, i):
            continue
        lower_high = coin.high(i - 11, i + 1) < coin.high(i - 36, i - 12)
        if lower_high and r[i]["h"] >= e[i] and r[i]["c"] < e[i] and r[i - 1]["c"] < e[i - 1]:
            stop = coin.high(i - 2, i + 1) + 0.5 * atr_abs(coin, i)
            pl = plan(coin, i, stop, p["time"], targets=r_targets(r[i]["c"], stop, ((1.5, 0.5), (3.0, 0.5))),
                      trail_act_r=2.0, trail_dist_r=1.0)
            if pl:
                yield i, pl


# ---------------------------------------------------------------- post-pump decay: short the bounce into the pump VWAP
def post_pump_decay(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    r = coin.rows
    used: set[int] = set()   # pump bars already traded
    for pi in pumps(m, coin):
        if any(abs(pi - u) < 24 for u in used):
            continue
        base = statistics.fmean(x["c"] for x in r[pi - 12:pi])
        pv = pq = 0.0
        for j in range(pi, min(len(r), pi + 25)):
            if j >= pi + 3 and pq:
                vw = pv / pq
                if (r[j - 1]["c"] < vw and r[j]["h"] >= vw and r[j]["c"] < vw and tradable(coin, j)):
                    stop = max(r[j]["h"], vw) + p["stop_atr"] * atr_abs(coin, j)
                    tg = ((r[j]["c"] - (stop - r[j]["c"]), 0.5), (base, 0.5))
                    pl = plan(coin, j, stop, p["time"], targets=tg)
                    if pl:
                        used.add(pi)
                        yield j, pl
                        break
            pv += (r[j]["h"] + r[j]["l"] + r[j]["c"]) / 3 * r[j]["qv"]
            pq += r[j]["qv"]


# ---------------------------------------------------------------- funding / premium extremes
def _stop_above(coin: Coin, i: int, k: float = 2.0) -> float:
    return coin.high(i - 2, i + 1) + k * atr_abs(coin, i)


def funding_hi(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    r = coin.rows
    for ts, rate in coin.funding:
        if rate * 100 < p["thr"]:
            continue
        i = coin.pos.get(round(ts / 60_000) * 60_000 - 3_600_000)   # the bar whose CLOSE is the print time
        if i is None or i < WARMUP:
            continue
        if p["confirm"] == "ema":                  # wait for the first close below EMA20 within 8h
            i = next((j for j in range(i, min(len(r), i + 9)) if r[j]["c"] < coin.ema20[j]), None)
        if i is None or not tradable(coin, i):
            continue
        pl = plan(coin, i, _stop_above(coin, i), 24)
        if pl:
            yield i, pl


def premium_hi(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    for i in range(WARMUP, len(coin.rows)):
        pr = coin.rows[i].get("prem")
        if pr is not None and pr * 100 >= p["thr"] and tradable(coin, i):
            pl = plan(coin, i, _stop_above(coin, i), 24)
            if pl:
                yield i, pl


# ---------------------------------------------------------------- squeeze exhaustion: price up hard while OI falls
def squeeze_exhaustion(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    r = coin.rows
    for i in range(WARMUP, len(r)):
        o0, o1 = r[i - 1].get("oi"), r[i].get("oi")
        ret1 = (r[i]["c"] / r[i - 1]["c"] - 1) * 100
        if not (o0 and o1 and ret1 > 3 * coin.atr[i - 1] and (o1 / o0 - 1) * 100 <= -3):
            continue
        j: int | None = i   # the entry bar; None: no red bar came
        if p["entry"] == "red":
            j = next((k for k in range(i + 1, min(len(r), i + 7)) if r[k]["c"] < r[k]["o"]), None)
        if j is None or not tradable(coin, j):
            continue
        stop = coin.high(i, j + 1) + atr_abs(coin, j)
        tg = r_targets(r[j]["c"], stop, ((1.0, 0.5),)) + ((r[i]["o"], 0.5),)
        pl = plan(coin, j, stop, p["time"], targets=tg)
        if pl:
            yield j, pl


def listing_fade(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    """Young perps: short once the listing is `age_h` old (after the first volatility), wide stop, long hold."""
    r = coin.rows
    if not coin.onboard_ms or coin.onboard_ms < m.t0 - 3_600_000 or r[0]["ts"] > coin.onboard_ms + 6 * 3_600_000:
        return          # listed before the window, or bars do not start at the listing (unknown first hours)
    for i in range(24, len(r)):
        age_h = (r[i]["ts"] + 3_600_000 - coin.onboard_ms) / 3_600_000
        if age_h >= p["age_h"]:
            if sum(x["qv"] for x in r[i - 23:i + 1]) >= MIN_VOL24:
                stop = coin.high(0, i + 1) + atr_abs(coin, i)
                c = r[i]["c"]
                pl = plan(coin, i, stop, p["time"], targets=((c * 0.8, 0.5), (c * 0.65, 0.5)), be_r=None,
                          trail_act_r=None)
                if pl:
                    yield i, pl
            return


def trials() -> list[Trial]:
    """THE pre-registered ledger. Session variants are param set 0 of pump_fade / level_fade restricted to
    Asia/late/weekend hours (docs/PATTERNS.md hypothesis), fixed before the backtest ran."""
    from .strategies_more import delisting, laggard
    t = (grid("pump_fade", pump_fade, {"buf_atr": [0.5, 1.0], "time": [12, 24]})
         + grid("level_fade", level_fade, {"level": ["high24", "round"], "entry": ["market", "limit"], "time": [24]})
         + grid("bounce_fade", bounce_fade, {"drop": [-8, -15], "time": [12, 24]})
         + grid("post_pump_decay", post_pump_decay, {"stop_atr": [1.0, 2.0], "time": [12, 24]})
         + grid("listing_fade", listing_fade, {"age_h": [24, 72], "time": [72, 168]})
         + grid("funding_hi", funding_hi, {"thr": [0.05, 0.10], "confirm": ["none", "ema"]})
         + grid("premium_hi", premium_hi, {"thr": [0.5, 1.0]})
         + grid("squeeze_exhaustion", squeeze_exhaustion, {"entry": ["close", "red"], "time": [12, 24]})
         + grid("laggard", laggard, {"corr": [0.3, 0.5]}, cross=True)
         + grid("pump_fade_session", pump_fade, {"buf_atr": [0.5], "time": [12], "session": ["asia_late_wknd"]})
         + grid("level_fade_session", level_fade, {"level": ["high24"], "entry": ["market"], "time": [24],
                                                   "session": ["asia_late_wknd"]})
         + grid("delisting", delisting, {"days_before": [3, 1]}, notes="needs delist_ms"))
    return t
