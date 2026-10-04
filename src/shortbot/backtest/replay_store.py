"""Cache for the live-pipeline replay (task #29): 1-minute bars (perp, mark, premium, spot), derived hourly and daily
bars, 5-minute metrics, settled funding and point-in-time coin metadata, in ONE SQLite file under a scratch
directory (never data/: the journal's directory). Filled by replay_fetch.py; read by replay_exchange.py.

Bars are replay_bars tuples (ts, o, h, l, c, v, qv, n, tbv, tbq). Symbols are raw ("BTCUSDT"); spot bars are keyed
by the spot pair. Old inputs (data/hist.db, data/daily.db) are only ever opened read-only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import replay_bars as RB

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (kind TEXT, symbol TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, qv REAL,
    n INTEGER, tbv REAL, tbq REAL, PRIMARY KEY (kind, symbol, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS bars_h (kind TEXT, symbol TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,
    qv REAL, n INTEGER, tbv REAL, tbq REAL, PRIMARY KEY (kind, symbol, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS bars_d (symbol TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, qv REAL,
    n INTEGER, tbv REAL, tbq REAL, src TEXT, PRIMARY KEY (symbol, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS metrics (symbol TEXT, ts INTEGER, oi REAL, oi_val REAL, ls_top REAL, ls_top_acct REAL,
    ls_global REAL, taker REAL, PRIMARY KEY (symbol, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS funding (symbol TEXT, ts INTEGER, rate REAL, PRIMARY KEY (symbol, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (symbol TEXT PRIMARY KEY, info TEXT);
CREATE TABLE IF NOT EXISTS files (key TEXT PRIMARY KEY, n INTEGER, fetched_ms INTEGER);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""
COLS = "ts, o, h, l, c, v, qv, n, tbv, tbq"
METRIC_FIELDS = ("oi", "oi_val", "ls_top", "ls_top_acct", "ls_global", "taker")


def parse_klines(text: str) -> list[tuple]:
    """Kline CSV (header optional; ms or µs open times) -> replay tuples with base AND quote volumes."""
    out = []
    for line in text.splitlines():
        if not line or not line[0].isdigit():
            continue
        r = line.split(",")
        ts = int(r[0])
        if ts > 10 ** 14:                      # spot files since 2025 use microseconds
            ts //= 1000
        out.append((ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[7]),
                    int(float(r[8])), float(r[9]), float(r[10])))
    return sorted(out)


def ro_connect(path: Path | str) -> sqlite3.Connection:
    """Read-only connection to an input DB (falls back to immutable when the WAL index can't be opened)."""
    p = Path(path).resolve()
    try:
        db = sqlite3.connect(f"{p.as_uri()}?mode=ro", uri=True)
        db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        return db
    except sqlite3.OperationalError:
        return sqlite3.connect(f"{p.as_uri()}?mode=ro&immutable=1", uri=True)


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30)
        self.db.executescript(SCHEMA)

    # ------------------------------------------------------------ writes
    def put_bars(self, kind: str, symbol: str, bars: list[tuple], table: str = "bars") -> int:
        self.db.executemany(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            [(kind, symbol, *b) for b in bars])
        return len(bars)

    def put_daily(self, symbol: str, bars: list[tuple], src: str) -> None:
        self.db.executemany("INSERT OR REPLACE INTO bars_d VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            [(symbol, *b, src) for b in bars])

    def put_metrics(self, symbol: str, rows: list[dict[str, Any]]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO metrics VALUES (?,?,?,?,?,?,?,?)",
                            [(symbol, r["ts"], *(r.get(k) for k in METRIC_FIELDS)) for r in rows])

    def put_funding(self, symbol: str, rows: list[tuple[int, float]]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO funding VALUES (?,?,?)", [(symbol, t, r) for t, r in rows])

    def put_meta(self, meta: dict[str, dict[str, Any]]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [(s, json.dumps(m)) for s, m in meta.items()])
        self.db.commit()

    def set(self, k: str, v: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))
        self.db.commit()

    def get(self, k: str) -> Any:
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else None

    def have(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM files WHERE key=?", (key,)).fetchone() is not None

    def done(self, key: str, n: int, now_ms: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?)", (key, n, now_ms))
        self.db.commit()

    def derive_hourly(self, kind: str, symbols: list[str] | None = None) -> int:
        """bars_h from the 1m bars (whole hours only)."""
        syms = symbols or [r[0] for r in self.db.execute("SELECT DISTINCT symbol FROM bars WHERE kind=?", (kind,))]
        n = 0
        for s in syms:
            hours = RB.group(self.fine(kind, s, 0, 2 ** 62), RB.HOUR)
            n += self.put_bars(kind, s, hours, "bars_h")
        self.db.commit()
        return n

    def derive_five(self, kind: str, symbols: list[str] | None = None) -> int:
        """`<kind>5`: 5m bars from the 1m ones (label and delay windows read 5x fewer rows)."""
        syms = symbols or [r[0] for r in self.db.execute("SELECT DISTINCT symbol FROM bars WHERE kind=?", (kind,))]
        n = sum(self.put_bars(f"{kind}5", s, RB.group(self.fine(kind, s, 0, 2 ** 62), 5 * RB.MIN)) for s in syms)
        self.db.commit()
        self.set("derived", sorted({*(self.get("derived") or []), f"{kind}5"}))
        return n

    # ------------------------------------------------------------ reads
    def fine(self, kind: str, symbol: str, a: int, b: int) -> list[tuple]:
        return self.db.execute(f"SELECT {COLS} FROM bars WHERE kind=? AND symbol=? AND ts>=? AND ts<? ORDER BY ts",
                               (kind, symbol, a, b)).fetchall()

    def hourly(self, kind: str, symbol: str, a: int, b: int) -> list[tuple]:
        return self.db.execute(f"SELECT {COLS} FROM bars_h WHERE kind=? AND symbol=? AND ts>=? AND ts<? ORDER BY ts",
                               (kind, symbol, a, b)).fetchall()

    def last_close(self, kind: str, symbol: str, t_ms: int) -> float | None:
        """Close of the last 1m bar closed by t."""
        row = self.db.execute("SELECT c FROM bars WHERE kind=? AND symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                              (kind, symbol, t_ms - RB.MIN)).fetchone()
        return row[0] if row else None

    def daily(self, symbol: str, a: int, b: int) -> list[tuple]:
        return self.db.execute(f"SELECT {COLS} FROM bars_d WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts",
                               (symbol, a, b)).fetchall()

    def metrics(self, symbol: str, a: int, b: int) -> list[dict[str, Any]]:
        cur = self.db.execute("SELECT ts, oi, oi_val, ls_top, ls_top_acct, ls_global, taker FROM metrics WHERE symbol=?"
                              " AND ts>=? AND ts<? ORDER BY ts", (symbol, a, b))
        return [dict(zip(("ts", *METRIC_FIELDS), r, strict=True)) for r in cur.fetchall()]

    def funding(self, symbol: str, a: int, b: int) -> list[tuple[int, float]]:
        return self.db.execute("SELECT ts, rate FROM funding WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts",
                               (symbol, a, b)).fetchall()

    def meta(self) -> dict[str, dict[str, Any]]:
        return {s: json.loads(m) for s, m in self.db.execute("SELECT symbol, info FROM meta")}


def daily_from_inputs(store: Store, symbols: list[str], hist_db: Path | str | None, daily_db: Path | str | None,
                      fine_from_ms: int) -> int:
    """bars_d for the 200-day lookback: data/daily.db (oldest), data/hist.db hourly bars (next), then the cached 1m
    bars for whole days from `fine_from_ms` on (best). Base volume is estimated as quote volume / close where only
    quote volume exists (the controller's vol_cv multiplies it back)."""
    n = 0
    est = lambda o, h, l, c, qv: (o, h, l, c, qv / c if c else 0.0, qv)   # noqa: E731,E741
    if daily_db and Path(daily_db).exists():
        d = ro_connect(daily_db)
        for s in symbols:
            rows = d.execute("SELECT ts, o, h, l, c, qv FROM bars WHERE symbol=? ORDER BY ts", (s,)).fetchall()
            store.put_daily(s, [(t, *est(o, h, lo, c, qv), 0, 0.0, 0.0) for t, o, h, lo, c, qv in rows], "daily.db")
            n += len(rows)
        d.close()
    if hist_db and Path(hist_db).exists():
        hd = ro_connect(hist_db)
        for s in symbols:
            rows = hd.execute("SELECT ts, o, h, l, c, qv, n, tbq FROM bars WHERE symbol=? AND kind='perp' AND ts<? "
                              "ORDER BY ts", (s, fine_from_ms)).fetchall()
            hours = [(t, *est(o, h, lo, c, qv), int(k or 0), (tbq / c if c else 0.0), tbq or 0.0)
                     for t, o, h, lo, c, qv, k, tbq in rows]
            days = [b for b in RB.group(hours, RB.DAY) if b[RB.TS] + RB.DAY <= fine_from_ms]
            store.put_daily(s, days, "hist.db")
            n += len(days)
        hd.close()
    for s in symbols:
        hours = store.hourly("perp", s, fine_from_ms, 2 ** 62)
        days = RB.group(hours, RB.DAY)
        whole = [b for b in days if sum(1 for h in hours if h[RB.TS] // RB.DAY * RB.DAY == b[RB.TS]) == 24]
        store.put_daily(s, whole, "1m")
        n += len(whole)
    store.db.commit()
    return n
