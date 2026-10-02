"""Phase 0 of the long build (docs/LONG_BUILD.md): the forward test of docs/LONG_PLAYBOOK.md as shadow trades.
It places no orders and writes nothing to the journal.

Once a day after the 00:00 UTC close (a guardian research-lane job):
1. Fetch the closed daily bars (HISTORY_D days, with quote volume) of the FETCH_TOP perps by 24h quote volume, plus
   BTC. That is about FETCH_TOP calls at weight 2.
2. Evaluate gate G, L2 (every day) and L1 (Sundays) with the backtest's own code (backtest/long_studies.py), so the
   shadow trades exactly the tested rules. The universe is the same: top 50 by 30-day volume, >= $5M/day, >= 60 days.
3. Open one shadow trade per signal at that close, with its time exit and catastrophic stop:
   - L1: 7 days, 3x ATR(14) under entry;
   - L2: 5 days, under the 20-day low or 3x ATR, whichever is lower.
   Signals while G is off are logged with gate=0 and don't count for acceptance.
4. Close the trades whose exit day has come or whose stop a daily low touched (at the stop, or at the open if it
   gapped through). Label:
   - the price return;
   - the funding the long paid (one fundingRate call per closed trade);
   - the equal-weight return of the entry day's universe over the same days (price only);
   - excess = price return - benchmark - 10 bp - funding paid. The benchmark's own funding is not netted, a small
     conservative bias, since longs in the benchmark pay it too.
Catch-up: each run evaluates every day since the last run, so a missed day loses nothing. The first run starts at
FORWARD_START, the day after daily.db ends (2026-08-31). The rules were fixed (2026-10-02) without looking at any
later daily data, so September onward is out of sample. Fetching today's top FETCH_TOP by 24h volume (wider than
the top-50 universe) keeps the look-ahead in that backfill small.
Store: data/research/long_shadow.db (its own file). Acceptance rules: LONG_PLAYBOOK.md "Forward test".
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .backtest import long_studies as L
from .backtest.carry_regime import DAY
from .backtest.xs_factors import Panel

log = logging.getLogger("shortbot.long_shadow")
FETCH_TOP, HISTORY_D, CONCURRENCY = 150, 130, 4
FORWARD_START = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp() * 1000)
L1_LOOKBACK, L1_HOLD, L2_HOLD, ATR_STOP = 28, 7, 5, 3.0
SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow (id INTEGER PRIMARY KEY, setup TEXT NOT NULL, symbol TEXT NOT NULL,
    entry_day INTEGER NOT NULL, entry REAL NOT NULL, stop REAL NOT NULL, exit_day INTEGER NOT NULL, gate INTEGER,
    universe TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', closed_day INTEGER, exit REAL, reason TEXT,
    ret REAL, funding REAL, bench REAL, excess REAL, UNIQUE (setup, symbol, entry_day));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def db_path() -> Path:
    from . import provenance as P
    return P.data_dir() / "research" / "long_shadow.db"


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path or db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(p), timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def _sunday(d: int) -> bool:
    return datetime.fromtimestamp(d / 1000, UTC).weekday() == 6


def signals(st: L.Study, d: int, bo_days: list[tuple[int, str]] | None = None) -> list[dict[str, Any]]:
    """The playbook's signals at close d: [{setup, symbol, entry, stop, exit_day, gate}]. bo_days: breakout_days(st),
    computed once by a caller evaluating many days."""
    gate = st.gate(L.PRIMARY_GATE, d)
    out = []
    if _sunday(d) and (names := L.rs_names(st, d)):
        for s in L.rs_picks(st, d, L1_LOOKBACK, names):
            out.append(_trade(st, "L1", s, d, L1_HOLD, gate))
    for day, s in (bo_days if bo_days is not None else L.breakout_days(st)):
        if day == d:
            low20 = min(st.p.bars[s][d - k * DAY][3] for k in range(1, 21))
            out.append(_trade(st, "L2", s, d, L2_HOLD, gate, floor=low20))
    return [t for t in out if t]


def _trade(st: L.Study, setup: str, s: str, d: int, hold: int, gate: bool | None,
           floor: float | None = None) -> dict[str, Any] | None:
    entry, atr = st.p.bars[s][d][4], L._atr(st.p.bars[s], d)
    if atr is None:
        return None
    stop = entry - ATR_STOP * atr
    stop = min(stop, floor) if floor is not None else stop
    return {"setup": setup, "symbol": s, "entry_day": d, "entry": entry, "stop": max(stop, 0.0),
            "exit_day": d + hold * DAY, "gate": None if gate is None else int(gate), "universe": st.uni(d)}


def record(db: sqlite3.Connection, trades: list[dict[str, Any]]) -> int:
    with db:
        n = db.total_changes
        db.executemany("INSERT OR IGNORE INTO shadow (setup, symbol, entry_day, entry, stop, exit_day, gate, universe)"
                       " VALUES (?,?,?,?,?,?,?,?)", [(t["setup"], t["symbol"], t["entry_day"], t["entry"], t["stop"],
                                                       t["exit_day"], t["gate"], ",".join(t["universe"]))
                                                      for t in trades])
        return db.total_changes - n


def exits(db: sqlite3.Connection, p: Panel, today: int) -> list[dict[str, Any]]:
    """Open trades that close by `today`: (row, closed_day, exit price, reason). Stops are checked day by day."""
    out = []
    for r in db.execute("SELECT * FROM shadow WHERE status='open'"):
        bars = p.bars.get(r["symbol"], {})
        d = r["entry_day"] + DAY
        while d <= min(today, r["exit_day"]):
            b = bars.get(d)
            if b is None:
                if d > max(bars, default=0):                         # stopped trading: its last close (delivery)
                    last = max(bars, default=0)
                    if last > r["entry_day"]:
                        out.append({"row": dict(r), "day": last, "exit": bars[last][4], "reason": "delisted"})
                    break
                d += DAY
                continue
            if b[3] <= r["stop"]:
                out.append({"row": dict(r), "day": d, "exit": min(r["stop"], b[1]), "reason": "stop"})
                break
            if d >= r["exit_day"]:
                out.append({"row": dict(r), "day": d, "exit": b[4], "reason": "time"})
                break
            d += DAY
    return out


def bench(p: Panel, names: list[str], d0: int, d1: int) -> float | None:
    rs = [p.bars[s][d1][4] / p.bars[s][d0][4] - 1 for s in names
          if s in p.bars and d0 in p.bars[s] and d1 in p.bars[s]]
    return statistics.fmean(rs) if rs else None


def close(db: sqlite3.Connection, x: dict[str, Any], p: Panel, funding: float) -> None:
    r = x["row"]
    ret = x["exit"] / r["entry"] - 1
    b = bench(p, r["universe"].split(","), r["entry_day"], x["day"])
    excess = None if b is None else ret - b - L.COST - funding
    with db:
        db.execute("UPDATE shadow SET status='closed', closed_day=?, exit=?, reason=?, ret=?, funding=?, bench=?, "
                   "excess=? WHERE id=?", (x["day"], x["exit"], x["reason"], ret, funding, b, excess, r["id"]))


def summary(db: sqlite3.Connection) -> dict[str, Any]:
    """Per setup, gate-on closed trades: n, mean excess, and how many are open (for the acceptance check)."""
    out = {}
    for setup in ("L1", "L2"):
        xs = [r[0] for r in db.execute("SELECT excess FROM shadow WHERE setup=? AND status='closed' AND gate=1 AND "
                                       "excess IS NOT NULL", (setup,))]
        n_open = db.execute("SELECT COUNT(*) FROM shadow WHERE setup=? AND status='open'", (setup,)).fetchone()[0]
        out[setup] = {"closed": len(xs), "open": n_open,
                      "mean_excess": round(statistics.fmean(xs), 4) if xs else None}
    return out


# ------------------------------------------------------------------ exchange I/O (the job)
async def fetch_panel(ex, top: int = FETCH_TOP, days: int = HISTORY_D) -> Panel:
    """Closed daily bars [(ts, o, h, l, c, quote_vol)] of the top perps by 24h quote volume, plus BTC."""
    perps = await ex.coin_perps()
    ids = {m["id"]: s for s, m in perps.items()}
    tick = await ex.public.fapiPublicGetTicker24hr()
    ranked = sorted((t for t in tick if t["symbol"] in ids), key=lambda t: -float(t["quoteVolume"]))
    raws = list(dict.fromkeys([L.BTC] + [t["symbol"] for t in ranked[:top]]))
    today = int(datetime.now(UTC).timestamp() * 1000) // DAY * DAY
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(raw: str) -> tuple[str, list[tuple]]:
        async with sem:
            rows = await ex.public.fapiPublicGetKlines({"symbol": raw, "interval": "1d", "limit": days + 1})
        return raw, [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7]))
                     for r in rows if int(r[0]) < today]
    got = await asyncio.gather(*(one(r) for r in raws), return_exceptions=True)
    bars = {raw: rows for g in got if not isinstance(g, BaseException) for raw, rows in [g] if rows}
    return Panel({"bars": bars, "funding": {}})


async def funding_paid(ex, raw: str, d0: int, d1: int) -> float:
    """Sum of funding rates settled in (d0, d1]: what a long paid (negative = received)."""
    from .exchange import to_symbol
    rows = await ex.public.fetch_funding_rate_history(to_symbol(raw), since=d0 + 1, limit=200)
    return sum(float(r["fundingRate"]) for r in rows if d0 < int(r["timestamp"]) <= d1)


def _meta(db: sqlite3.Connection, key: str) -> str | None:
    r = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


SETTLE_MS = 10 * 60_000   # Binance finalises the daily bar shortly after 00:00 UTC; wait 10 minutes


def due(now_ms: int, path: Path | str | None = None) -> bool:
    """A new daily bar has closed (and settled) since the last run: the cheap check the guardian polls."""
    today = now_ms // DAY * DAY
    if now_ms < today + SETTLE_MS:
        return False
    p = Path(path or db_path())
    if not p.exists():
        return True
    db = sqlite3.connect(str(p), timeout=15)
    try:
        last = _meta(db, "last_day")
    except sqlite3.Error:
        return True
    finally:
        db.close()
    return last is None or int(last) < today - DAY


async def run(ex, path: Path | str | None = None) -> dict[str, Any]:
    """The daily job: open the signals of every day since the last run, close what is due, return the summary."""
    db = connect(path)
    try:
        last = int(_meta(db, "last_day") or FORWARD_START - DAY)
        oldest = db.execute("SELECT MIN(entry_day) FROM shadow WHERE status='open'").fetchone()[0]
        since = min(last, oldest or last)
        today = int(datetime.now(UTC).timestamp() * 1000) // DAY * DAY
        p = await fetch_panel(ex, days=min(1499, HISTORY_D + max(0, (today - since) // DAY)))
        if L.BTC not in p.bars:
            raise RuntimeError("no BTC daily bars: the shadow test can't evaluate gate G")
        st, d = L.Study(p), max(p.bars[L.BTC])
        bo = L.breakout_days(st)
        new = [t for day in sorted(x for x in p.bars[L.BTC] if last < x <= d) for t in signals(st, day, bo)]
        opened = record(db, new)
        closed = 0
        for x in exits(db, p, d):
            try:
                f = await funding_paid(ex, x["row"]["symbol"], x["row"]["entry_day"], x["day"])
            except Exception as e:  # noqa: BLE001 - label without funding rather than not at all; logged
                log.warning("funding for %s unavailable: %s", x["row"]["symbol"], e)
                f = 0.0
            close(db, x, p, f)
            closed += 1
        with db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('last_day', ?)", (str(d),))
        return {"day": datetime.fromtimestamp(d / 1000, UTC).date().isoformat(), "gate": st.gate(L.PRIMARY_GATE, d),
                "evaluated_from": datetime.fromtimestamp((last + DAY) / 1000, UTC).date().isoformat(),
                "opened": opened, "closed": closed, "signals": [(t["setup"], t["symbol"]) for t in new],
                "summary": summary(db)}
    finally:
        db.close()
