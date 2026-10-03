"""Level-sweep study (docs/MARKET_MAP.md, research 2026-09-28): how far does price poke past obvious levels
(round numbers, prior 24h highs) before failing or continuing, on LAST and on MARK price? Measured, not assumed:
Osler (2003) found stops cluster just past round numbers, but Osler (2005) found crossings more often CONTINUE.
Our stops go past the measured overshoot percentile, not past a textbook distance.

Event: first 1m bar whose high exceeds level L after >= 60 min entirely below it (cooldown 240 min per level).
Levels at time t use only past data: grid step from the previous close, prior-24h high, and non-round control
levels (L + 0.37 step) as the baseline. Pure detection + summary are unit-tested; `run_study` fetches klines."""

from __future__ import annotations

import asyncio
import json
import math
import statistics
from typing import Any

from .tape_math import clear_above_round, round_step

QUIET_BARS, COOLDOWN, WINDOW = 60, 240, 240
SCHEMA = """CREATE TABLE IF NOT EXISTS level_sweeps (symbol TEXT, ts INTEGER, kind TEXT, tier INTEGER, level REAL,
    data TEXT, PRIMARY KEY (symbol, ts, kind, level))"""


def tier_of(level: float, step: float) -> int:
    k = round(level / step)
    return 3 if k % 10 == 0 else 2 if k % 2 == 0 else 1


def _atr_1h(bars: list[list[float]], t: int) -> float:
    hours = [bars[i:i + 60] for i in range(max(0, t - 14 * 60), t - 59, 60)]
    rng = [max(b[2] for b in h) - min(b[3] for b in h) for h in hours if h]
    return statistics.fmean(rng) if rng else 0.0


def measure(bars: list[list[float]], mark: dict[int, list[float]], t: int, level: float) -> dict[str, Any]:
    """Excursion after the cross at bar t: until the first close back below L, or WINDOW bars."""
    end = min(len(bars), t + WINDOW)
    back = next((i for i in range(t, end) if bars[i][4] < level), None)
    stop_i = back if back is not None else end
    seg = bars[t:stop_i + 1] if back is not None else bars[t:end]
    hi = max(b[2] for b in seg)
    mk = [mark[b[0]][2] for b in seg if b[0] in mark]
    atr = _atr_1h(bars, t)
    prior_vol = statistics.median(b[5] for b in bars[max(0, t - 60):t]) or 1e-9
    rev = {}
    target = level - 0.25 * atr
    for n in (15, 60, 240):
        rev[f"rev{n}"] = any(b[4] < target for b in bars[t:min(len(bars), t + n)])
    # fake-out size = the most price went past L before the failure was confirmed (close < L - 0.25 ATR):
    # the number a stop must clear. For continuations it is the max within the window.
    fail = next((i for i in range(t, end) if bars[i][4] < target), None)
    run = bars[t:(fail if fail is not None else end) + 1]
    run_mk = [mark[b[0]][2] for b in run if b[0] in mark]
    rev["fakeout_pct"] = round((max(b[2] for b in run) / level - 1) * 100, 4) if fail is not None else None
    rev["fakeout_mark_pct"] = round((max(run_mk) / level - 1) * 100, 4) if fail is not None and run_mk else None
    rev["minutes_to_fail"] = None if fail is None else fail - t
    return {"overshoot_pct": round((hi / level - 1) * 100, 4),
            "overshoot_mark_pct": round((max(mk) / level - 1) * 100, 4) if mk else None,
            "overshoot_atr": round((hi - level) / atr, 3) if atr else None, "atr1h_pct": round(atr / level * 100, 3),
            "closes_above": sum(1 for b in bars[t:stop_i] if b[4] > level),
            "bars_to_reclose": None if back is None else back - t, "vol_mult": round(bars[t][5] / prior_vol, 2),
            "complete": t + WINDOW <= len(bars), **rev}


def detect(bars: list[list[float]], mark_bars: list[list[float]]) -> list[dict[str, Any]]:
    """All upward level crossings in 1m bars [ts, o, h, l, c, v]; mark bars keyed by ts for mark overshoot."""
    mark = {b[0]: b for b in mark_bars}
    last_hit: dict[tuple[str, float], int] = {}
    out = []
    for t in range(max(QUIET_BARS, 1440 if len(bars) > 1500 else QUIET_BARS), len(bars)):
        prev_close, hi = bars[t - 1][4], bars[t][2]
        quiet_hi = max(b[2] for b in bars[t - QUIET_BARS:t])
        if hi <= quiet_hi:
            continue
        step = round_step(prev_close)
        levels: list[tuple[str, float, int]] = []
        k = math.floor(quiet_hi / step) + 1
        while k * step < hi:
            L = round(k * step, 10)
            levels.append(("round", L, tier_of(L, step)))
            k += 1
        if not levels:   # control = a non-round level (grid + 0.37 step) crossed by a bar that crossed no round level
            c = round((math.floor(quiet_hi / step) + 0.37) * step, 10)
            c = c if c > quiet_hi else round(c + step, 10)
            if quiet_hi < c < hi:
                levels.append(("control", c, 0))
        if t >= 1440:
            h24 = max(b[2] for b in bars[t - 1440:t])
            if quiet_hi < h24 < hi:
                levels.append(("high24h", h24, 0))
        for kind, L, tier in levels:
            key = (kind, L)
            if t - last_hit.get(key, -COOLDOWN) < COOLDOWN:
                continue
            last_hit[key] = t
            out.append({"ts": bars[t][0], "kind": kind, "tier": tier, "level": L, **measure(bars, mark, t, L)})
    return out


def _pcts(xs: list[float]) -> dict[str, float] | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))]  # noqa: E731
    return {"n": len(xs), "p50": round(q(0.5), 3), "p75": round(q(0.75), 3), "p90": round(q(0.9), 3),
            "p95": round(q(0.95), 3)}


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Overshoot percentiles and reversal rates per level type. p90 needs n >= 300, p95 n >= 500 to trust."""
    out = {}
    groups = {"round_tier3": lambda e: e["kind"] == "round" and e["tier"] == 3,
              "round_tier1-2": lambda e: e["kind"] == "round" and e["tier"] < 3,
              "high24h": lambda e: e["kind"] == "high24h", "control": lambda e: e["kind"] == "control"}
    for name, f in groups.items():
        ev = [e for e in events if f(e) and e["complete"]]
        if not ev:
            continue
        out[name] = {"n": len(ev), "overshoot_pct": _pcts([e["overshoot_pct"] for e in ev]),
                     "overshoot_mark_pct": _pcts([e["overshoot_mark_pct"] for e in ev]),
                     "overshoot_atr": _pcts([e["overshoot_atr"] for e in ev]),
                     "fakeout_pct": _pcts([e.get("fakeout_pct") for e in ev]),
                     "fakeout_mark_pct": _pcts([e.get("fakeout_mark_pct") for e in ev]),
                     "fakeout_atr": _pcts([e["fakeout_pct"] / e["atr1h_pct"] for e in ev
                                           if e.get("fakeout_pct") is not None and e["atr1h_pct"]]),
                     "reclosed_pct": round(sum(e["bars_to_reclose"] is not None for e in ev) / len(ev) * 100, 1),
                     **{f"{r}_pct": round(sum(e[r] for e in ev) / len(ev) * 100, 1) for r in ("rev15", "rev60", "rev240")}}
    out["by_session"] = by_session(events)
    out["note"] = "trust p90 only with n >= 300 and p95 with n >= 500; events co-move with BTC, so they are not independent"
    return out


def by_session(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Does the session change how levels behave? (asia 00-07, london 07-NY open, ny, late; weekends apart)"""
    from datetime import datetime, timezone

    from .sessions import session_bucket
    out: dict[str, dict[str, Any]] = {}
    for kind in ("high24h", "round", "control"):
        ev = [e for e in events if e["kind"] == kind and e["complete"]]
        groups: dict[str, list[dict]] = {}
        for e in ev:
            groups.setdefault(session_bucket(datetime.fromtimestamp(e["ts"] / 1000, timezone.utc)), []).append(e)
        for sess, g in sorted(groups.items()):
            fm = _pcts([e.get("fakeout_mark_pct") for e in g])
            out.setdefault(sess, {})[kind] = {
                "n": len(g), "rev60_pct": round(sum(e["rev60"] for e in g) / len(g) * 100, 1),
                "rev240_pct": round(sum(e["rev240"] for e in g) / len(g) * 100, 1),
                "fakeout_mark_p50": fm and fm["p50"], "fakeout_mark_p90": fm and fm["p90"]}
    return out


async def _fetch_1m(ex, sym: str, since: int, until: int, mark: bool) -> list[list[float]]:
    rows, cur = [], since
    params = {"price": "mark"} if mark else {}
    while cur < until:
        chunk = await ex.public.fetch_ohlcv(sym, "1m", since=cur, limit=1500, params=params)
        if not chunk:
            break
        rows += [c for c in chunk if c[0] < until]
        if chunk[-1][0] + 60_000 <= cur:
            break
        cur = chunk[-1][0] + 60_000
    return rows


async def run_study(ex, journal, days: float = 3, top: int = 60, now_ms: int | None = None) -> dict[str, Any]:
    import time
    now_ms = now_ms or int(time.time() * 1000)
    since = now_ms - int((days + 1) * 86_400_000)          # +1 day of history for the prior-24h high
    perps = await ex.coin_perps()
    tickers = await ex.public.fetch_tickers()
    syms = sorted((s for s in perps if s in tickers), key=lambda s: tickers[s].get("quoteVolume") or 0,
                  reverse=True)[:top]
    journal.db.execute(SCHEMA)
    sem, n_events = asyncio.Semaphore(4), 0

    async def one(sym: str) -> int:
        async with sem:
            try:
                last = await _fetch_1m(ex, sym, since, now_ms, False)
                mk = await _fetch_1m(ex, sym, since, now_ms, True)
            except Exception:  # noqa: BLE001
                return 0
        ev = await asyncio.to_thread(detect, last, mk)
        rows = [(sym, e["ts"], e["kind"], e["tier"], e["level"], json.dumps(e)) for e in ev]
        await journal.write(lambda j: j.db.executemany("INSERT OR REPLACE INTO level_sweeps VALUES (?,?,?,?,?,?)",
                                                       rows))   # a unit per coin (#79)
        return len(ev)

    for n in await asyncio.gather(*(one(s) for s in syms)):
        n_events += n
    return {"coins": len(syms), "days": days, "events_logged": n_events, "report": report(journal)}


def report(journal) -> dict[str, Any]:
    journal.db.execute(SCHEMA)
    return summarize([json.loads(r[0]) for r in journal.db.execute("SELECT data FROM level_sweeps")])


GUIDE_MIN_N = 100


def guide(db_path, session: str | None = None) -> dict[str, dict[str, float]]:
    """p90 fake-out size per level type (mark %, ATR multiple), only where n >= GUIDE_MIN_N. With a session
    bucket, the session's own p90 is used when it has enough events AND is wider (thin weekend/Asia books run
    stops further); never tighter than the overall figure."""
    import sqlite3
    db = sqlite3.connect(str(db_path), timeout=15)
    try:
        db.execute(SCHEMA)
        s = summarize([json.loads(r[0]) for r in db.execute("SELECT data FROM level_sweeps")])
    finally:
        db.close()
    out = {}
    for k in ("round_tier3", "round_tier1-2", "high24h"):
        g = s.get(k) or {}
        fm, fa = g.get("fakeout_mark_pct"), g.get("fakeout_atr")
        if fm and fa and fm["n"] >= GUIDE_MIN_N:
            out[k] = {"p90_mark_pct": fm["p90"], "p90_atr": fa["p90"], "n": fm["n"]}
            ss = (s.get("by_session", {}).get(session or "", {}).get("round" if k.startswith("round") else k) or {})
            if ss.get("n", 0) >= GUIDE_MIN_N and (ss.get("fakeout_mark_p90") or 0) > fm["p90"]:
                out[k].update(p90_mark_pct=ss["fakeout_mark_p90"], session=session, n=ss["n"])
    return out


def sweep_proof_stop(last: float, high_24h: float | None, atr: float, g: dict[str, dict]) -> dict[str, Any] | None:
    """Stop past the nearest obvious level ABOVE price by its measured p90 fake-out (the larger of % and ATR),
    then pushed clear of any round-number stop cluster. None without enough measured events."""
    step = round_step(last)
    nxt = (math.floor(last / step) + 1) * step
    cands = [("round_tier3" if tier_of(nxt, step) == 3 else "round_tier1-2", nxt)]
    if high_24h and high_24h > last:
        cands.append(("high24h", high_24h))
    kind, level = min(cands, key=lambda c: c[1])
    if kind not in g:
        return None
    buf = max(level * g[kind]["p90_mark_pct"] / 100, g[kind]["p90_atr"] * atr)
    stop = level + buf
    # stay out of the round-number zone on BOTH sides: just past it is the crowd's stop cluster, and just under it
    # gets tagged because price is pulled to round numbers (take-profits sit AT them, Osler 2003)
    stop = clear_above_round(stop)
    return {"price": stop, "level": level, "kind": kind, "buffer_pct": round(buf / level * 100, 2),
            "basis": f"p90 fake-out past {kind} = {g[kind]['p90_mark_pct']}% mark / {g[kind]['p90_atr']} ATR "
                     f"(n={g[kind]['n']}{', ' + g[kind]['session'] + ' session' if g[kind].get('session') else ''}"
                     "), cleared of the round-number stop zone"}
