"""Readers of the stream store (market/streams.py): any process, and a missing file is a miss (None / [] / {}).

Every reader is bounded by "now" (the market clock): rows written after it are a replay's future (#29). Live,
nothing in the store is newer than now.

    candles(symbol, tf, n)       -> [[ts, o, h, l, c, v], ...]       or None (not fresh, gapped, too short)
    flow_candles(symbol, tf, n)  -> [[ts, o, h, l, c, v, tbv], ...]  taker-buy base volume, like exchange.flow_klines

Streamed timeframes are read as stored. A derived timeframe (DERIVED, e.g. 30m) is built from its base timeframe's
bars: open of the first, max high, min low, close of the last, summed volumes. Checked against Binance's own klines
on 2026-10-02 (BTC, SOL, BAT; 5m / 15m / 30m / 1h from 1m): 84 of 84 bars identical, taker-buy volume included.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .. import clock as market_clock  # market time (a replay pins it)
from .streams import DB_PATH, DERIVED, TF_MS, raw_id


def path_for(db_path: str | Path | None) -> Path | None:
    """The store beside the journal (data/shortbot.db -> data/streams.db); None for an in-memory journal (tests)."""
    if not db_path or str(db_path) == ":memory:":
        return None
    return Path(db_path).parent / "streams.db"


def _ro(path: str | Path) -> sqlite3.Connection | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None


def _now(now_ms: int | None) -> int:
    return now_ms if now_ms is not None else market_clock.now_ms()


def _window(symbol: str, tf: str, n: int, flow: bool, path: str | Path, now_ms: int | None,
            max_age_s: float | None) -> list[list[float]] | None:
    """The last n stored bars of a streamed timeframe, closed plus the forming one, when fresh: the last closed bar
    is at most 1 bar old and the window has no gap. With max_age_s, the symbol's kline stream must also have updated
    within it: complete-looking bars from a socket that went quiet are stale (the forming bar stopped)."""
    db = _ro(path)
    if db is None:
        return None
    now, sym = _now(now_ms), raw_id(symbol)
    cols = "ts, o, h, l, c, v, tbv, closed" if flow else "ts, o, h, l, c, v, closed"
    try:
        rows = db.execute(f"SELECT {cols} FROM candles WHERE symbol=? AND tf=? AND ts <= ? ORDER BY ts DESC LIMIT ?",
                          (sym, tf, now, n + 2)).fetchall()
        seen = db.execute("SELECT ts FROM fresh WHERE symbol=? AND stream=?", (sym, f"kline_{tf}")).fetchone() \
            if max_age_s is not None else None
    except sqlite3.Error:
        return None
    finally:
        db.close()
    if max_age_s is not None and (seen is None or not now - max_age_s * 1000 <= seen[0] <= now):
        return None
    step = TF_MS[tf]
    if any(r[-1] and r[0] + step > now for r in rows):
        return None                                                  # closed after now: a replay's future
    closed = [r for r in rows if r[-1]]
    if not closed or closed[0][0] < now // step * step - 2 * step:   # last closed bar more than 1 bar old
        return None
    forming = [r for r in rows if not r[-1] and r[0] > closed[0][0]]
    if len(forming) > 1 or (forming and forming[0][0] != closed[0][0] + step):
        return None                                                  # a missed close: that bar is incomplete
    window = list(reversed(forming + closed))[-n:]
    if len(window) < n or any(b[0] - a[0] != step for a, b in zip(window, window[1:])):
        return None                                                  # too short, or a gap inside the window
    if flow and any(r[6] is None for r in window):
        return None                                                  # a bar without taker volume (old backfill)
    return [list(r[:-1]) for r in window]


def aggregate(bars: list[list[float]], tf: str, n: int) -> list[list[float]] | None:
    """Base bars (oldest first, the forming one last) -> the last n bars of `tf`. A leading bucket that starts before
    the window is dropped; the last bucket may be the forming one, as in REST."""
    step = TF_MS[tf]
    buckets: dict[int, list[list[float]]] = {}
    for b in bars:
        buckets.setdefault(int(b[0]) // step * step, []).append(b)
    k = step // TF_MS[DERIVED[tf]]
    keys = sorted(buckets)
    if keys and len(buckets[keys[0]]) < k:
        keys = keys[1:]
    out = []
    for t in keys:
        g = buckets[t]
        row = [t, g[0][1], max(x[2] for x in g), min(x[3] for x in g), g[-1][4], sum(x[5] for x in g)]
        if len(g[0]) > 6:
            row.append(sum(x[6] for x in g))
        out.append(row)
    if any(len(buckets[t]) < k for t in keys[:-1]):
        return None
    return out[-n:] if len(out) >= n else None


def _read(symbol: str, tf: str, n: int, flow: bool, path: str | Path, now_ms: int | None,
          max_age_s: float | None) -> list[list[float]] | None:
    if n < 1 or tf not in TF_MS:
        return None
    base = DERIVED.get(tf)
    if base is None:
        return _window(symbol, tf, n, flow, path, now_ms, max_age_s)
    k = TF_MS[tf] // TF_MS[base]
    bars = _window(symbol, base, n * k + k, flow, path, now_ms, max_age_s)
    return aggregate(bars, tf, n) if bars else None


def candles(symbol: str, tf: str, n: int, path: str | Path = DB_PATH, now_ms: int | None = None,
            max_age_s: float | None = None) -> list[list[float]] | None:
    """The last n bars [ts, o, h, l, c, v], closed plus the forming one (like REST), when fresh (see _window).
    Otherwise None, and the caller falls back to REST."""
    return _read(symbol, tf, n, False, path, now_ms, max_age_s)


def flow_candles(symbol: str, tf: str, n: int, path: str | Path = DB_PATH, now_ms: int | None = None,
                 max_age_s: float | None = None) -> list[list[float]] | None:
    """The last n bars [ts, o, h, l, c, v, taker_buy_base_volume] (exchange.flow_klines' shape) when fresh and every
    bar carries its taker volume; otherwise None (REST)."""
    return _read(symbol, tf, n, True, path, now_ms, max_age_s)


def fresh_symbols(stream: str, max_age_s: float, path: str | Path = DB_PATH, now_ms: int | None = None) -> list[str]:
    """Symbols whose last `stream` update is at most max_age_s old: what the collector is streaming right now."""
    db = _ro(path)
    if db is None:
        return []
    now = _now(now_ms)
    try:
        return [s for (s,) in db.execute("SELECT symbol FROM fresh WHERE stream=? AND ts BETWEEN ? AND ? "
                                         "ORDER BY symbol", (stream, now - max_age_s * 1000, now))]
    except sqlite3.Error:   # e.g. the collector created the file but not its tables yet: a miss, as in _window
        return []
    finally:
        db.close()


def latest(kind: str, path: str | Path = DB_PATH, max_age_s: float = 120,
           now_ms: int | None = None) -> dict[str, dict[str, Any]]:
    """{symbol: data} of the ticker / mark rows younger than max_age_s ({} when the file is missing)."""
    db = _ro(path)
    if db is None:
        return {}
    now = _now(now_ms)
    try:
        return {s: json.loads(d) for s, d in db.execute("SELECT symbol, data FROM latest WHERE kind=? AND ts BETWEEN "
                                                        "? AND ?", (kind, now - max_age_s * 1000, now))}
    except sqlite3.Error:   # a store without its tables yet (see fresh_symbols)
        return {}
    finally:
        db.close()
