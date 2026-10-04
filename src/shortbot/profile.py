"""Per-coin behaviour profile: every coin plays its own game, but a coin-specific number is an anecdote until it
repeats. So each coin statistic is SHRUNK toward the universe value with weight w = n / (n + PRIOR_N): with 5
events the coin barely moves the estimate, with 300 it mostly speaks for itself. Sections:
- levels: how this coin behaves at round numbers / 24h highs (from the sweep study)
- sessions: where its moves and volume happen (30 days of 1h bars), drift per session with a t-stat
- context: sector, BTC beta, residual moves (market map); our own trades on it
The 'who controls it' section (float, holders, unlocks, market makers) comes from docs/COIN_PROFILE.md research."""

from __future__ import annotations

import itertools
import json
import math
import statistics
from datetime import UTC, datetime
from typing import Any

from .sessions import session_bucket

PRIOR_N = 30


def shrink(coin: float | None, n: int, universe: float | None, prior_n: int = PRIOR_N) -> dict[str, Any]:
    """Blend a coin estimate with the universe one; report the weight so nobody over-reads a small sample."""
    if coin is None or universe is None:
        return {"value": universe if coin is None else coin, "coin": coin, "universe": universe, "n": n, "w": 0.0}
    w = n / (n + prior_n)
    return {"value": round(w * coin + (1 - w) * universe, 3), "coin": round(coin, 3), "universe": round(universe, 3),
            "n": n, "w": round(w, 2)}


def _p90(xs: list[float]) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(0.9 * len(xs)))] if xs else None


def level_behaviour(events: list[dict], symbol: str) -> dict[str, Any]:
    """Sweep-study stats for this coin vs all coins, per level type, shrunk."""
    out = {}
    for kind in ("high24h", "round"):
        allk = [e for e in events if e["kind"] == kind and e.get("complete")]
        mine = [e for e in allk if e.get("symbol") == symbol]
        if not allk:
            continue
        rate = lambda ev: sum(e["rev240"] for e in ev) / len(ev) * 100 if ev else None  # noqa: E731
        out[kind] = {
            "failed_within_4h_pct": shrink(rate(mine), len(mine), rate(allk)),
            "fakeout_p90_mark_pct": shrink(_p90([e.get("fakeout_mark_pct") for e in mine]),
                                           sum(1 for e in mine if e.get("fakeout_mark_pct") is not None),
                                           _p90([e.get("fakeout_mark_pct") for e in allk]))}
    return out


def session_behaviour(c1h: list[list[float]]) -> dict[str, Any]:
    """Where this coin's moves and volume happen (share of total |return| and quote volume per session bucket),
    and the mean signed hourly return per bucket with a t-stat. |t| < 2 = noise; even |t| > 2 across 8 buckets
    will happen by chance about once in three coins, so treat it as a hypothesis to watch."""
    rows: dict[str, dict[str, list[float]]] = {}
    for a, b in itertools.pairwise(c1h):
        if not a[4] or not b[4]:
            continue
        r = (b[4] / a[4] - 1) * 100
        k = session_bucket(datetime.fromtimestamp(b[0] / 1000, UTC))
        d = rows.setdefault(k, {"r": [], "v": []})
        d["r"].append(r)
        d["v"].append(b[5] * b[4])
    tot_abs = sum(abs(x) for d in rows.values() for x in d["r"]) or 1.0
    tot_vol = sum(sum(d["v"]) for d in rows.values()) or 1.0
    out = {}
    for k, d in sorted(rows.items()):
        n = len(d["r"])
        mean = statistics.fmean(d["r"])
        sd = statistics.stdev(d["r"]) if n > 1 else 0.0
        t = mean / (sd / math.sqrt(n)) if sd else 0.0
        out[k] = {"hours": n, "move_share_pct": round(sum(abs(x) for x in d["r"]) / tot_abs * 100, 1),
                  "volume_share_pct": round(sum(d["v"]) / tot_vol * 100, 1), "mean_ret_pct_per_h": round(mean, 3),
                  "t_stat": round(t, 2), "note": "possible pattern, watch" if abs(t) >= 2 else ""}
    return out


async def build_profile(ex, journal, symbol: str) -> dict[str, Any]:
    from .exchange import to_symbol
    from .market.market_map import cached
    symbol = to_symbol(symbol)
    events = []
    for sym, data in journal.db.execute("SELECT symbol, data FROM level_sweeps"):
        e = json.loads(data)
        e["symbol"] = sym
        events.append(e)
    c1h = await ex.public.fetch_ohlcv(symbol, "1h", limit=720)
    closes = [c[4] for c in c1h]
    m = cached(journal, 3600) or {}
    trades = [t for t in journal.trades(500) if t["symbol"] == symbol and t["status"] == "closed"]
    return {
        "symbol": symbol,
        "how_to_read": "every coin number is blended with the all-coin number by w = n/(n+30); w < 0.5 means the "
                       "coin's own history is still mostly anecdote",
        "levels": level_behaviour(events, symbol),
        "sessions_30d": session_behaviour(c1h),
        "range_30d": {"high": max(c[2] for c in c1h), "low": min(c[3] for c in c1h),
                      "off_high_pct": round((1 - closes[-1] / max(c[2] for c in c1h)) * 100, 1)} if c1h else None,
        "context": (m.get("coins") or {}).get(symbol),
        "our_trades": [{"id": t["id"], "setup": t["setup"], "pnl": t["pnl"], "opened_at": t["opened_at"],
                        "exit_reason": t["exit_reason"], "lesson": t.get("lesson")} for t in trades],
        "controllers": "see docs/COIN_PROFILE.md (research pending): float, holder concentration, unlocks, MM deals",
    }
