"""Labels and report for the market-news shadow log (market_news.py, docs/MARKET_MAP.md "Market news").

Once an event is LABEL_AFTER_MS old, the research job fills its outcome from 15m klines: BTC's and ETH's price return
at +1h/+4h/+24h from the reference price logged at detection, their lowest low within 4h and 24h, and the alt basket's
median return and share of alts down at each horizon (breadth). Returns are price returns in %: negative = the market
fell. The report groups them by class, one mean per UTC day first (a burst of headlines on one story is one event,
not ten), and is descriptive: a test of whether the tags predict anything goes through the trial register first."""

from __future__ import annotations

import json
import statistics
import time
from datetime import UTC, datetime
from typing import Any

HORIZONS_H = (1, 4, 24)
BAR_MS = 900_000
LABEL_AFTER_MS = 24 * 3_600_000 + 2 * BAR_MS
BARS = 97        # 24h of 15m bars from the bar holding the event; < 100 keeps each klines call at weight 1
LIMIT = 5        # events per run (each costs 2 + ALT_N klines calls, research class under the weight guard)


def returns(p0: float, bars: list[list[float]]) -> dict[str, float | None]:
    """+h price returns (%) from p0 on 15m bars starting at the bar holding the event (close of bar 4h-1)."""
    out: dict[str, float | None] = {}
    for h in HORIZONS_H:
        k = h * 4
        out[f"r{h}h"] = round((bars[k - 1][4] / p0 - 1) * 100, 3) if p0 and len(bars) >= k else None
    return out


def lows(p0: float, bars: list[list[float]]) -> dict[str, float | None]:
    """The deepest drop (%) from p0 within 4h and within 24h (the move may come and go between the horizons)."""
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
    """Fill the outcome of every due event (up to `limit`). Fetches first, then one journal unit (#79)."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    rows = journal.db.execute("SELECT id, ts_ms, ref FROM market_news_events WHERE labelled_at IS NULL AND ts_ms < ? "
                              "ORDER BY id LIMIT ?", (now_ms - LABEL_AFTER_MS, limit)).fetchall()
    cache: dict[tuple[str, int], list[list[float]]] = {}

    async def bars(sym: str, since: int) -> list[list[float]]:
        if (sym, since) not in cache:
            try:
                cache[(sym, since)] = await ex.public.fetch_ohlcv(sym, "15m", since=since, limit=BARS)
            except Exception:  # noqa: BLE001 - a delisted alt drops out of the basket; the rest still label
                cache[(sym, since)] = []
        return cache[(sym, since)]

    done = []
    for rid, ts, raw in rows:
        ref = json.loads(raw) if raw else None
        o: dict[str, Any] = {"no_data": True}
        if ref and ref.get("btc"):
            since = ts - ts % BAR_MS
            o = {}
            for name, sym in (("btc", "BTC/USDT:USDT"), ("eth", "ETH/USDT:USDT")):
                b = await bars(sym, since)
                o[name] = {**returns(ref.get(name) or 0, b), **lows(ref.get(name) or 0, b)}
            per_alt = {s: returns(p0, await bars(s, since)) for s, p0 in (ref.get("alts") or {}).items()}
            o["alts"] = basket(per_alt)
        done.append((json.dumps(o), datetime.now(UTC).isoformat(timespec="seconds"), rid))
    if done:
        await journal.write(lambda j: j.db.executemany(
            "UPDATE market_news_events SET outcome=?, labelled_at=? WHERE id=?", done))
    return len(done)


def report(journal) -> dict[str, Any]:
    """Per class (and all): labelled events, distinct days, and per horizon the day-clustered mean of BTC, ETH and the
    alt basket's median return, with the share of days the alts fell. No titles: third-party text stays out."""
    rows = journal.db.execute("SELECT ts, classes, outcome FROM market_news_events WHERE outcome IS NOT NULL").fetchall()
    pending = journal.db.execute("SELECT COUNT(*) FROM market_news_events WHERE outcome IS NULL").fetchone()[0]
    groups: dict[str, list[tuple[str, dict]]] = {}
    for ts, classes, raw in rows:
        o = json.loads(raw)
        if o.get("no_data"):
            continue
        for c in ["all", *(classes or "").split(",")]:
            if c:
                groups.setdefault(c, []).append((ts[:10], o))
    out: dict[str, Any] = {"labelled": len(rows), "pending": pending, "classes": {}}
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
