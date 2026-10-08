"""Labels and report for the market-news shadow log (market_news.py, docs/MARKET_MAP.md "Market news").

The window starts at the first full 15m bar after detection (`start_ms`, 0-15 min after it; `start_lag_min` in the
outcome): nothing before the item was seen counts. Its horizons are bar closes: `r1h` is the close of the 4th bar
after the start, so 1h to 1h15m after detection; likewise `r4h` and `r24h`. Returns are price returns in % from the
reference price logged at detection: negative = the market fell. `low4h` / `low24h` are the deepest low within the
first 16 / 96 bars of the window. The alt basket gives its median return and the share of alts down per horizon.

A failed BTC or ETH fetch (weight guard wait, 418/429, network) leaves the event for the next run, MAX_TRIES times,
then it is marked `failed` (the report counts it); a failed alt only drops out of the basket. The report groups by
class, one mean per UTC day first (a burst of headlines on one story is one event, not ten), and is descriptive: a
test of whether the tags predict anything goes through the trial register first."""

from __future__ import annotations

import json
import statistics
import time
from datetime import UTC, datetime
from typing import Any

HORIZONS_H = (1, 4, 24)
BAR_MS = 900_000
LABEL_AFTER_MS = 24 * 3_600_000 + 3 * BAR_MS   # the window's last bar has closed (start <= detection + 15 min)
BARS = 96        # 24h of 15m bars from the start; < 100 keeps each klines call at weight 1
LIMIT = 5        # events per run (each costs 2 + ALT_N klines calls, research class under the weight guard)
MAX_TRIES = 6    # runs (hourly) a BTC/ETH fetch may fail before the event is marked failed
MAJORS = (("btc", "BTC/USDT:USDT"), ("eth", "ETH/USDT:USDT"))


def window_start(ts_ms: int) -> int:
    """The open of the first 15m bar wholly after detection."""
    return ts_ms - ts_ms % BAR_MS + BAR_MS


def returns(p0: float, bars: list[list[float]]) -> dict[str, float | None]:
    """Price returns (%) from p0 at the close of bar h*4 of the window (see the module doc)."""
    out: dict[str, float | None] = {}
    for h in HORIZONS_H:
        k = h * 4
        out[f"r{h}h"] = round((bars[k - 1][4] / p0 - 1) * 100, 3) if p0 and len(bars) >= k else None
    return out


def lows(p0: float, bars: list[list[float]]) -> dict[str, float | None]:
    """The deepest low (%) from p0 within the window's first 4h and 24h (the move may come and go between closes)."""
    return {f"low{h}h": round((min(b[3] for b in bars[:h * 4]) / p0 - 1) * 100, 3) if p0 and len(bars) >= h * 4
            else None for h in (4, 24)}


def basket(per_alt: dict[str, dict[str, float | None]]) -> dict[str, Any]:
    """Alt breadth per horizon: the median return and the share (%) of alts down, over the alts with a value."""
    out: dict[str, Any] = {"n": len(per_alt)}
    for h in HORIZONS_H:
        xs = [x for r in per_alt.values() if (x := r.get(f"r{h}h")) is not None]
        out[f"r{h}h_median"] = round(statistics.median(xs), 3) if xs else None
        out[f"r{h}h_down_pct"] = round(sum(x < 0 for x in xs) / len(xs) * 100, 1) if xs else None
    return out


async def label(ex, journal, now_ms: int | None = None, limit: int = LIMIT) -> int:
    """Label every due event (up to `limit`); returns how many were finished (labelled or marked failed). Fetches
    first, then one journal unit (#79)."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    rows = journal.db.execute("SELECT id, ts_ms, ref, label_tries FROM market_news_events WHERE labelled_at IS NULL "
                              "AND ts_ms < ? ORDER BY id LIMIT ?", (now_ms - LABEL_AFTER_MS, limit)).fetchall()
    cache: dict[tuple[str, int], list[list[float]] | None] = {}

    async def bars(sym: str, since: int) -> list[list[float]] | None:
        if (sym, since) not in cache:
            try:
                cache[(sym, since)] = await ex.public.fetch_ohlcv(sym, "15m", since=since, limit=BARS)
            except Exception:  # noqa: BLE001 - the caller decides: retry (majors) or drop (an alt)
                cache[(sym, since)] = None
        return cache[(sym, since)]

    done, retry = [], []
    for rid, ts, raw, tries in rows:
        ref = json.loads(raw) if raw else None
        o: dict[str, Any] = {"no_data": True}
        if ref and ref.get("btc"):
            start = window_start(ts)
            o = {"start_ms": start, "start_lag_min": round((start - ts) / 60_000, 1)}
            for name, sym in MAJORS:
                b = await bars(sym, start)
                if b is None:
                    o = {"failed": f"{sym} klines unavailable after {tries + 1} tries"}
                    break
                o[name] = {**returns(ref.get(name) or 0, b), **lows(ref.get(name) or 0, b)}
            if "failed" not in o:
                per_alt = {s: returns(p0, b) for s, p0 in (ref.get("alts") or {}).items()
                           if (b := await bars(s, start)) is not None}
                o["alts"] = basket(per_alt)
            elif tries + 1 < MAX_TRIES:
                retry.append((tries + 1, rid))
                continue
        done.append((json.dumps(o), datetime.now(UTC).isoformat(timespec="seconds"), tries + 1, rid))
    if done or retry:
        def write(j) -> None:
            j.db.executemany("UPDATE market_news_events SET outcome=?, labelled_at=?, label_tries=? WHERE id=?", done)
            j.db.executemany("UPDATE market_news_events SET label_tries=? WHERE id=?", retry)
        await journal.write(write)
    return len(done)


def report(journal) -> dict[str, Any]:
    """Per class (and all): labelled events, distinct days, and per horizon the day-clustered mean of BTC, ETH and the
    alt basket's median return, with the share of days the alts fell. No titles: third-party text stays out."""
    rows = journal.db.execute("SELECT ts, classes, outcome FROM market_news_events WHERE outcome IS NOT NULL").fetchall()
    pending = journal.db.execute("SELECT COUNT(*) FROM market_news_events WHERE outcome IS NULL").fetchone()[0]
    groups: dict[str, list[tuple[str, dict]]] = {}
    failed = no_data = 0
    for ts, classes, raw in rows:
        o = json.loads(raw)
        failed, no_data = failed + ("failed" in o), no_data + bool(o.get("no_data"))
        if "failed" in o or o.get("no_data"):
            continue
        for c in ["all", *(classes or "").split(",")]:
            if c:
                groups.setdefault(c, []).append((ts[:10], o))
    out: dict[str, Any] = {"labelled": len(rows) - failed - no_data, "failed": failed, "no_data": no_data,
                           "pending": pending, "classes": {}}
    for c, items in sorted(groups.items()):
        res: dict[str, Any] = {"events": len(items), "days": len({d for d, _ in items})}
        for h in HORIZONS_H:
            res[f"{h}h"] = {k: _by_day(items, path) for k, path in (
                ("btc", ("btc", f"r{h}h")), ("eth", ("eth", f"r{h}h")), ("alts_median", ("alts", f"r{h}h_median")))}
        out["classes"][c] = res
    out["read"] = ("descriptive: compare each mean with the unconditional mean of the same horizon over the same weeks "
                   "(shortbot.lab); effective N = days; register the test in the trial register before acting on it")
    return out


def _by_day(items: list[tuple[str, dict]], path: tuple[str, str]) -> dict[str, Any] | None:
    by_day: dict[str, list[float]] = {}
    for day, o in items:
        v = (o.get(path[0]) or {}).get(path[1])
        if v is not None:
            by_day.setdefault(day, []).append(v)
    days = [statistics.fmean(v) for v in by_day.values()]
    if not days:
        return None
    m = statistics.fmean(days)
    se = statistics.stdev(days) / len(days) ** 0.5 if len(days) > 1 else None
    return {"days": len(days), "mean_pct": round(m, 3), "t_by_day": round(m / se, 2) if se else None,
            "down_days_pct": round(sum(d < 0 for d in days) / len(days) * 100, 1)}
