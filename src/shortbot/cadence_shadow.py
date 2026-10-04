"""Forward check of a per-rule tier-1 cadence (task #40, docs/SCANNING.md "Cadence"). No deep scores, no alerts.

The cadence study kept tier 1 at 15 minutes (pre-registered, pooled over all rules). Post hoc, volume_surge and
lower_high_after_pump passed every threshold even at 1 minute. That is a hypothesis, so it is checked on live data:
- every minute (guardian scan lane), for every coin the collector streams, the two rules are evaluated on the windows
  ending at each 1m close since the last run;
- definitions are the study's (backtest/cadence_study.py), on the stream store's 1m bars;
- the minutes where a rule's raw condition holds are logged to data/research/cadence_shadow.db.
`--report` (after ~2 weeks) applies the study's thresholds per rule, with the 15-minute grid taken from the same
log (its minutes divisible by 15):
- median lead over 15 min >= 5 min;
- transients <= 25%;
- p95 new episodes per 15 min <= 30.

Needs 30 h of 1m history (29 prior hours + the last one) before volume_surge can be evaluated; streams.KEEP["1m"]
holds that.

    .venv/bin/python -m shortbot.cadence_shadow --report
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import numpy as np

from .backtest import cadence_study as CS
from .market import stream_read as R

MIN = 60_000
HISTORY = 1800 + 60          # 30 aligned hours of 1m bars, plus margin
MAX_CATCHUP = 30             # minutes evaluated per run at most (a stalled lane loses the oldest, logged)
RULES = ("volume_surge", "lower_high_after_pump")
SCHEMA = """
CREATE TABLE IF NOT EXISTS flags (minute INTEGER NOT NULL, symbol TEXT NOT NULL, rule TEXT NOT NULL,
    value REAL, PRIMARY KEY (minute, symbol, rule)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def db_path() -> Path:
    from . import provenance as P
    return P.data_dir() / "research" / "cadence_shadow.db"


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path or db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(p), timeout=15)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def minute_bars(store: Path | str, symbol: str, upto_minute: int, n: int = HISTORY) -> tuple[np.ndarray, ...] | None:
    """The last n closed 1m bars ending at `upto_minute` (minute index = open time // 60 s) on a dense minute grid:
    (minutes, close, high, quote volume); gaps forward-filled, zero volume. None without enough history."""
    db = R._ro(store)
    if db is None:
        return None
    try:
        rows = db.execute("SELECT ts, h, c, q FROM candles WHERE symbol=? AND tf='1m' AND closed=1 AND ts BETWEEN ? "
                          "AND ? ORDER BY ts", (symbol, (upto_minute - n + 1) * MIN, upto_minute * MIN)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    if not rows or rows[0][0] // MIN > upto_minute - n + 60 or rows[-1][0] // MIN != upto_minute:
        return None                                   # not enough history, or the last minute isn't in yet
    a = np.array(rows, dtype=float)
    first = int(a[0, 0]) // MIN
    idx = (a[:, 0] // MIN).astype(int) - first
    m = upto_minute - first + 1
    have = np.zeros(m, dtype=int)
    have[idx] = idx
    pick = np.maximum.accumulate(have)
    c, q = np.zeros(m), np.zeros(m)
    c[idx], q[idx] = a[:, 2], np.nan_to_num(a[:, 3])
    c = c[pick]                                       # gaps: the last close (as the study's grid)
    h = c.copy()
    h[idx] = a[:, 1]
    return first + np.arange(m), c, h, q


def evaluate(store: Path | str, symbol: str, minutes: list[int]) -> list[tuple[int, str, float]]:
    """(minute, rule, value) for each evaluation minute where a rule's raw condition holds."""
    got = minute_bars(store, symbol, max(minutes))
    if got is None:
        return []
    mins, c, h, q = got
    vz, lh = CS.volume_z(q), CS.lower_high(c, h)
    pos = {int(x): i for i, x in enumerate(mins)}
    out = []
    for m in minutes:
        i = pos.get(m)
        if i is None:
            continue
        if not np.isnan(vz[i]) and vz[i] >= CS.Z:
            out.append((m, "volume_surge", round(float(vz[i]), 2)))
        if lh[i]:
            out.append((m, "lower_high_after_pump", 1.0))
    return out


def run(store: Path | str, now_ms: int, path: Path | str | None = None) -> dict[str, Any]:
    """Evaluate every closed minute since the last run (at most MAX_CATCHUP) for every coin streaming 1m bars."""
    db = connect(path)
    try:
        last_closed = now_ms // MIN - 1
        r = db.execute("SELECT value FROM meta WHERE key='last_minute'").fetchone()
        start = max(int(r[0]) + 1 if r else last_closed, last_closed - MAX_CATCHUP + 1)
        minutes = list(range(start, last_closed + 1))
        if not minutes:
            return {"minutes": 0, "flags": 0}
        coins = R.fresh_symbols("kline_1m", 90, store, now_ms)
        rows = [(m, s, rule, v) for s in coins for m, rule, v in evaluate(store, s, minutes)]
        with db:
            db.executemany("INSERT OR REPLACE INTO flags VALUES (?,?,?,?)", rows)
            db.execute("INSERT OR REPLACE INTO meta VALUES ('last_minute', ?)", (str(last_closed),))
        return {"minutes": len(minutes), "coins": len(coins), "flags": len(rows)}
    finally:
        db.close()


def report(path: Path | str | None = None) -> dict[str, Any]:
    """The study's comparison on the logged minutes: per rule, the 1-minute cadence against the 15-minute grid."""
    db = connect(path)
    try:
        rows = db.execute("SELECT symbol, rule, minute FROM flags ORDER BY symbol, rule, minute").fetchall()
        span = db.execute("SELECT MIN(minute), MAX(minute) FROM flags").fetchone()
    finally:
        db.close()
    if not rows:
        return {"flags": 0}
    by: dict[tuple[str, str], list[int]] = {}
    for s, rule, m in rows:
        by.setdefault((s, rule), []).append(m)
    out: dict[str, Any] = {"days": round((span[1] - span[0]) / 1440, 1), "rules": {}}
    starts: dict[str, dict[int, list[int]]] = {r: {1: [], 15: []} for r in RULES}
    leads: dict[str, list[int]] = {r: [] for r in RULES}
    trans: dict[str, list[int]] = {r: [0, 0] for r in RULES}
    for (_s, rule), mins in by.items():
        lo = min(mins)
        grid = np.arange(lo, max(mins) + 1)
        flags = np.zeros(len(grid), dtype=bool)
        flags[np.array(mins) - lo] = True
        new = rule == "lower_high_after_pump"
        eps = {cd: CS.episodes(flags, grid, cd, new_only=new) for cd in (1, 15)}
        for cd in (1, 15):
            starts[rule][cd].extend(eps[cd].tolist())
        cmp = CS.compare({1: eps[1], 5: eps[1], 15: eps[15]})[1]
        leads[rule].extend(cmp["leads"])
        trans[rule][0] += cmp["transients"]
        trans[rule][1] += cmp["n"]
    windows = int((span[1] - span[0]) // 15) + 1
    for rule in RULES:
        st = np.array(starts[rule][1])
        w = np.bincount(((st - span[0]) // 15).astype(int), minlength=windows) if len(st) else np.zeros(1)
        lead = float(np.median(leads[rule])) if leads[rule] else 0.0
        tr = trans[rule][0] / max(trans[rule][1], 1)
        out["rules"][rule] = {"episodes_1m": len(st), "episodes_15m": len(starts[rule][15]), "lead_median": lead,
                              "transients": round(tr, 3), "p95_new_per_15m": float(np.percentile(w, 95)),
                              "passes": lead >= 5 and tr <= 0.25 and float(np.percentile(w, 95)) <= 30}
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="shortbot.cadence_shadow")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--db", type=Path)
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if a.report:
        print(json.dumps(report(a.db), indent=1))


if __name__ == "__main__":
    main()
