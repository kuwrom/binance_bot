"""Multi-year DAILY history for every USDT coin perp, delisted ones included (regime studies, docs/CARRY_REGIME.md).

Source: data.binance.vision monthly archives only (1d klines + fundingRate), so the build ends at the last published
month. Its own SQLite file (data/daily.db, never the guardian's shortbot.db). Read-only, resumable: every archive file
is fetched once (table `files`). Hourly 60-day history stays in histdata.py / data/hist.db.

    .venv/bin/python -m shortbot.histdaily build --since 2020-01    # log: data/logs/histdaily.log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import histdata_vision as V
from .histdata import _exchange_info, _public_ex, universe

log = logging.getLogger("shortbot.histdaily")
DB_PATH = "data/daily.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (key TEXT PRIMARY KEY, symbol TEXT, fetched_ms INTEGER, n INTEGER);
CREATE TABLE IF NOT EXISTS bars (symbol TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, qv REAL,
    PRIMARY KEY (symbol, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS funding (symbol TEXT, ts INTEGER, rate REAL, PRIMARY KEY (symbol, ts)) WITHOUT ROWID;
"""


def connect(db_path: str | Path = DB_PATH) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=30)
    db.executescript(SCHEMA)
    return db


def monthly_prefix(kind: str, raw: str) -> tuple[str, str]:
    """(S3 prefix, file stem) of a symbol's monthly archive: kind 'bars' (1d klines) or 'funding'."""
    if kind == "bars":
        return f"{V.UM}/monthly/klines/{raw}/1d/", f"{raw}-1d-"
    if kind == "funding":
        return f"{V.UM}/monthly/fundingRate/{raw}/", f"{raw}-fundingRate-"
    raise ValueError(kind)


def put(db, kind: str, raw: str, text: str) -> int:
    if kind == "bars":
        rows = [(raw, b["ts"], b["o"], b["h"], b["l"], b["c"], b["qv"]) for b in V.parse_klines(text)]
        db.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?)", rows)
    else:
        rows = [(raw, ts, rate) for ts, rate in V.parse_funding(text)]
        db.executemany("INSERT OR REPLACE INTO funding VALUES (?,?,?)", rows)
    return len(rows)


async def archive(f: V.Fetcher, db, raw: str, since: str) -> int:
    """Fetch every not-yet-cached monthly file of `raw` from month `since` (YYYY-MM). Returns files fetched."""
    done = {k for (k,) in db.execute("SELECT key FROM files WHERE symbol=?", (raw,))}
    n = 0
    for kind in ("bars", "funding"):
        prefix, stem = monthly_prefix(kind, raw)
        keys, _ = await f.list(prefix, marker=f"{prefix}{stem}{since}")
        todo = [k for k in keys if k.endswith(".zip") and k not in done]
        texts = await asyncio.gather(*(f.csv(k) for k in todo))      # the Fetcher's semaphore caps concurrency
        for key, text in zip(todo, texts):
            rows = put(db, kind, raw, text) if text else 0
            db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?)", (key, raw, int(time.time() * 1000), rows))
            n += 1
    db.commit()
    return n


async def build(since: str = "2020-01", db_path: str | Path = DB_PATH, concurrency: int = 8,
                symbols: list[str] | None = None) -> dict[str, Any]:
    t0 = time.time()
    db, f, ex = connect(db_path), V.Fetcher(concurrency), _public_ex(db_path)
    errors: dict[str, str] = {}
    try:
        raws = symbols or await universe(f, await _exchange_info(ex))
        sem, done = asyncio.Semaphore(concurrency), [0]

        async def one(raw: str) -> None:
            async with sem:
                try:
                    n = await archive(f, db, raw, since)
                except Exception as e:  # noqa: BLE001 - one coin failing must not stop the build
                    errors[raw] = f"{type(e).__name__}: {e}"[:160]
                    db.rollback()
                    n = -1
            done[0] += 1
            if done[0] % 25 == 0 or n < 0:
                log.info("[%d/%d] %s %s (%.0fs, %d http)", done[0], len(raws), raw,
                         errors.get(raw) or f"{n} files", time.time() - t0, f.requests)

        await asyncio.gather(*(one(r) for r in raws))
    finally:
        await f.close()
        await ex.close()
    out = summary(db)
    out.update(seconds=round(time.time() - t0), http_requests=f.requests, errors=len(errors),
               error_sample=dict(list(errors.items())[:5]))
    db.close()
    log.info("done: %s", json.dumps(out))
    return out


def summary(db) -> dict[str, Any]:
    b = db.execute("SELECT COUNT(DISTINCT symbol), COUNT(*), MIN(ts), MAX(ts) FROM bars").fetchone()
    fu = db.execute("SELECT COUNT(DISTINCT symbol), COUNT(*) FROM funding").fetchone()
    return {"coins": b[0], "bars": b[1], "first_day": V.day_str(b[2]) if b[2] else None,
            "last_day": V.day_str(b[3]) if b[3] else None, "funding_coins": fu[0], "funding_rows": fu[1]}


def load(db_path: str | Path = DB_PATH) -> dict[str, Any]:
    """{"bars": {sym: [(ts, o, h, l, c, qv), ...]}, "funding": {sym: [(ts, rate), ...]}} sorted by ts."""
    db = sqlite3.connect(db_path)
    bars: dict[str, list[tuple]] = {}
    for row in db.execute("SELECT symbol, ts, o, h, l, c, qv FROM bars ORDER BY symbol, ts"):
        bars.setdefault(row[0], []).append(row[1:])
    funding: dict[str, list[tuple]] = {}
    for sym, ts, rate in db.execute("SELECT symbol, ts, rate FROM funding ORDER BY symbol, ts"):
        funding.setdefault(sym, []).append((ts, rate))
    db.close()
    return {"bars": bars, "funding": funding}


def main() -> None:
    ap = argparse.ArgumentParser(prog="shortbot.histdaily")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--since", default="2020-01")
    b.add_argument("--db", default=DB_PATH)
    b.add_argument("--concurrency", type=int, default=8)
    b.add_argument("--symbols", nargs="*")
    sub.add_parser("summary").add_argument("--db", default=DB_PATH)
    args = ap.parse_args()
    Path("data/logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler("data/logs/histdaily.log"), logging.StreamHandler()])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.cmd == "build":
        print(json.dumps(asyncio.run(build(args.since, args.db, args.concurrency, args.symbols)), indent=1))
    else:
        print(json.dumps(summary(connect(args.db)), indent=1))


if __name__ == "__main__":
    main()
