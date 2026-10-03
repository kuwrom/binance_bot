"""Intents (#36 gateway step 2, docs/GATEWAY.md §4-5): every write the desk makes, journalled on receipt before
anything runs, under an id its caller picks (a ULID). gateway.py runs them; this module is the record.

- An intent is a write command (its kind is the command's, its args the body's keyword arguments: commands.py) or a
  guardian action (restore_stop, force_close, cap_stop, plan_step). It goes received -> running -> a final status:
  done | refused | failed | unknown | interrupted | expired, or busy (nothing sent: it may run again under its id).
  Until step 3 types the results, the status is today's outcome: refused = the result said ok: false.
- The row replaces the #24 `call` decision row of a write: it holds the request (args, actor, Claude session, code)
  from before the call runs and its result after. Rows are append-only like decisions: the request never changes and
  a final status is final (triggers). Decision and alert rows written while an intent runs carry its id (audit.py).
- Its orders are linked by their #9 client id (`intent_orders`, written by the Trader primitives just before each
  send): the per-order write-ahead rows (entries, target_orders, the trade's open id) keep their own state and are
  reached through that id; nothing is copied.
- A process that died mid-intent leaves it open: `sweep` (the guardian's reconcile job) ends it, expired if it never
  started, interrupted if it did, and lists its client ids; the existing reconcilers resolve the orders themselves.

Every function takes the journal and runs plain synchronous SQLite: no event-loop state, so a caller may run it in a
thread on its own connection (#79)."""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from . import audit, clientid

log = logging.getLogger("shortbot.intents")

OPEN = ("received", "running")
FINAL = ("done", "refused", "failed", "unknown", "expired")   # interrupted may still get its owner's late answer
MAX_OPEN_S = 3600.0   # longer than any intent runs: the sweep ends it even if its pid now belongs to another process
SIDES = ("short", "long")

SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, symbol TEXT, side TEXT NOT NULL, args TEXT NOT NULL,
    status TEXT NOT NULL, result TEXT, error TEXT, trade_id INTEGER,
    actor TEXT NOT NULL, claude_session TEXT, session_id INTEGER, code_version TEXT, mode TEXT NOT NULL,
    pid INTEGER NOT NULL, received_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS intents_open ON intents (status) WHERE status IN ('received', 'running');
CREATE INDEX IF NOT EXISTS intents_received ON intents (received_at);
CREATE TRIGGER IF NOT EXISTS intents_no_delete BEFORE DELETE ON intents
    BEGIN SELECT RAISE(ABORT, 'intents are append-only'); END;
CREATE TRIGGER IF NOT EXISTS intents_request_frozen BEFORE UPDATE OF id, kind, symbol, side, args, actor,
    claude_session, session_id, code_version, mode, pid, received_at ON intents
    BEGIN SELECT RAISE(ABORT, 'an intent''s request never changes'); END;
CREATE TRIGGER IF NOT EXISTS intents_final BEFORE UPDATE ON intents
    WHEN OLD.status IN ('done', 'refused', 'failed', 'unknown', 'expired')
    BEGIN SELECT RAISE(ABORT, 'a finished intent never changes'); END;
CREATE TABLE IF NOT EXISTS intent_orders (
    client_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES intents (id), kind TEXT NOT NULL,
    symbol TEXT NOT NULL, at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS intent_orders_intent ON intent_orders (intent_id);
CREATE TRIGGER IF NOT EXISTS intent_orders_frozen BEFORE UPDATE ON intent_orders
    BEGIN SELECT RAISE(ABORT, 'an intent''s order link never changes'); END;
CREATE TRIGGER IF NOT EXISTS intent_orders_no_delete BEFORE DELETE ON intent_orders
    BEGIN SELECT RAISE(ABORT, 'an intent''s order link never changes'); END;
"""

_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford


def new_id() -> str:
    """A ULID: 48 bits of milliseconds then 80 random bits, 26 Crockford base32 characters (sorts by time)."""
    n = (int(time.time() * 1000) << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    return "".join(_B32[(n >> (5 * i)) & 31] for i in reversed(range(26)))


def now_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Intent:
    """One write: `kind` names its handler (gateway.py), `args` are its keyword arguments, `symbol` the ccxt symbol
    it acts on (None: account-wide or several). Every intent carries its side (GATEWAY.md §7)."""
    kind: str
    args: dict[str, Any]
    symbol: str | None = None
    side: str = "short"
    id: str = field(default_factory=new_id)
    actor: str = field(default_factory=audit.actor)

    def __post_init__(self) -> None:
        if self.side not in SIDES:
            raise ValueError(f"side must be one of {SIDES}, got {self.side!r}")


def symbol_of(args: dict[str, Any]) -> str | None:
    s = args.get("symbol")
    if not isinstance(s, str) or not s:
        return None
    from .exchange import to_symbol   # here: exchange pulls in ccxt's exchange classes
    return to_symbol(s)


def dumps(obj: Any) -> str:
    """JSON as journalled: secrets blanked and signed URLs redacted (audit.clean), nothing cut."""
    return json.dumps(audit.clean(obj, trim=False), default=str, sort_keys=True)


# ------------------------------------------------------------ the row
def receive(j, it: Intent, refused: Any = None) -> dict[str, Any] | None:
    """Journal `it` as received (or straight away as refused, with the caller's refusal as its result). None when it is
    new; the stored row when its id was journalled before (a resubmission: never run again)."""
    st = audit.stamp()
    now = now_ms()
    done = refused is not None
    cur = j.db.execute(
        "INSERT INTO intents (id, kind, symbol, side, args, status, result, actor, claude_session, session_id,"
        " code_version, mode, pid, received_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT (id) DO NOTHING",
        (it.id, it.kind, it.symbol, it.side, dumps(it.args), "refused" if done else "received",
         dumps(refused) if done else None, it.actor, st["claude_session"], j.session_of(st["claude_session"]),
         st["code_version"], j.mode, os.getpid(), now, now if done else None))
    j.db.commit()
    return None if cur.rowcount == 1 else get(j, it.id)


def start(j, intent_id: str) -> bool:
    """received (or busy, run again) -> running. False: another caller started it first."""
    cur = j.db.execute("UPDATE intents SET status='running', started_at=? WHERE id=? AND status IN ('received','busy')",
                       (now_ms(), intent_id))
    j.db.commit()
    return cur.rowcount == 1


def finish(j, intent_id: str, status: str, result: Any = None, error: str | None = None) -> bool:
    """running -> `status`, with the result (today's shape) or the error. An intent the sweep ended while its owner
    was suspended takes the owner's answer: it is the truth."""
    tid = result.get("trade_id") if isinstance(result, dict) else None
    cur = j.db.execute(
        "UPDATE intents SET status=?, result=?, error=?, trade_id=?, finished_at=? WHERE id=?"
        " AND status IN ('running','interrupted')",
        (status, None if result is None else dumps(result), error,
         tid if isinstance(tid, int) and not isinstance(tid, bool) else None, now_ms(), intent_id))
    j.db.commit()
    return cur.rowcount == 1


def get(j, intent_id: str) -> dict[str, Any] | None:
    row = j.db.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    for k in ("args", "result"):
        out[k] = json.loads(out[k]) if out[k] is not None else None
    return out


# ------------------------------------------------------------ orders
def link(j, cid: str, symbol: str) -> None:
    """Called by a Trader primitive just before it sends an order under client id `cid`: the order belongs to the
    intent running in this task, if any. Bookkeeping only: the order's own row already carries `cid`, so a failed link
    is logged and never costs the send."""
    iid = audit.intent_id()
    if iid is None:
        return
    try:
        j.db.execute("INSERT INTO intent_orders (client_id, intent_id, kind, symbol, at) VALUES (?,?,?,?,?)",
                     (cid, iid, clientid.kind_of(cid), symbol, now_ms()))
        j.db.commit()
    except Exception:  # noqa: BLE001 - logged; the send it precedes is protected by its own write-ahead row
        log.exception("could not link order %s to intent %s", cid, iid)


def orders(j, intent_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in j.db.execute("SELECT * FROM intent_orders WHERE intent_id=? ORDER BY at, client_id",
                                          (intent_id,))]


# ------------------------------------------------------------ a process that died mid-intent
def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # another user's process: alive (D2 runs every desk process as one user anyway)
    return True


def sweep(j, now: float | None = None, alive: Callable[[int], bool] | None = None) -> list[dict[str, Any]]:
    """End this mode's open intents whose process is gone (one desk host, D2) or that have been open longer than
    MAX_OPEN_S: received -> expired (nothing ran), running -> interrupted. Returns them, each with its client ids."""
    now, alive = time.time() if now is None else now, alive or _alive
    out = []
    for r in j.db.execute("SELECT id, kind, symbol, actor, status, pid, received_at, started_at FROM intents"
                          " WHERE status IN ('received', 'running') AND mode=?", (j.mode,)).fetchall():
        since = datetime.fromisoformat(r["started_at"] or r["received_at"]).timestamp()
        if alive(r["pid"]) and now - since < MAX_OPEN_S:
            continue
        new = "expired" if r["status"] == "received" else "interrupted"
        why = f"still {r['status']}: its process {r['pid']} is gone, or it ran past {MAX_OPEN_S:g} s"
        cur = j.db.execute("UPDATE intents SET status=?, finished_at=?, error=? WHERE id=? AND status=?",
                           (new, now_ms(), why, r["id"], r["status"]))
        j.db.commit()
        if cur.rowcount:
            out.append({**dict(r), "status": new, "orders": [o["client_id"] for o in orders(j, r["id"])]})
    return out
