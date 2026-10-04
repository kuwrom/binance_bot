"""SQLite journal: trades (with plan, entry features, fees/funding, excursions), decisions, alerts, kv,
metrics. Research tables (scan candidates + outcomes) live in journal_research.py, learnings in journal_learnings.py.

Trade lifecycle: opening -> open -> closed, or opening -> aborted. Rows are addressed by id.
Schema grows by adding columns to TRADE_COLUMNS; existing DBs are migrated automatically.

Demo and live share this file: every trade carries its `mode`, and the readers that feed decisions (open trades,
closed results, stats) see only the journal's own mode unless asked for all_modes. Account-level kv (equity peak,
period marks, research overrides) goes through `mode_key`.

Audit trail (#24, audit.py): every decision, alert and session row is stamped with the actor, the Claude Code session,
the code version and the mode (+ the journal session and trade id on decisions, the running intent on decisions and
alerts). Decisions are append-only; so are intents (intents.py, the record of every write).

Writes on an event loop never wait for the write lock (#79, journal_writer.py): `await write(fn, *a)` and `post(fn, *a)`
run fn(journal, *a) as one unit. Decisions and alerts are always posted: stamped at the call, written in order. A kv
marker that guards a repeat (a cooldown stamp, a dedupe marker) is `mark`ed: this process reads it back at once, also
while its write is queued behind another process's lock (#81). A value merged with what is stored when it lands (a risk
peak, a period's first mark) is `merge`d: its reader merges `pending` the same way (#82)."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from . import audit, fill_report, intent_changes, intents, journal_tables, registry
from .journal_learnings import LEARNINGS_SCHEMA, LearningStore
from .journal_research import RESEARCH_SCHEMA, ResearchStore, add_columns
from .journal_writer import Writer, connect
from .redact import redact
from .sessions import session_bucket

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, mode TEXT NOT NULL, notes TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, session_id INTEGER, symbol TEXT,
    action TEXT NOT NULL, reasoning TEXT NOT NULL, snapshot TEXT
);
CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
    BEGIN SELECT RAISE(ABORT, 'decisions are append-only: log a new decision instead'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
    BEGIN SELECT RAISE(ABORT, 'decisions are append-only: log a new decision instead'); END;
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, symbol TEXT, kind TEXT NOT NULL, message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metrics (
    ts TEXT NOT NULL, symbol TEXT NOT NULL, price REAL, funding_rate REAL,
    open_interest REAL, quote_volume_24h REAL, PRIMARY KEY (ts, symbol)
);
"""

# Add new trade fields here; old databases get them via ALTER TABLE on startup.
TRADE_COLUMNS: dict[str, str] = {
    "status": "TEXT NOT NULL DEFAULT 'open'", "opened_at": "TEXT", "entry_price": "REAL",
    "amount": "REAL", "notional": "REAL", "leverage": "INTEGER", "stop_price": "REAL",
    "initial_stop": "REAL", "setup": "TEXT", "thesis": "TEXT", "plan": "TEXT", "entry_features": "TEXT",
    "experiment": "TEXT", "closed_at": "TEXT", "exit_price": "REAL", "pnl": "REAL", "gross_pnl": "REAL",
    "fees": "REAL", "funding": "REAL", "exit_reason": "TEXT", "lesson": "TEXT", "mae_pct": "REAL",
    "mfe_pct": "REAL", "low_since_entry": "REAL", "targets_hit": "TEXT", "notes": "TEXT",
    "conviction": "TEXT", "regime_cell": "TEXT", "post_exit": "TEXT", "mode": "TEXT NOT NULL DEFAULT 'demo'",
    "fill_refs": "TEXT",   # mainnet references at each fill (fill_quality.py, #35): atomic json writes only
}
# The audit stamp (#24); rows written by a process that predates it keep NULLs.
AUDIT_COLUMNS: dict[str, dict[str, str]] = {
    "decisions": {"actor": "TEXT", "claude_session": "TEXT", "code_version": "TEXT", "trade_id": "INTEGER",
                  "mode": "TEXT", "intent_id": "TEXT"},
    "alerts": {"actor": "TEXT", "session_id": "INTEGER", "claude_session": "TEXT", "code_version": "TEXT",
               "mode": "TEXT", "intent_id": "TEXT"},
    "sessions": {"actor": "TEXT", "claude_session": "TEXT", "code_version": "TEXT"},
}
AUDIT_ACTIONS = ("call", "claude_session")   # audit-only rows (call: a write, before intents): decisions() skips them
JSON_FIELDS = ("plan", "entry_features", "targets_hit", "post_exit", "fill_refs")
MODES = ("demo", "live")
T = TypeVar("T")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _group(rows: list[dict[str, Any]], key: str, empty: str = "untagged") -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        s = out.setdefault(r[key] or empty, {"n": 0, "wins": 0, "pnl": 0.0, "_r": [], "_mae": [], "_mfe": []})
        s["n"] += 1
        s["wins"] += r["pnl"] > 0
        s["pnl"] += r["pnl"]
        if r.get("r_multiple") is not None:
            s["_r"].append(r["r_multiple"])
        for src, dst in (("mae_pct", "_mae"), ("mfe_pct", "_mfe")):
            if r.get(src) is not None:
                s[dst].append(r[src])
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None  # noqa: E731
    for s in out.values():
        s["win_rate"] = round(s["wins"] / s["n"] * 100, 1)
        s["pnl"] = round(s["pnl"], 2)
        s["avg_r"], s["avg_mae_pct"], s["avg_mfe_pct"] = avg(s.pop("_r")), avg(s.pop("_mae")), avg(s.pop("_mfe"))
    return out


def exit_quality(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Did price keep falling after we exited (runners cut too early) or bounce (exits well timed)?"""
    pe = [t["post_exit"] for t in trades if t.get("post_exit") and t["post_exit"].get("further_drop_24h_pct") is not None]
    if not pe:
        return {"n": 0, "note": "post-exit moves are labelled 24h after each close by the guardian"}
    avg = lambda k: round(sum(p[k] for p in pe if p.get(k) is not None) / len(pe), 2)  # noqa: E731
    return {"n": len(pe), "avg_further_drop_24h_pct": avg("further_drop_24h_pct"),
            "avg_rise_against_24h_pct": avg("rise_against_24h_pct"), "avg_ret_24h_after_exit_pct": avg("ret_24h_pct"),
            "how_to_read": "consistently large further drops (n >= 20) = hold runners longer / looser trail"}


class Journal(ResearchStore, LearningStore):
    def __init__(self, path: Path | str, mode: str = "demo"):
        """mode: the account this process trades (settings.mode). Stamped on new trades; filters what it reads."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        self.mode = mode
        self.session_id: int | None = None   # the trading session this process started (start_session)
        self._marks: dict[str, str] = {}      # kv this process marked whose write may not have landed yet (mark)
        self._merges: dict[str, str] = {}     # this process's view of kv it merges, until its write lands (merge)
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = connect(path)   # WAL: the guardian, MCP servers and CLI share this file
        self.db.row_factory = sqlite3.Row
        # every table at open: a reader never runs DDL, which waits for the write lock (registry #81, journal_tables #82)
        self.db.executescript(SCHEMA + RESEARCH_SCHEMA + LEARNINGS_SCHEMA + intents.SCHEMA + registry.SCHEMA
                              + journal_tables.SCHEMA + intent_changes.SCHEMA)
        self._migrate()
        self.w = Writer(self.db)

    # ------------------------------------------------------------ units (journal_writer.py)
    async def write(self, fn: Callable[..., T], *a: Any, **k: Any) -> T:
        """fn(self, *a, **k) as one unit, its result once committed; an event loop never waits for the lock."""
        return await self.w.write(fn, self, *a, **k)

    def post(self, fn: Callable[..., T], *a: Any, **k: Any) -> T | None:
        """fn(self, *a, **k) as one unit nobody waits for: now when the lock is free, else queued in order."""
        return self.w.post(fn, self, *a, **k)

    def run(self, fn: Callable[..., T], *a: Any, **k: Any) -> T:
        """fn(self, *a, **k) as one unit, waiting for the lock (a caller off an event loop)."""
        return self.w.run(fn, self, *a, **k)

    async def flush(self) -> None:
        await self.w.flush()

    def _migrate(self) -> None:
        for table, cols in {"trades": TRADE_COLUMNS, **intents.COLUMNS, **AUDIT_COLUMNS,
                            **journal_tables.COLUMNS}.items():
            add_columns(self.db, table, cols)
        intents.migrate(self.db)
        self._migrate_research()
        self.db.commit()
        self._migrate_learnings()   # its index is built in a transaction of its own

    def _insert(self, sql: str, args: tuple) -> int:
        cur = self.db.execute(sql, args)
        self.db.commit()
        if (rowid := cur.lastrowid) is None:   # a plain INSERT always sets it
            raise sqlite3.ProgrammingError(f"no row id after {sql}")
        return rowid

    def _rows(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _in_mode(self, all_modes: bool = False) -> tuple[str, tuple]:
        return ("1=1", ()) if all_modes else ("mode=?", (self.mode,))

    def mode_key(self, k: str) -> str:
        """kv key for state that belongs to one account. Demo keeps the bare key (everything before the split was
        demo, and old processes keep working); live gets '@live', so a switch never inherits demo's peak or marks."""
        return k if self.mode == "demo" else f"{k}@{self.mode}"

    # ------------------------------------------------------------ sessions / decisions
    def start_session(self, mode: str, notes: str = "") -> int:
        """Rows this process journals from now on belong to the new session."""
        st = audit.stamp()
        self.session_id = self._insert(
            "INSERT INTO sessions (started_at, mode, notes, actor, claude_session, code_version) VALUES (?,?,?,?,?,?)",
            (now_iso(), mode, notes, st["actor"], st["claude_session"], st["code_version"]))
        return self.session_id

    def session_of(self, claude_session: str | None) -> int | None:
        """This process's session, else the latest one its Claude Code session started (a CLI call from its Bash)."""
        if self.session_id is None and claude_session:
            row = self.db.execute("SELECT id FROM sessions WHERE claude_session=? ORDER BY id DESC LIMIT 1",
                                  (claude_session,)).fetchone()
            return row[0] if row else None
        return self.session_id

    def log_decision(self, action: str, reasoning: str, symbol: str | None = None,
                     snapshot: dict | None = None, session_id: int | None = None, trade_id: int | None = None
                     ) -> int | None:
        """Stamped (audit.py); trade_id defaults to snapshot['trade_id']. Text is redacted like alerts. Posted: its id,
        or None while queued behind another process's write lock."""
        audit.count_decision()
        st = audit.stamp()
        if trade_id is None and isinstance(snapshot, dict) and type(snapshot.get("trade_id")) is int:
            trade_id = snapshot["trade_id"]
        return self.post(
            Journal._insert,
            "INSERT INTO decisions (ts, session_id, symbol, action, reasoning, snapshot, actor, claude_session,"
            " code_version, trade_id, mode, intent_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), session_id if session_id is not None else self.session_of(st["claude_session"]), symbol,
             action, redact(reasoning),
             json.dumps(audit.clean(snapshot, trim=False), default=str) if snapshot else None,
             st["actor"], st["claude_session"], st["code_version"], trade_id, self.mode, st["intent_id"]))

    def decisions(self, symbol: str | None = None, limit: int = 50, calls: bool = False) -> list[dict[str, Any]]:
        """Newest first. calls=True also returns the audit-only rows (AUDIT_ACTIONS: tool/CLI calls, hook rows)."""
        where, args = ("1=1", ()) if calls else (f"action NOT IN ({','.join('?' * len(AUDIT_ACTIONS))})", AUDIT_ACTIONS)
        where, args = where + " AND COALESCE(mode, 'demo')=?" + (" AND symbol=?" if symbol else ""), \
            (*args, self.mode, *([symbol] if symbol else []))   # this desk's (rows from before #24: demo's)
        return self._rows(f"SELECT * FROM decisions WHERE {where} ORDER BY id DESC LIMIT ?", (*args, limit))

    # ------------------------------------------------------------ trades
    def _trade(self, row: dict[str, Any]) -> dict[str, Any]:
        for f in JSON_FIELDS:
            if row.get(f):
                try:
                    row[f] = json.loads(row[f])
                except (TypeError, ValueError):
                    pass
        return row

    def create_trade(self, symbol: str, setup: str, thesis: str, stop_price: float, leverage: int,
                     plan: dict | None = None, entry_features: dict | None = None,
                     experiment: str | None = None) -> int:
        """Pending row written BEFORE the entry order, so a crash mid-entry is still traceable."""
        return self._insert(
            "INSERT INTO trades (symbol, status, opened_at, stop_price, initial_stop, leverage, setup, thesis, plan,"
            " entry_features, experiment, targets_hit, mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, "opening", now_iso(), stop_price, stop_price, leverage, setup, thesis,
             json.dumps(plan or {}, default=str), json.dumps(entry_features or {}, default=str), experiment, "[]",
             self.mode))

    def activate_trade(self, trade_id: int, entry_price: float, amount: float, notional: float) -> None:
        self.update_trade(trade_id, status="open", entry_price=entry_price, amount=amount, notional=notional,
                          low_since_entry=entry_price, mae_pct=0.0, mfe_pct=0.0)

    def update_trade(self, trade_id: int, **fields: Any) -> None:
        bad = set(fields) - set(TRADE_COLUMNS)
        if bad:
            raise ValueError(f"unknown trade fields {bad}")
        vals = [json.dumps(v, default=str) if k in JSON_FIELDS and not isinstance(v, str) else v
                for k, v in fields.items()]
        self.db.execute(f"UPDATE trades SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*vals, trade_id))
        self.db.commit()

    def close_trade(self, trade_id: int, exit_price: float | None, pnl: float | None, exit_reason: str,
                    lesson: str = "", gross_pnl: float | None = None, fees: float | None = None,
                    funding: float | None = None) -> None:
        self.update_trade(trade_id, status="closed", closed_at=now_iso(), exit_price=exit_price, pnl=pnl,
                          exit_reason=exit_reason, lesson=lesson, gross_pnl=gross_pnl, fees=fees, funding=funding)

    def abort_trade(self, trade_id: int, reason: str) -> None:
        self.update_trade(trade_id, status="aborted", closed_at=now_iso(), exit_reason=reason)

    def get_trade(self, trade_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM trades WHERE id=?", (trade_id,))
        return self._trade(rows[0]) if rows else None

    def active_trade(self, symbol: str) -> dict[str, Any] | None:
        """Newest opening/open row for a symbol, in this journal's mode."""
        rows = self._rows("SELECT * FROM trades WHERE symbol=? AND mode=? AND status IN ('opening','open') "
                          "ORDER BY id DESC LIMIT 1", (symbol, self.mode))
        return self._trade(rows[0]) if rows else None

    def open_trades(self, include_opening: bool = False, all_modes: bool = False) -> list[dict[str, Any]]:
        st = "('opening','open')" if include_opening else "('open')"
        m, args = self._in_mode(all_modes)
        return [self._trade(r) for r in self._rows(f"SELECT * FROM trades WHERE status IN {st} AND {m} ORDER BY id",
                                                   args)]

    def trades(self, limit: int = 50, all_modes: bool = False, drills: bool = True) -> list[dict[str, Any]]:
        """Newest first. drills=False: without the plumbing drills' trades (statistics, journal_tables.NOT_DRILL)."""
        m, args = self._in_mode(all_modes)
        m += "" if drills else f" AND {journal_tables.NOT_DRILL}"
        return [self._trade(r) for r in self._rows(f"SELECT * FROM trades WHERE {m} ORDER BY id DESC LIMIT ?",
                                                   (*args, limit))]

    def closed_results(self, all_modes: bool = False) -> list[dict[str, Any]]:
        """Closed trades oldest first, with R = net pnl / initial risk at the stop (governor + tilts input; the
        registry's forward feed), the limits each was sized under (entry_features.limits, JSON text) and the
        learnings its plan cited (uses_learnings: learnings_view.scores). Never a plumbing drill's (#46)."""
        m, args = self._in_mode(all_modes)
        rows = self._rows(
            "SELECT id, symbol, setup, experiment, conviction, regime_cell, opened_at, closed_at, pnl, mae_pct,"
            " mfe_pct, entry_price, initial_stop, amount, plan, CASE WHEN json_valid(entry_features)"
            " THEN json_extract(entry_features, '$.limits') END AS limits"
            f" FROM trades WHERE status='closed' AND pnl IS NOT NULL AND {m} AND {journal_tables.NOT_DRILL}"
            " ORDER BY closed_at", args)
        for r in rows:
            plan = json.loads(r.pop("plan") or "{}") or {}
            pinned = plan.get("r1_usdt")   # adds/split entries pin 1R
            r["uses_learnings"] = [i for i in plan.get("uses_learnings") or [] if type(i) is int]
            risk = pinned or ((r["initial_stop"] - r["entry_price"]) * r["amount"] if r["initial_stop"]
                              and r["entry_price"] and r["amount"] else None)
            r["r_multiple"], r["risk_usdt"] = (r["pnl"] / risk if risk else None), risk
            r["session"] = session_bucket(datetime.fromisoformat(r["opened_at"])) if r.get("opened_at") else None
        return rows

    def stats(self, all_modes: bool = False) -> dict[str, Any]:
        """by_experiment is keyed by the full set of experiments active at entry (params.py); param_sets > 1 means
        the trades behind one label were sized under different limits (a reused tag, or a new .env baseline)."""
        rows = self.closed_results(all_modes)
        by_exp = _group(rows, "experiment", "baseline")
        for name, g in by_exp.items():
            g["param_sets"] = len({r["limits"] for r in rows if (r["experiment"] or "baseline") == name
                                   and r["limits"]})
        other = {} if all_modes else dict(self.db.execute(
            f"SELECT mode, COUNT(*) FROM trades WHERE status='closed' AND mode!=? AND {journal_tables.NOT_DRILL}"
            " GROUP BY mode", (self.mode,)))
        return {"mode": "all" if all_modes else self.mode, "closed_trades": len(rows),
                "total_pnl": round(sum(r["pnl"] for r in rows), 2), "by_setup": _group(rows, "setup"),
                "by_experiment": by_exp, "by_entry_session": _group(rows, "session"),
                "exits": exit_quality(self.trades(500, all_modes, drills=False)),
                "fill_quality": fill_report.report(self, all_modes),
                **({"other_modes": other} if other else {})}

    # ------------------------------------------------------------ kv
    def get(self, k: str) -> str | None:
        if k in self._marks:
            return self._marks[k]
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    def mark(self, k: str, v: str) -> None:
        """Set a kv marker that guards a repeat (alert_ts:, news_seen:...): posted, and read back by this process at
        once. While another process holds the write lock the post waits in the queue, and a check reading only the
        journal repeated what the marker guards on every pass (#81); get() answers from here until it lands.
        Process-local: the journal stays the cross-process record."""
        self._marks[k] = v
        self.post(Journal._land, self._marks, k, v, Journal.set, (k, v))

    def merge(self, k: str, v: str, fn: Callable[..., None], *a: Any) -> None:
        """Post fn(self, *a): a unit that merges this process's view `v` of kv `k` into what is stored when it lands
        (a max, a first-wins insert), atomically across processes. Until it lands pending(k) returns v, and the caller's
        reader merges it with get(k) the same way: a risk peak this process saw is never read lower while another
        process holds the write lock (#82). Process-local, like mark."""
        self._merges[k] = v
        self.post(Journal._land, self._merges, k, v, fn, a)

    def pending(self, k: str) -> str | None:
        """This process's view of kv k (merge) while its write waits in the queue; None once landed."""
        return self._merges.get(k)

    def _land(self, held: dict[str, str], k: str, v: str, fn: Callable[..., None], a: tuple) -> None:
        fn(self, *a)
        if held.get(k) == v:   # else a later mark or merge of k is still on its way
            del held[k]

    def set(self, k: str, v: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, v))
        self.db.commit()

    def prune_kv(self, prefixes: tuple[str, ...], max_age_s: float) -> int:
        """Delete timestamp-valued kv keys (dedupe markers) older than max_age_s."""
        cutoff, n = time.time() - max_age_s, 0
        for p in prefixes:
            n += self.db.execute("DELETE FROM kv WHERE k LIKE ? AND CAST(v AS REAL) < ?", (p + "%", cutoff)).rowcount
        self.db.commit()
        return n

    # ------------------------------------------------------------ alerts / metrics
    def log_alert(self, kind: str, message: str, symbol: str | None = None) -> int | None:
        """Posted, like log_decision."""
        st = audit.stamp()
        return self.post(
            Journal._insert,
            "INSERT INTO alerts (ts, symbol, kind, message, actor, session_id, claude_session, code_version, mode,"
            " intent_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), symbol, kind, redact(message),   # no signed URL or token in a row
             st["actor"], self.session_of(st["claude_session"]), st["claude_session"], st["code_version"], self.mode,
             st["intent_id"]))

    def alerts(self, limit: int = 30, since: str | None = None) -> list[dict[str, Any]]:
        from .alerts_view import MINE  # this desk's alerts and the shared market ones
        sql = f"SELECT * FROM alerts WHERE {MINE}" + (" AND ts > ?" if since else "")
        return self._rows(sql + " ORDER BY id DESC LIMIT ?", (self.mode, *([since] if since else []), limit))

    def record_metrics(self, rows: list[tuple]) -> None:
        """rows: (ts, symbol, price, funding_rate, open_interest, quote_volume_24h)"""
        self.db.executemany("INSERT OR REPLACE INTO metrics VALUES (?,?,?,?,?,?)", rows)
        self.db.commit()

    def metric_history(self, symbol: str, limit: int = 200) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM metrics WHERE symbol=? ORDER BY ts DESC LIMIT ?", (symbol, limit))
