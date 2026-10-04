"""Tier 1 of the scanner (docs/SCANNING.md): cheap local rules that decide which coins get a deep score.

Pure functions over the stream store's data (market/streams.py) and the recorded liquidations. A trigger's strength is
its value over its threshold, so 1.0 means "just triggered" and strengths add across rules. Every threshold reuses a
rule the desk already has, so each trigger can be evaluated later from its logged counts:
- funding and premium extremes: the funding signal's "elevated" level, 0.05%/8h (signals/core.py). Binance funding
  follows the premium index once it leaves the ±0.05% clamp, so the premium uses the same floor;
- liquidation bursts: signals/tape.py (> $50k in 15m and > 3x the prior 15m average);
- the BTC/ETH impulse: market_map.BTC_IMPULSE_15M, the market-pause veto;
- the volume surge: a robust z of the last hour above 2, decay.heat's "hot" level;
- moves: a 1h or 4h log return at |z| >= 2 versus the coin's own 15m volatility (SCANNING.md);
- the pump threshold: scan_job.MIN_CHANGE.
Fast events (liquidation bursts, a premium spike, the BTC/ETH impulse) decay in 10-45 min: the caller runs
`fast_events` between 15m closes and wakes tier 1 at once.

Keys are Binance ids (BTCUSDT); any ccxt symbols passed in are converted.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .decay import heat
from .indicators import lower_high
from .market.market_map import BTC_IMPULSE_15M
from .market.streams import TF_MS, raw_id
from .scan_job import MIN_CHANGE
from .signals.tape import LIQ_BURST_MIN_USD, LIQ_BURST_MULT

M15, H1 = TF_MS["15m"], TF_MS["1h"]
MOVE_Z, MIN_VOL_BARS = 2.0, 32            # z of the 1h/4h move; 15m returns needed to estimate volatility
VOL_Z = 2.0
FUNDING_FLOOR = PREMIUM_FLOOR = 0.0005    # per 8h; the premium index is quoted per 8h
DECILE = 0.10
PREMIUM_SPIKE_Z = 4.0                     # cross-sectional robust z of the premium (a fast event)
LISTING_DAYS = 30
TIER2_CAP = 20                            # deep scores per 15m window (~600 weight at ~29 per coin)
RESCORE_S = 3600                          # a coin re-triggering with the same rules waits this long (caller's window)


@dataclass(frozen=True)
class Trigger:
    symbol: str
    rule: str
    strength: float
    detail: str


def closed(bars: list[list[float]], now_ms: int, step: int = M15) -> list[list[float]]:
    return [b for b in bars if b[0] + step <= now_ms]


def move_z(bars: list[list[float]]) -> dict[str, float | None]:
    """{"z1h", "z4h"}: the move's log return over the coin's own 15m volatility, estimated on the bars before it."""
    c = [b[4] for b in bars if b[4] > 0]
    rets = [math.log(b / a) for a, b in itertools.pairwise(c)]
    out: dict[str, float | None] = {}
    for name, k in (("z1h", 4), ("z4h", 16)):
        base = rets[:-k][-96:]
        sd = _pstdev(base) if len(base) >= MIN_VOL_BARS else 0.0
        out[name] = round(sum(rets[-k:]) / (sd * math.sqrt(k)), 2) if sd > 0 else None
    return out


def _pstdev(xs: list[float]) -> float:
    """Population stdev in floats (two-pass, fsum): statistics.pstdev's value to float precision, ~10x faster (its
    exact fraction arithmetic held the guardian's loop ~20 ms per close, #78)."""
    m = math.fsum(xs) / len(xs)
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / len(xs))


def volume_z(bars: list[list[float]]) -> float | None:
    """Robust z (median/MAD) of the last hour's volume versus the hours before it, from closed 15m bars."""
    v = [b[5] for b in bars]
    hourly = [sum(v[i:i + 4]) for i in range(len(v) % 4, len(v), 4)]
    return heat(hourly, fast_hl=2, slow_hl=12)["z"]


def new_lower_high_after_pump(bars: list[list[float]], pct_24h: float | None) -> bool:
    """A pumped coin (24h >= MIN_CHANGE) whose 15m highs just turned lower: fires on the bar that turns it."""
    return (pct_24h or 0) >= MIN_CHANGE and lower_high(bars, 4) and not lower_high(bars[:-1], 4)


def _quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(q * len(s))))]


def cross_extremes(mark: dict[str, dict[str, Any]], interval_h: dict[str, int] | None = None) -> list[Trigger]:
    """Funding (per 8h) and premium in the universe's top or bottom decile AND beyond the floor."""
    out: list[Trigger] = []
    f8 = {s: m["funding"] * 8 / max((interval_h or {}).get(s, 8), 1) for s, m in mark.items()
          if m.get("funding") is not None}
    prem = {s: m["premium"] for s, m in mark.items() if m.get("premium") is not None}
    for name, vals, floor in (("funding", f8, FUNDING_FLOOR), ("premium", prem, PREMIUM_FLOOR)):
        if len(vals) < 10:
            continue
        hi, lo = _quantile(list(vals.values()), 1 - DECILE), _quantile(list(vals.values()), DECILE)
        for s, x in vals.items():
            if x >= hi and x >= floor:
                out.append(Trigger(s, f"{name}_hi", round(x / floor, 2), f"{name} {x * 100:.3f}% (top decile)"))
            elif x <= lo and x <= -floor:
                out.append(Trigger(s, f"{name}_lo", round(-x / floor, 2), f"{name} {x * 100:.3f}% (bottom decile)"))
    return out


def liq_bursts(liqs: dict[str, list[dict[str, Any]]], now_ms: int) -> list[Trigger]:
    """Per side: notional in the last 15m > LIQ_BURST_MIN_USD and > LIQ_BURST_MULT x the prior 15m average (1h)."""
    out = []
    for s, rows in liqs.items():
        for side in ("long_liquidated", "short_liquidated"):
            last = sum(r["notional"] for r in rows if r["side"] == side and r["ts_ms"] >= now_ms - M15)
            prior = sum(r["notional"] for r in rows if r["side"] == side
                        and now_ms - H1 <= r["ts_ms"] < now_ms - M15) / 3
            bar = max(LIQ_BURST_MIN_USD, LIQ_BURST_MULT * prior)
            if last > bar:
                out.append(Trigger(raw_id(s), f"liq_{side.split('_')[0]}_burst", round(last / bar, 2),
                                   f"{side.replace('_', ' ')} ${last:,.0f} in 15m"))
    return out


def premium_spikes(mark: dict[str, dict[str, Any]]) -> list[Trigger]:
    prem = {s: m["premium"] for s, m in mark.items() if m.get("premium") is not None}
    if len(prem) < 10:
        return []
    med = statistics.median(prem.values())
    mad = statistics.median(abs(x - med) for x in prem.values()) * 1.4826
    if mad <= 0:
        return []
    return [Trigger(s, "premium_spike", round(abs(x - med) / mad / PREMIUM_SPIKE_Z, 2),
                    f"premium {x * 100:.3f}% (cross-section z {(x - med) / mad:.1f})")
            for s, x in prem.items() if abs(x - med) / mad >= PREMIUM_SPIKE_Z]


def impulses(bars15: dict[str, list[list[float]]]) -> list[Trigger]:
    """BTC or ETH moving >= BTC_IMPULSE_15M % in 15 min: the latest price versus the close one bar earlier."""
    out = []
    for s in ("BTCUSDT", "ETHUSDT"):
        b = bars15.get(s) or []
        if len(b) >= 2 and b[-2][4]:
            pct = (b[-1][4] / b[-2][4] - 1) * 100
            if abs(pct) >= BTC_IMPULSE_15M:
                out.append(Trigger(s, "btc_impulse", round(abs(pct) / BTC_IMPULSE_15M, 2), f"{s} {pct:+.2f}% in 15m"))
    return out


def fast_events(bars15: dict[str, list[list[float]]], mark: dict[str, dict[str, Any]],
                liqs: dict[str, list[dict[str, Any]]], now_ms: int) -> list[Trigger]:
    """The events that wake tier 1 between closes. A btc_impulse also means every coin gets re-checked."""
    return liq_bursts(liqs, now_ms) + premium_spikes(_raw(mark)) + impulses(_raw(bars15))


def evaluate(bars15: dict[str, list[list[float]]], ticker: dict[str, dict[str, Any]],
             mark: dict[str, dict[str, Any]], liqs: dict[str, list[dict[str, Any]]], now_ms: int,
             listing_age_d: dict[str, float] | None = None, events: dict[str, str] | None = None,
             interval_h: dict[str, int] | None = None) -> list[Trigger]:
    """Every rule at a 15m close. bars15: candles(sym, "15m", ~120) per liquid coin (forming bar included);
    ticker / mark: stream_read.latest(); liqs: recorded liquidations of the last hour per symbol."""
    bars15, ticker, mark = _raw(bars15), _raw(ticker), _raw(mark)
    out: list[Trigger] = []
    for s, bars in bars15.items():
        cb = closed(bars, now_ms)
        for name, z in move_z(cb).items():
            if z is not None and abs(z) >= MOVE_Z:
                out.append(Trigger(s, f"move_{name[1:]}", round(abs(z) / MOVE_Z, 2), f"{name[1:]} move z {z:+.1f}"))
        vz = volume_z(cb)
        if vz is not None and vz >= VOL_Z:
            out.append(Trigger(s, "volume_surge", round(vz / VOL_Z, 2), f"last hour volume z {vz:.1f}"))
        pct = (ticker.get(s) or {}).get("pct")
        if new_lower_high_after_pump(cb, pct):
            out.append(Trigger(s, "lower_high_after_pump", 1.0, f"15m lower high, 24h {pct:+.1f}%"))
    out += cross_extremes(mark, _raw(interval_h or {})) + liq_bursts(liqs, now_ms)
    if now_ms % H1 < M15:   # standing states: once an hour, not every window
        out += [Trigger(s, "new_listing", 1.0, f"listed {age:.0f}d ago")
                for s, age in _raw(listing_age_d or {}).items() if age < LISTING_DAYS]
    out += [Trigger(s, "event", 1.0, why) for s, why in _raw(events or {}).items()]
    return out


def select(triggers: list[Trigger], carried: list[dict[str, Any]] | None = None,
           recent: dict[str, set[str]] | None = None, cap: int = TIER2_CAP) -> dict[str, Any]:
    """Merge per coin (strengths add), add the last window's queue at half strength (dropped below 1), rank, cap.
    `recent`: {symbol: rules} deep-scored in the last RESCORE_S; a coin with no new rule since is skipped (84% of
    4h-move triggers persist into the next window, docs/SCANNING.md). Returns {"selected", "queued", "skipped",
    "counts"}; counts per rule (before skipping) are what gets logged for evaluating the triggers."""
    coins: dict[str, dict[str, Any]] = {}
    skipped = sorted({t.symbol for t in triggers if t.symbol in (recent or {})
                      and {u.rule for u in triggers if u.symbol == t.symbol} <= (recent or {})[t.symbol]})
    for t in triggers:
        if t.symbol in skipped:
            continue
        c = coins.setdefault(t.symbol, {"symbol": t.symbol, "strength": 0.0, "rules": [], "details": []})
        c["strength"] = round(c["strength"] + t.strength, 2)
        c["rules"].append(t.rule)
        c["details"].append(t.detail)
    for q in carried or []:
        half = round(q["strength"] / 2, 2)
        if half >= 1 and q["symbol"] not in coins:
            coins[q["symbol"]] = {**q, "strength": half, "carried": True}
    ranked = sorted(coins.values(), key=lambda c: -c["strength"])
    return {"selected": ranked[:cap], "queued": ranked[cap:], "skipped": skipped,
            "counts": dict(Counter(t.rule for t in triggers))}


def _raw(d: dict[str, Any]) -> dict[str, Any]:
    return {raw_id(k): v for k, v in d.items()}
