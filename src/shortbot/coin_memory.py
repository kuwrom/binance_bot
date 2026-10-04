"""Coin memory (docs/COIN_MEMORY.md): a rolling state per focus coin, the session's current read of it and a watchlist,
in shortbot.db (the tables are declared here and created inside its own units, so backups carry them).

- focus_symbols: open trades, resting entries, the watchlist and the top tradable candidates of the last hour, at most
  FOCUS_CAP coins. The one shared definition of the coins the desk is watching.
- coin_state: one row per coin; its state JSON is coin_stats.measure's (with a bounded 3-day series: long_short()
  returns only the latest point). A pass (coin_pass.refresh) writes it every 15 min from the guardian's `coins` lane,
  or on demand (`coin_memory --refresh`). No LLM runs in a pass: it measures, alerts and wakes a session.
- coin_reads: append-only (triggers, like decisions). The session's read of a coin (<= 1200 chars, a bias, levels, an
  invalidation price) expires, 24h by default: coin-specific tells lasted about a day (#79, #80). The current read is
  the newest unexpired one; a new read archives the earlier ones. read_score scores expired reads by the move from
  price_at over their window (coin_read_outcomes, filled by the pass): the analyst is scored by outcomes.
- watchlist: coins a session asked to keep in focus, until a date. A coin out of focus for 3 days is archived."""

from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import UTC, datetime
from typing import Any

from .exchange import to_symbol
from .untrusted import flat

FOCUS_CAP = 12
SCAN_TOP, SCAN_WINDOW_MS = 5, 3_600_000      # the top tradable candidates of the last hour
READ_MAX = 1200
READ_TTL_H, READ_MAX_TTL_H = 24.0, 72.0      # update_coin_read's default and cap
BIASES = ("up", "down", "neutral")           # update_coin_read's choices
ARCHIVE_MS = 3 * 86_400_000                  # out of focus this long: archived
STALE_MS = 2 * 3_600_000                     # the brief shows states this fresh
BRIEF_LINES = 8
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS coin_state (symbol TEXT PRIMARY KEY, updated_ms INTEGER NOT NULL, focus TEXT, "
    "status TEXT NOT NULL DEFAULT 'focus', state TEXT)",
    "CREATE TABLE IF NOT EXISTS coin_reads (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, ts TEXT NOT NULL, "
    "ts_ms INTEGER NOT NULL, read TEXT NOT NULL, bias TEXT NOT NULL, levels TEXT, invalidation REAL, "
    "expires_ms INTEGER NOT NULL, price_at REAL, actor TEXT, claude_session TEXT, session_id INTEGER)",
    "CREATE INDEX IF NOT EXISTS idx_coin_reads ON coin_reads (symbol, ts_ms)",
    "CREATE TRIGGER IF NOT EXISTS coin_reads_no_update BEFORE UPDATE ON coin_reads "
    "BEGIN SELECT RAISE(ABORT, 'coin reads are append-only: write a new read'); END",
    "CREATE TRIGGER IF NOT EXISTS coin_reads_no_delete BEFORE DELETE ON coin_reads "
    "BEGIN SELECT RAISE(ABORT, 'coin reads are append-only: write a new read'); END",
    "CREATE TABLE IF NOT EXISTS coin_read_outcomes (read_id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, "
    "price_end REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS watchlist (symbol TEXT PRIMARY KEY, added_ts TEXT NOT NULL, reason TEXT, "
    "until_ms INTEGER NOT NULL)",
)


def ensure(db: sqlite3.Connection) -> None:
    """The tables, one statement each (works inside a journal unit). Existing ones need no write lock."""
    for sql in SCHEMA:
        db.execute(sql)


def _rows(db: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    cur = db.execute(sql, args)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds")


def _ago(ms: int) -> str:
    m = max(0, ms) / 60_000
    return f"{m:.0f}m" if m < 90 else f"{m / 60:.0f}h" if m < 2880 else f"{m / 1440:.0f}d"


# ------------------------------------------------------------ the focus set
def focus_symbols(j: Any, entries: Any, now_ms: int, cap: int = FOCUS_CAP) -> dict[str, list[str]]:
    """{symbol: why} in priority order: open trades, resting entries, the watchlist, the top SCAN_TOP tradable
    candidates of the last hour (a SQL window: j.candidates() would load 5,000 rows)."""
    out: dict[str, list[str]] = {}
    for t in j.open_trades():
        out.setdefault(t["symbol"], []).append(f"trade #{t['id']}")
    for e in entries.pending() if entries is not None else ():
        out.setdefault(e["symbol"], []).append(f"entry #{e['id']}")
    for w in watching(j.db, now_ms):
        out.setdefault(w["symbol"], []).append("watch")
    for sym, score in j.db.execute(
            "SELECT symbol, MAX(score) FROM candidates WHERE tradable=1 AND ts_ms >= ? GROUP BY symbol "
            "ORDER BY 2 DESC, symbol LIMIT ?", (now_ms - SCAN_WINDOW_MS, SCAN_TOP)):
        out.setdefault(sym, []).append(f"scan {score}")
    return dict(list(out.items())[:cap])


def watching(db: sqlite3.Connection, now_ms: int) -> list[dict[str, Any]]:
    try:
        return _rows(db, "SELECT * FROM watchlist WHERE until_ms > ? ORDER BY added_ts", (now_ms,))
    except sqlite3.OperationalError:   # no table yet: nothing watched
        return []


def watch(j: Any, symbol: str, remove: bool, reason: str | None, days: float, now_ms: int) -> dict[str, Any]:
    """Add (or extend) a coin on the watchlist for `days`, or take it off. Runs inside a journal unit."""
    if not remove and not 0 < days <= 30:
        raise ValueError("days must be in (0, 30]")
    ensure(j.db)
    sym = to_symbol(symbol)
    if remove:
        n = j.db.execute("DELETE FROM watchlist WHERE symbol=?", (sym,)).rowcount
        return {"ok": True, "symbol": sym, "removed": bool(n)}
    until = now_ms + int(days * 86_400_000)
    j.db.execute("INSERT OR REPLACE INTO watchlist (symbol, added_ts, reason, until_ms) VALUES (?,?,?,?)",
                 (sym, _iso(now_ms), flat(reason, 200) if reason else None, until))
    return {"ok": True, "symbol": sym, "until": _iso(until)}


# ------------------------------------------------------------ state
def states(db: sqlite3.Connection, symbols: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """{symbol: row} with `state` and `focus` parsed; {} before the first pass."""
    q, args = "SELECT * FROM coin_state", tuple[Any, ...]()
    if symbols is not None:
        q, args = q + f" WHERE symbol IN ({','.join('?' * len(symbols))})", tuple(symbols)
    try:
        rows = _rows(db, q, args)
    except sqlite3.OperationalError:
        return {}
    for r in rows:
        r["state"], r["focus"] = json.loads(r["state"] or "{}"), json.loads(r["focus"] or "[]")
    return {r["symbol"]: r for r in rows}


def save(j: Any, coins: dict[str, dict[str, Any]], focus: dict[str, list[str]], now_ms: int) -> int:
    """One pass's states (inside a journal unit); coins out of focus for ARCHIVE_MS are archived. Returns how many."""
    ensure(j.db)
    j.db.executemany(
        "INSERT INTO coin_state (symbol, updated_ms, focus, status, state) VALUES (?,?,?,'focus',?) ON CONFLICT(symbol)"
        " DO UPDATE SET updated_ms=excluded.updated_ms, focus=excluded.focus, status='focus', state=excluded.state",
        [(s, now_ms, json.dumps(focus.get(s, [])), json.dumps(st, default=str)) for s, st in coins.items()])
    j.db.execute("UPDATE coin_state SET status='archived' WHERE status='focus' AND updated_ms < ?",
                 (now_ms - ARCHIVE_MS,))
    return len(coins)


# ------------------------------------------------------------ reads
def _levels(levels: Any) -> dict[str, Any] | None:
    if levels is None:
        return None
    ok = isinstance(levels, dict) and all(
        isinstance(k, str) and (_num(v) or (isinstance(v, list) and v and all(_num(x) for x in v)))
        for k, v in levels.items())
    if not ok or len(levels) > 12:
        raise ValueError('levels: a JSON object of named prices, e.g. {"resistance": 251.2, "support": [242, 236]}')
    return levels


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def add_read(j: Any, symbol: str, read: str, bias: str, levels: Any, invalidation: float | None, expires_h: float,
             price_at: float | None, now_ms: int, stamp: dict[str, Any]) -> dict[str, Any]:
    """Append the session's read of a coin (inside a journal unit). Earlier reads stay as history."""
    text = (read or "").strip()
    if not text or len(text) > READ_MAX:
        raise ValueError(f"read: 1-{READ_MAX} characters (got {len(text)}); keep the gist, the evidence is in the "
                         "tools")
    if bias not in BIASES:
        raise ValueError(f"bias must be one of {BIASES}")
    if invalidation is not None and not _num(invalidation):
        raise ValueError("invalidation: the price that proves the read wrong")
    if not 0 < expires_h <= READ_MAX_TTL_H:
        raise ValueError(f"expires_h must be in (0, {READ_MAX_TTL_H:g}]: coin tells lasted about a day (#79, #80)")
    ensure(j.db)
    sym, exp = to_symbol(symbol), now_ms + int(expires_h * 3_600_000)
    lv = _levels(levels)
    rid = j.db.execute(
        "INSERT INTO coin_reads (symbol, ts, ts_ms, read, bias, levels, invalidation, expires_ms, price_at, actor, "
        "claude_session, session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (sym, _iso(now_ms), now_ms, text, bias, json.dumps(lv) if lv else None, invalidation, exp, price_at,
         stamp.get("actor"), stamp.get("claude_session"), j.session_of(stamp.get("claude_session")))).lastrowid
    return {"ok": True, "read_id": rid, "symbol": sym, "expires": _iso(exp), "price_at": price_at}


def reads(db: sqlite3.Connection, symbol: str, limit: int = 3) -> list[dict[str, Any]]:
    try:
        rows = _rows(db, "SELECT * FROM coin_reads WHERE symbol=? ORDER BY id DESC LIMIT ?", (symbol, limit))
    except sqlite3.OperationalError:
        return []
    for r in rows:
        r["levels"] = json.loads(r["levels"]) if r.get("levels") else None
    return rows


def current_read(db: sqlite3.Connection, symbol: str, now_ms: int) -> dict[str, Any] | None:
    """The newest read, while it hasn't expired."""
    r = reads(db, symbol, 1)
    return r[0] if r and r[0]["expires_ms"] > now_ms else None


def due_outcomes(db: sqlite3.Connection, now_ms: int, limit: int = 10) -> list[dict[str, Any]]:
    """Expired reads the pass hasn't scored yet (the price at their expiry is still to fetch)."""
    try:
        return _rows(db, "SELECT r.id, r.symbol, r.expires_ms FROM coin_reads r LEFT JOIN coin_read_outcomes o ON "
                         "o.read_id = r.id WHERE o.read_id IS NULL AND r.expires_ms <= ? AND r.price_at IS NOT NULL "
                         "ORDER BY r.expires_ms LIMIT ?", (now_ms, limit))
    except sqlite3.OperationalError:
        return []


def save_outcomes(j: Any, rows: list[tuple[int, int, float]]) -> None:
    """(read_id, ts_ms, price_end) rows, inside a journal unit."""
    ensure(j.db)
    j.db.executemany("INSERT OR IGNORE INTO coin_read_outcomes (read_id, ts_ms, price_end) VALUES (?,?,?)", rows)


def read_score(db: sqlite3.Connection, symbol: str | None = None) -> dict[str, Any]:
    """Expired reads scored by the move from price_at to the price at expiry: an up or down read is a hit when price
    went its way; a neutral read reports its median absolute move. A read superseded early is scored on its full
    window."""
    q = ("SELECT r.symbol, r.bias, r.price_at, o.price_end FROM coin_reads r JOIN coin_read_outcomes o ON "
         "o.read_id = r.id")
    try:
        rows = _rows(db, q + (" WHERE r.symbol=?" if symbol else ""), (symbol,) if symbol else ())
    except sqlite3.OperationalError:
        rows = []
    moves = [(r["bias"], (r["price_end"] / r["price_at"] - 1) * 100) for r in rows if r["price_at"]]
    called = [(b, m) for b, m in moves if b != "neutral"]
    hits = sum((m > 0) if b == "up" else (m < 0) for b, m in called)
    neutral = [abs(m) for b, m in moves if b == "neutral"]
    return {"directional": {"n": len(called), "hits": hits,
                            "hit_rate": round(hits / len(called), 2) if called else None},
            "neutral": {"n": len(neutral), "median_abs_move_pct": round(statistics.median(neutral), 2)
                        if neutral else None},
            "how_to_read": "a coin flip hits ~50%; under 30 scored reads it is an anecdote"}


# ------------------------------------------------------------ views (no third-party text)
def line(sym: str, row: dict[str, Any] | None, read: dict[str, Any] | None, now_ms: int) -> str:
    """One focus coin: `QNT active vol 1.9x | swing 2.1%/40m | fakeout p90 0.9% | read 3h (down) | events: ...`."""
    base = sym.split("/")[0]
    if not row:
        return f"{base}: no state yet (the guardian's coins lane, or coin_memory refresh=True)"
    st, parts = row["state"], []
    rg, sw = st.get("regime") or {}, st.get("swing_5m") or {}
    parts.append(f"{base} {rg.get('state') or '?'} vol {rg.get('rel_vol')}x")
    if sw.get("median_pct") is not None:
        parts.append(f"swing {sw['median_pct']}%/{sw.get('median_min')}m")
    if st.get("fakeout_p90") is not None:
        parts.append(f"fakeout p90 {st['fakeout_p90']}%")
    parts.append(f"read {_ago(now_ms - read['ts_ms'])} ({read['bias']})" if read else "no read")
    if st.get("break"):
        parts.append(f"break {st['break']['score']}/4")
    names = [e["name"] for e in st.get("events") or ()]
    if names:
        parts.append("events: " + ", ".join(names))
    if now_ms - row["updated_ms"] > STALE_MS:
        parts.append(f"state {_ago(now_ms - row['updated_ms'])} old")
    return " | ".join(parts)


def brief_lines(j: Any, now_ms: int | None = None, limit: int = BRIEF_LINES) -> list[str]:
    """The brief's focus coins (only reads; nothing third-party): at most `limit` lines, trades first."""
    import time
    now_ms = now_ms or int(time.time() * 1000)
    rows = [r for r in states(j.db).values() if r["status"] == "focus" and now_ms - r["updated_ms"] <= STALE_MS]
    if not rows:
        return []
    order = ("trade", "entry", "watch", "scan")
    rows.sort(key=lambda r: min((order.index(w.split()[0]) for w in r["focus"] if w.split()[0] in order),
                                default=len(order)))
    last = max(r["updated_ms"] for r in rows)
    out = [f"### focus coins ({len(rows)}, coin memory pass {_ago(now_ms - last)} ago; `coin_memory` for more)"]
    out += [f"- {line(r['symbol'], r, current_read(j.db, r['symbol'], now_ms), now_ms)}" for r in rows[:limit]]
    return out


def overview(j: Any, entries: Any, now_ms: int) -> dict[str, Any]:
    """Every focus coin as one line (the live focus set: a coin the guardian hasn't measured yet says so), the
    watchlist and the read score across coins."""
    focus = focus_symbols(j, entries, now_ms)
    rows = states(j.db, list(focus))
    return {"focus": [{"symbol": s, "why": why, "line": line(s, rows.get(s), current_read(j.db, s, now_ms), now_ms)}
                      for s, why in focus.items()],
            "watchlist": [{"symbol": w["symbol"], "reason": w["reason"], "until": _iso(w["until_ms"])}
                          for w in watching(j.db, now_ms)],
            "read_score": read_score(j.db),
            "how_to_read": "scale persists per coin (swings, fake-outs), direction doesn't (#54, #55): a read expires "
                           "(24h). A symbol shows one coin in full; update_coin_read rewrites its read."}


def view(j: Any, symbol: str, now_ms: int) -> dict[str, Any]:
    """One coin in full: its state (the series summarised), the current read, the last 3 reads, read_score."""
    sym = to_symbol(symbol)
    row = states(j.db, [sym]).get(sym)
    st = dict(row["state"]) if row else {}
    series = st.pop("series", None)
    st.pop("blocks15", None)   # the break score's input (break.move_pct says what it found)
    return {"symbol": sym, "focus": row["focus"] if row else [], "status": row["status"] if row else None,
            "updated": _iso(row["updated_ms"]) if row else None, "state": st,
            "series": {"points": len(series), "from": _iso(series[0][0])} if series else None,
            "read": current_read(j.db, sym, now_ms), "reads": reads(j.db, sym), "read_score": read_score(j.db, sym),
            "line": line(sym, row, current_read(j.db, sym, now_ms), now_ms)}
