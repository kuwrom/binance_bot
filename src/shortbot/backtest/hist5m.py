"""5-minute perp klines for the trail5m study (docs/failures/ROUND4.md), cached in data/hist5m.db (never hist.db or
shortbot.db). Only data.binance.vision daily archive files (`futures/um/daily/klines/SYM/5m/SYM-5m-DAY.zip`, same
CSV as the 1h files, verified 2026-09-29), only for the (coin, day) pairs a study asks for, at most 8 concurrent
requests with a pause after each. No Binance REST API use. Resumable: every file is fetched once (table `files`;
a 404 for a day older than yesterday is recorded as n = -1 so it is not retried)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from .. import histdata_vision as V

log = logging.getLogger("shortbot.hist5m")
DB_PATH = "data/hist5m.db"
M5 = 300_000
MAX_CONCURRENCY = 8
SCHEMA = """
CREATE TABLE IF NOT EXISTS bars5 (symbol TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, qv REAL, n INTEGER,
    tbq REAL, PRIMARY KEY (symbol, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS files (key TEXT PRIMARY KEY, symbol TEXT, day TEXT, n INTEGER, fetched_ms INTEGER);
"""


def connect(db_path: str | Path = DB_PATH) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path))
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def raw_of(symbol: str) -> str:
    """'ACX/USDT:USDT' -> 'ACXUSDT'."""
    base, rest = symbol.split("/", 1)
    return base + rest.split(":", 1)[0]


def key5(raw: str, day: str) -> str:
    return f"{V.UM}/daily/klines/{raw}/5m/{raw}-5m-{day}.zip"


def days_between(t0: int, t1: int) -> list[str]:
    """UTC day strings covering [t0, t1]."""
    d, out = t0 // V.DAY * V.DAY, []
    while d <= t1:
        out.append(V.day_str(d))
        d += V.DAY
    return out


async def fetch(pairs: set[tuple[str, str]], db_path: str | Path = DB_PATH, concurrency: int = MAX_CONCURRENCY,
                pause_s: float = 0.1) -> dict[str, Any]:
    """Download the daily 5m kline files of the (raw symbol, YYYY-MM-DD) pairs not cached yet."""
    concurrency = min(concurrency, MAX_CONCURRENCY)
    db, t0 = connect(db_path), time.time()
    done = {k for (k,) in db.execute("SELECT key FROM files")}
    todo = sorted(p for p in pairs if key5(*p) not in done)
    stale = V.day_str(int(t0 * 1000) - V.DAY)            # yesterday's file may not be published yet
    f, sem, stat = V.Fetcher(concurrency), asyncio.Semaphore(concurrency), {"files": 0, "missing": 0, "rows": 0}

    async def one(raw: str, day: str) -> None:
        async with sem:
            text = await f.csv(key5(raw, day))
            await asyncio.sleep(pause_s)
        now = int(time.time() * 1000)
        if text is None:
            stat["missing"] += 1
            if day < stale:
                db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)", (key5(raw, day), raw, day, -1, now))
            return
        bars = V.parse_klines(text)
        db.executemany("INSERT OR REPLACE INTO bars5 VALUES (?,?,?,?,?,?,?,?,?)",
                       [(raw, b["ts"], b["o"], b["h"], b["l"], b["c"], b["qv"], b["n"], b["tbq"]) for b in bars])
        db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)", (key5(raw, day), raw, day, len(bars), now))
        stat["files"] += 1
        stat["rows"] += len(bars)
        if stat["files"] % 200 == 0:
            db.commit()
            log.info("5m files %d/%d (%.0fs)", stat["files"], len(todo), time.time() - t0)

    try:
        for k in range(0, len(todo), 400):                  # bounded batches keep memory and the pacing simple
            await asyncio.gather(*(one(r, d) for r, d in todo[k:k + 400]))
            db.commit()
    finally:
        await f.close()
        db.commit()
        db.close()
    stat.update(requested=len(todo), cached_before=len(pairs) - len(todo), seconds=round(time.time() - t0),
                http_requests=f.requests)
    return stat


def load(raw: str, db_path: str | Path = DB_PATH) -> list[dict[str, Any]]:
    """Every cached 5m bar of one coin, sorted: [{"ts","o","h","l","c","qv","n","tbq"}]."""
    db = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT ts,o,h,l,c,qv,n,tbq FROM bars5 WHERE symbol=? ORDER BY ts", (raw,)).fetchall()
    finally:
        db.close()
    return [dict(zip(("ts", "o", "h", "l", "c", "qv", "n", "tbq"), r, strict=True)) for r in rows]
