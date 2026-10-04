"""Intents (#36 gateway steps 2-3, docs/GATEWAY.md §4-5): every write the desk makes, journalled on receipt before
anything runs, under an id its caller picks (a ULID). gateway.py runs them; this module is the record.

- An intent is a write command (its kind is the command's, its args the body's keyword arguments: commands.py) or a
  guardian action: restore_stop, force_close, cap_stop, plan_step, pull_targets, and the entry-row sends of the fill
  poll, the sweeps and the reconciler (entries_acts.py, orphans.py). It goes received -> running -> a final status
  (results.py, exact by what it sent): done | refused | failed | partial | unknown | interrupted | expired, or busy
  (nothing sent: it may run again under its id). An unknown or interrupted one is resolved once (resolver.py).
- The row replaces the #24 `call` decision row of a write: it holds the request (args, actor, Claude session, code)
  from before the call runs and its result after: the status, what was sent, today's result and the typed error
  (errors.OrderError.to_dict). Rows are append-only like decisions: the request never changes and a final status is
  final (triggers). Decision and alert rows written while an intent runs carry its id (audit.py).
- Its orders are linked by their #9 client id (`intent_orders`), written by the Trader primitives just before each
  send (`link`, or `send` around it), then what Binance answered (`settle`: the outcome, clientid.FULL / RAN); a send
  of unknown outcome stays without one until the resolver looks it up. The per-order write-ahead rows (entries,
  target_orders, the trade's open id) keep their own state and are reached through that id; nothing is copied.
- What a run sent is also kept in memory (`Ledger`, one per run, set by the gateway: `recording`), so its status never
  waits for the journal.
- A process that died mid-intent leaves it open: `sweep` (the guardian's reconcile job) ends it, expired if it never
  started, interrupted if it did, and lists its client ids; the resolver settles them. A row keeps the pid namespace of
  the process that received it (`ns`, procs.NS): the sweep asks after a pid only in its own namespace, since a pid seen
  from another one names another process or none (a guardian container and the host's MCP servers).
- Journal-first, with one exception (#79): an intent whose handler is registered late_ok (restoring or capping a stop,
  closing a naked position, protecting a fill, withdrawing resting entries: gateway.LATE_OK) sends after CLAIM_WAIT_S
  without its row when another process holds the journal's write lock. Its rows are posted, stamped when it ran, and
  marked journal_late; its sends are idempotent (client ids, one closePosition stop per side, compare-and-set rows).

Every function that takes the journal runs plain synchronous SQLite: the gateway runs each as a unit (journal.write /
post, journal_writer.py), so an event loop never waits for the write lock. A time is the caller's (`at`): a posted row
lands later, stamped when it happened."""

from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from . import audit, clientid, errors
from .procs import NS

if TYPE_CHECKING:
    from .journal import Journal
    from .results import Result

OPEN = ("received", "running")
UNRESOLVED = ("unknown", "interrupted")   # the resolver settles these (interrupted may get its owner's answer first)
MAX_OPEN_S = 3600.0   # longer than any intent runs: the sweep ends it even if its pid now belongs to another process
SIDES = ("short", "long")

SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, symbol TEXT, side TEXT NOT NULL, args TEXT NOT NULL,
    status TEXT NOT NULL, result TEXT, error TEXT, trade_id INTEGER,
    actor TEXT NOT NULL, claude_session TEXT, session_id INTEGER, code_version TEXT, mode TEXT NOT NULL,
    pid INTEGER NOT NULL, received_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, journal_late INTEGER,
    sent INTEGER, resolved_at TEXT, ns TEXT
);
CREATE INDEX IF NOT EXISTS intents_open ON intents (status) WHERE status IN ('received', 'running');
CREATE INDEX IF NOT EXISTS intents_unresolved ON intents (status) WHERE status IN ('unknown', 'interrupted');
CREATE INDEX IF NOT EXISTS intents_received ON intents (received_at);
CREATE TRIGGER IF NOT EXISTS intents_no_delete BEFORE DELETE ON intents
    BEGIN SELECT RAISE(ABORT, 'intents are append-only'); END;
CREATE TRIGGER IF NOT EXISTS intents_request_frozen BEFORE UPDATE OF id, kind, symbol, side, args, actor,
    claude_session, session_id, code_version, mode, pid, received_at ON intents
    BEGIN SELECT RAISE(ABORT, 'an intent''s request never changes'); END;
CREATE TABLE IF NOT EXISTS intent_orders (
    client_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES intents (id), kind TEXT NOT NULL,
    symbol TEXT NOT NULL, at TEXT NOT NULL, outcome TEXT
);
CREATE INDEX IF NOT EXISTS intent_orders_intent ON intent_orders (intent_id);
CREATE TRIGGER IF NOT EXISTS intent_orders_no_delete BEFORE DELETE ON intent_orders
    BEGIN SELECT RAISE(ABORT, 'an intent''s order link never changes'); END;
"""
# Created once the columns they name exist (migrate): a final status is final, an unknown one is only resolved (to done,
# refused or partial, stamped, nothing else changed); an order's link never changes and its outcome is set once.
TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS intents_settled BEFORE UPDATE ON intents
    WHEN OLD.status IN ('done', 'refused', 'failed', 'partial', 'expired')
      OR (OLD.status = 'unknown' AND (NEW.status NOT IN ('done', 'refused', 'partial') OR NEW.resolved_at IS NULL
          OR NEW.result IS NOT OLD.result OR NEW.error IS NOT OLD.error OR NEW.sent IS NOT OLD.sent
          OR NEW.finished_at IS NOT OLD.finished_at))
    BEGIN SELECT RAISE(ABORT, 'a finished intent never changes (an unknown one is resolved once)'); END;
CREATE TRIGGER IF NOT EXISTS intent_orders_settled BEFORE UPDATE ON intent_orders
    WHEN OLD.outcome IS NOT NULL OR NEW.client_id IS NOT OLD.client_id OR NEW.intent_id IS NOT OLD.intent_id
      OR NEW.kind IS NOT OLD.kind OR NEW.symbol IS NOT OLD.symbol OR NEW.at IS NOT OLD.at
    BEGIN SELECT RAISE(ABORT, 'an intent''s order link never changes and its outcome is set once'); END;
CREATE TRIGGER IF NOT EXISTS intents_ns_frozen BEFORE UPDATE OF ns ON intents
    BEGIN SELECT RAISE(ABORT, 'an intent''s request never changes'); END;
"""
# added since the tables were first created (Journal._migrate)
COLUMNS = {"intents": {"journal_late": "INTEGER", "sent": "INTEGER", "resolved_at": "TEXT", "ns": "TEXT"},
           "intent_orders": {"outcome": "TEXT"}}
RETIRED = ("intents_final", "intent_orders_frozen")   # step 2's triggers: replaced by TRIGGERS


def migrate(db: Any) -> None:
    """After the columns: step 2's triggers dropped where a journal still has them, then TRIGGERS."""
    for name in RETIRED:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone():
            db.execute(f"DROP TRIGGER IF EXISTS {name}")
    db.executescript(TRIGGERS)


_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford


def new_id() -> str:
    """A ULID: 48 bits of milliseconds then 80 random bits, 26 Crockford base32 characters (sorts by time)."""
    n = (int(time.time() * 1000) << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    return "".join(_B32[(n >> (5 * i)) & 31] for i in reversed(range(26)))


def now_ms() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


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
    from .exchange import to_symbol  # here: exchange pulls in ccxt's exchange classes
    return to_symbol(s)


def dumps(obj: Any) -> str:
    """JSON as journalled: secrets blanked and signed URLs redacted (audit.clean), nothing cut."""
    return json.dumps(audit.clean(obj, trim=False), default=str, sort_keys=True)


# ------------------------------------------------------------ the row
def receive(j: Journal, it: Intent, refused: Any = None, at: str | None = None,
            late: bool = False) -> dict[str, Any] | None:
    """Journal `it` as received (or straight away as refused, with the caller's refusal as its result: nothing sent;
    or, late, as running since `at`: it ran without waiting for its row). None when it is new; the stored row when its
    id was journalled before (a resubmission: never run again)."""
    st = audit.stamp()
    at = at or now_ms()
    done = refused is not None
    status = "refused" if done else "running" if late else "received"
    cur = j.db.execute(
        "INSERT INTO intents (id, kind, symbol, side, args, status, result, actor, claude_session, session_id,"
        " code_version, mode, pid, ns, received_at, started_at, finished_at, journal_late, sent)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING",
        (it.id, it.kind, it.symbol, it.side, dumps(it.args), status, dumps(refused) if done else None, it.actor,
         st["claude_session"], j.session_of(st["claude_session"]), st["code_version"], j.mode, os.getpid(), NS, at,
         at if late else None, at if done else None, 1 if late else None, 0 if done else None))
    j.db.commit()
    return None if cur.rowcount == 1 else get(j, it.id)


def start(j: Journal, intent_id: str, at: str | None = None) -> bool:
    """received (or busy, run again) -> running. False: another caller started it first."""
    cur = j.db.execute("UPDATE intents SET status='running', started_at=? WHERE id=? AND status IN ('received','busy')",
                       (at or now_ms(), intent_id))
    j.db.commit()
    return cur.rowcount == 1


def finish(j: Journal, intent_id: str, r: Result, at: str | None = None) -> bool:
    """running -> its result's status, with what was sent, today's result and the typed error. An intent the sweep
    ended while its owner was suspended takes the owner's answer: it is the truth."""
    tid = r.body.get("trade_id") if isinstance(r.body, dict) else None
    cur = j.db.execute(
        "UPDATE intents SET status=?, sent=?, result=?, error=?, trade_id=?, finished_at=? WHERE id=?"
        " AND status IN ('running','interrupted')",
        (r.status, None if r.sent is None else int(r.sent), None if r.body is None else dumps(r.body),
         None if r.error is None else dumps(r.error.to_dict()),
         tid if isinstance(tid, int) and not isinstance(tid, bool) else None, at or now_ms(), intent_id))
    j.db.commit()
    return cur.rowcount == 1


def get(j: Journal, intent_id: str) -> dict[str, Any] | None:
    row = j.db.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    for k in ("args", "result", "error"):
        out[k] = json.loads(out[k]) if out[k] is not None else None
    return out


# ------------------------------------------------------------ what a run sent
@dataclass
class Ledger:
    """What the running intent sent, in memory (its status must never wait for the journal): `sent` once any exchange
    write left, and each client id's outcome (None while in doubt)."""
    sent: bool = False
    orders: dict[str, str | None] = field(default_factory=dict)

    @property
    def doubt(self) -> list[str]:
        return [c for c, o in self.orders.items() if o is None]

    def has(self, *outcomes: str) -> bool:
        return any(o in outcomes for o in self.orders.values())


_ledger: ContextVar[Ledger | None] = ContextVar("intent_ledger", default=None)


@contextmanager
def recording() -> Iterator[Ledger]:
    """The ledger of the run inside (gateway.submit): the primitives below write to it."""
    led = Ledger()
    tok = _ledger.set(led)
    try:
        yield led
    finally:
        _ledger.reset(tok)


def sent() -> None:
    """Called by a Trader primitive just before an exchange write without a client id: a cancel, an edit, prepare."""
    if (led := _ledger.get()) is not None:
        led.sent = True


def link(j: Journal, cid: str, symbol: str) -> None:
    """Called by a Trader primitive just before it sends an order under client id `cid`: the order belongs to the
    intent running in this task, if any, in doubt until `settle`. Bookkeeping only (the order's own row already carries
    `cid`): posted, so it never holds up the send, and a failure is logged."""
    if (led := _ledger.get()) is not None:
        led.sent = True
        led.orders.setdefault(cid, None)
    iid = audit.intent_id()
    if iid is not None:
        j.post(_link, cid, iid, symbol, now_ms())


def settle(j: Journal, cid: str, outcome: str) -> None:
    """What the order sent under `cid` did, once its primitive knows (clientid.outcome, or rejected): posted."""
    if (led := _ledger.get()) is not None and cid in led.orders:
        led.orders[cid] = outcome
    if audit.intent_id() is not None:
        j.post(record, cid, outcome)


async def send(j: Journal, cid: str, symbol: str, request: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    """One order sent under client id `cid`: linked, sent, its answer settled. A definite refusal is `rejected`; an
    answer that may have run leaves it in doubt (the caller looks it up, or the resolver does)."""
    link(j, cid, symbol)
    try:
        o = await request
    except Exception as e:
        if not errors.classify(e).unknown:
            settle(j, cid, "rejected")
        raise
    settle(j, cid, clientid.outcome(cid, o))
    return o


def _link(j: Journal, cid: str, iid: str, symbol: str, at: str) -> None:
    j.db.execute("INSERT INTO intent_orders (client_id, intent_id, kind, symbol, at) VALUES (?,?,?,?,?)",
                 (cid, iid, clientid.kind_of(cid), symbol, at))
    j.db.commit()


def record(j: Journal, cid: str, outcome: str) -> bool:
    """An order's outcome, once (False: it had one)."""
    cur = j.db.execute("UPDATE intent_orders SET outcome=? WHERE client_id=? AND outcome IS NULL", (outcome, cid))
    j.db.commit()
    return cur.rowcount == 1


def orders(j: Journal, intent_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in j.db.execute("SELECT * FROM intent_orders WHERE intent_id=? ORDER BY at, rowid",
                                          (intent_id,))]


# ------------------------------------------------------------ resolution (resolver.py)
def unresolved(j: Journal) -> list[dict[str, Any]]:
    """This mode's unknown and interrupted intents that sent orders under client ids, oldest first."""
    return j._rows("SELECT * FROM intents WHERE status IN ('unknown', 'interrupted') AND mode=? AND EXISTS"
                   " (SELECT 1 FROM intent_orders o WHERE o.intent_id = intents.id) ORDER BY received_at", (j.mode,))


def resolve(j: Journal, intent_id: str, was: str, status: str, at: str) -> bool:
    """unknown (or interrupted) -> done | refused | partial, once."""
    cur = j.db.execute("UPDATE intents SET status=?, resolved_at=? WHERE id=? AND status=?",
                       (status, at, intent_id, was))
    j.db.commit()
    return cur.rowcount == 1


# ------------------------------------------------------------ a process that died mid-intent
def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # another user's process: alive (D2 runs every desk process as one user anyway)
    return True


def sweep(j: Journal, now: float | None = None, alive: Callable[[int], bool] | None = None) -> list[dict[str, Any]]:
    """End this mode's open intents whose process is gone (one desk host, D2) or that have been open longer than
    MAX_OPEN_S: received -> expired (nothing ran), running -> interrupted (it may have sent: the resolver settles its
    orders). Returns them, each with its client ids. A pid is judged only in this process's pid namespace: a row
    from another one (a container's, or the host's seen from a container), or from before `ns`, ends by age alone."""
    now, alive = time.time() if now is None else now, alive or _alive
    out = []
    for r in j.db.execute("SELECT id, kind, symbol, actor, status, pid, ns, received_at, started_at FROM intents"
                          " WHERE status IN ('received', 'running') AND mode=?", (j.mode,)).fetchall():
        since = datetime.fromisoformat(r["started_at"] or r["received_at"]).timestamp()
        if (r["ns"] != NS or alive(r["pid"])) and now - since < MAX_OPEN_S:
            continue
        new = "expired" if r["status"] == "received" else "interrupted"
        why = (f"still {r['status']}: its process {r['pid']} is gone, or it ran past {MAX_OPEN_S:g} s"
               if r["ns"] == NS else f"still {r['status']} after {MAX_OPEN_S:g} s (its process {r['pid']} is in "
               "another pid namespace: not judged by its pid)")
        err = errors.OrderError(None, why) if new == "expired" else errors.OutcomeUnknown(None, why)
        cur = j.db.execute("UPDATE intents SET status=?, sent=?, finished_at=?, error=? WHERE id=? AND status=?",
                           (new, 0 if new == "expired" else None, now_ms(), dumps(err.to_dict()), r["id"],
                            r["status"]))
        j.db.commit()
        if cur.rowcount:
            out.append({**dict(r), "status": new, "orders": [o["client_id"] for o in orders(j, r["id"])]})
    return out
