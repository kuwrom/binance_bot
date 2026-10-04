"""Journal writes that never make an event loop wait for SQLite's write lock (#79).

The journal is one SQLite file in WAL mode shared by the guardian, the MCP servers, CLI calls and research jobs, and WAL
has one writer at a time (https://www.sqlite.org/wal.html). A statement that needs the write lock while another
connection holds it sleeps in SQLite's busy handler for up to the connection's timeout (15 s): on the guardian's event
loop that froze every lane, the protective one included (22 such stalls in a week of guardian.log).

Here a write is a *unit*: one function run inside BEGIN IMMEDIATE ... COMMIT on the caller's own connection
(https://www.sqlite.org/lang_transaction.html). Its BEGIN never waits on a loop: with the busy timeout at 0 it fails at
once when another connection holds the lock (https://www.sqlite.org/c3ref/busy_timeout.html), and the unit is queued
and retried every POLL_S by a task on the loop. Measured on a lock held 5 s by another process: the loop never stalls,
and the unit commits ~5 ms after the release (a worker thread in SQLite's busy handler: 54-102 ms, its sleeps grow to
100 ms; it also needs a second connection, which an in-memory journal can't share).

- `await write(fn, *a)`: fn's result once committed. The caller waits (its lane does), the loop doesn't.
- `post(fn, *a)`: a write nobody waits for (bookkeeping): committed now when the lock is free, else queued; its error
  is logged, never raised. Returns fn's result when it ran now, else None.
- One queue per connection, first in first out across write and post, so an awaited write also lands every earlier
  post. While another process holds the lock, this process's own reads lag what it posted (by the hold, at most),
  except the kv markers it set with Journal.mark (#81).
- A unit runs fn synchronously, in the caller's context (audit stamp, intent id), atomically: commit() inside it is held
  (UnitConnection), so helpers that commit after each statement become one transaction; an exception rolls it back.
  A write or post made inside a unit is part of it.
- A backlog drains in batches, one per loop iteration (#81): up to BATCH_MAX queued units, or BATCH_CPU_S of the loop
  thread's CPU, in one BEGIN IMMEDIATE ... COMMIT, in order. Each unit runs in its own SAVEPOINT: one that raises is
  rolled back alone and the rest commit (https://www.sqlite.org/lang_savepoint.html). Callers learn their outcome once
  the batch is committed. An error SQLite answers by rolling back the whole transaction (SQLITE_FULL, SQLITE_IOERR,
  SQLITE_NOMEM: https://www.sqlite.org/lang_transaction.html), or a failed COMMIT, fails every unit of the batch.
  Why: a COMMIT is one fsync of the WAL, and one unit per COMMIT drained ~350 units/s on EC2's gp3 disk. 3,000
  queued decisions with a real flush per fsync (macOS F_FULLFSYNC): 18.3 s one per COMMIT, 0.17 s in batches.
- Durability: synchronous=FULL, pinned. In WAL mode FULL syncs the WAL at every COMMIT; with NORMAL a committed
  transaction "might roll back following a power loss or system crash" (https://www.sqlite.org/pragma.html
  #pragma_synchronous, https://www.sqlite.org/wal.html). A journal-first intent must outlive the order it sent, so
  the desk keeps FULL and pays one fsync per batch instead.
- `run(fn, *a)`: the same unit for a synchronous caller off a loop (a CLI body, a script): it waits per the timeout.
- A unit kept waiting STALL_LOG_S is logged with the process that holds the lock (wal_holder.py), and on_stall is told.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import sqlite3
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, TypeVar

from . import wal_holder

log = logging.getLogger("shortbot.journal")
T = TypeVar("T")
POLL_S = 0.005                      # a queued unit tries the lock again this often
BATCH_MAX = 200                     # queued units committed together: one COMMIT (one WAL fsync) per batch
BATCH_CPU_S = 0.02                  # nor more of the loop thread's CPU per batch (CPU: a busy host never shrinks it)
STALL_LOG_S = (1.0, 10.0, 60.0)     # a wait this long is logged, with the lock's holder
Outcome = tuple[Any] | Exception    # a unit's (result,), or what it raised


class UnitConnection(sqlite3.Connection):
    """A connection whose commit() waits for the unit it runs in. `unit` names the unit running: write | post (never
    waits for the lock) or run (waits per the busy timeout)."""
    unit_depth = 0
    unit: str | None = None

    def commit(self) -> None:
        if not self.unit_depth:
            super().commit()

    def __exit__(self, typ: type[BaseException] | None, val: BaseException | None, tb: TracebackType | None, /
                 ) -> Literal[False]:   # `with db:` inside a unit: the unit commits or rolls back
        if not self.unit_depth:
            super().__exit__(typ, val, tb)
        return False

    def executescript(self, sql: str, /) -> sqlite3.Cursor:
        if self.unit_depth:   # it COMMITs first (Python sqlite3 docs): the unit would end half-way
            raise sqlite3.ProgrammingError("executescript inside a journal unit: run schema DDL before the unit")
        return super().executescript(sql)

    def rollback(self) -> None:
        if self.unit_depth:   # it would undo the units batched before this one too
            raise sqlite3.ProgrammingError("rollback inside a journal unit: raise, and the unit alone rolls back")
        super().rollback()


def connect(path: str | Path, timeout_s: float = 15.0) -> UnitConnection:
    """The journal file (or a store beside it in it): WAL, a busy timeout for writers off a loop, units for the rest."""
    db = sqlite3.connect(str(path), check_same_thread=False, timeout=timeout_s, factory=UnitConnection)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")   # SQLite's default, pinned: see the module doc
    return db


@dataclass
class _Queued:
    fn: Callable[..., Any]
    a: tuple[Any, ...]
    k: dict[str, Any]
    ctx: contextvars.Context
    fut: asyncio.Future[Any] | None
    since: float = field(default_factory=time.monotonic)


def _busy(e: sqlite3.OperationalError) -> bool:
    return "locked" in str(e) or "busy" in str(e)


class Writer:
    def __init__(self, db: UnitConnection):
        self.db = db
        self.busy_ms = int(db.execute("PRAGMA busy_timeout").fetchone()[0])
        self.path: str | None = next((r[2] for r in db.execute("PRAGMA database_list") if r[1] == "main"), None) or None
        self.on_stall: Callable[[float, dict[str, Any] | None], None] | None = None
        self._q: deque[_Queued] = deque()
        self._task: asyncio.Task[None] | None = None
        self._reports: set[asyncio.Task[None]] = set()
        self._logged = 0                 # STALL_LOG_S thresholds already reported for the unit at the head
        self.stats: dict[str, float] = {"queued": 0, "failed_posts": 0, "max_wait_s": 0.0}

    # ------------------------------------------------------------ the unit
    def _begin(self, wait: bool) -> bool:
        """BEGIN IMMEDIATE. False when another connection holds the lock and `wait` is off."""
        if not wait and self.busy_ms:
            self.db.execute("PRAGMA busy_timeout=0")
        try:
            self.db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            if wait or not _busy(e):
                raise
            return False
        finally:
            if not wait and self.busy_ms:
                self.db.execute(f"PRAGMA busy_timeout={self.busy_ms}")
        return True

    def _unit(self, kind: str, fn: Callable[..., T], a: tuple[Any, ...], k: dict[str, Any]) -> tuple[T] | None:
        """Run fn as one unit: (its result,), or None when the lock is held elsewhere (and kind never waits)."""
        db = self.db
        if db.unit_depth:
            return (fn(*a, **k),)
        if db.in_transaction:   # a statement outside any unit failed and left its implicit BEGIN open
            log.warning("journal: rolled back a transaction left open outside a unit")
            db.rollback()
        if not self._begin(wait=kind == "run"):
            return None
        db.unit_depth, db.unit = 1, kind
        try:
            out = fn(*a, **k)
        except BaseException:
            db.unit_depth, db.unit = 0, None
            db.rollback()
            raise
        db.unit_depth, db.unit = 0, None
        db.commit()
        return (out,)

    # ------------------------------------------------------------ the API
    def run(self, fn: Callable[..., T], *a: Any, **k: Any) -> T:
        res = self._unit("run", fn, a, k)
        assert res is not None   # run waits: it gets the lock or raises
        return res[0]

    async def write(self, fn: Callable[..., T], *a: Any, **k: Any) -> T:
        if self._free():
            res = self._unit("write", fn, a, k)
            if res is not None:
                return res[0]
        fut: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        self._queue(_Queued(fn, a, k, contextvars.copy_context(), fut))
        return await fut

    def post(self, fn: Callable[..., T], *a: Any, **k: Any) -> T | None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:   # no loop to keep free: a plain write, waiting per the busy timeout
            kind = "run"
        else:
            kind = "post"
            if not self._free():
                self._queue(_Queued(fn, a, k, contextvars.copy_context(), None))
                return None
        try:
            res = self._unit(kind, fn, a, k)
        except Exception:  # noqa: BLE001 - a posted write is bookkeeping: logged, never its caller's failure
            self.stats["failed_posts"] += 1
            log.exception("journal: posted write %s failed", getattr(fn, "__qualname__", fn))
            return None
        if res is None:
            self._queue(_Queued(fn, a, k, contextvars.copy_context(), None))
            return None
        return res[0]

    def _free(self) -> bool:
        """Nothing queued ahead, and no statement of this thread's own transaction in progress."""
        return self.db.unit_depth > 0 or not (self._q or self.db.in_transaction)

    async def flush(self) -> None:
        """Until every unit queued so far is written."""
        if self._q:
            await self.write(lambda: None)

    async def drain(self, timeout_s: float) -> None:
        """At exit: flush, or log what is lost after timeout_s (the lock held elsewhere all that time)."""
        try:
            await asyncio.wait_for(self.flush(), timeout_s)
        except TimeoutError:
            log.error("journal: %d queued write(s) lost: the write lock stayed held for %.0f s at exit", self.pending(),
                      timeout_s)

    def pending(self) -> int:
        return len(self._q)

    # ------------------------------------------------------------ the queue
    def _queue(self, q: _Queued) -> None:
        self._q.append(q)
        self.stats["queued"] += 1
        loop = asyncio.get_running_loop()
        if self._task is None or self._task.done() or self._task.get_loop() is not loop:
            self._task = loop.create_task(self._drain(), name="journal-writer")

    async def _drain(self) -> None:
        while self._q:
            head = self._q[0]
            ran = self._batch()
            if ran is None:
                self._stalled(head)
                await asyncio.sleep(POLL_S)
                continue
            self._done(head)
            for q, res in ran:
                self._settle(q, res)
            await asyncio.sleep(0)   # one batch per loop iteration

    def _batch(self) -> list[tuple[_Queued, Outcome]] | None:
        """The queue's head as one batch (module doc): each unit run with its outcome, once committed, and taken off the
        queue; None when another connection holds the lock. A unit whose caller gave up (cancelled, timed out) is
        dropped unrun. KeyboardInterrupt and the like roll the batch back and leave its units queued."""
        db = self.db
        if db.in_transaction:   # a statement outside any unit failed and left its implicit BEGIN open
            log.warning("journal: rolled back a transaction left open outside a unit")
            db.rollback()
        if not self._begin(wait=False):
            return None
        out: list[tuple[_Queued, Outcome]] = []
        taken, cpu0 = 0, time.thread_time()
        try:
            for q in self._q:   # no unit can queue another: a write or post inside a unit runs in it
                if out and (len(out) >= BATCH_MAX or time.thread_time() - cpu0 >= BATCH_CPU_S):
                    break
                taken += 1
                if q.fut is not None and q.fut.done():
                    continue
                res = self._savepoint(q)
                if isinstance(res, Exception) and not db.in_transaction:   # SQLite rolled the whole batch back
                    raise res
                out.append((q, res))
            db.commit()
        except Exception as e:  # noqa: BLE001 - the batch is lost: each of its units fails with e
            if db.in_transaction:
                db.rollback()
            out = [(q, e) for q in islice(self._q, taken) if q.fut is None or not q.fut.done()]
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        for _ in range(taken):
            self._q.popleft()
        return out

    def _savepoint(self, q: _Queued) -> Outcome:
        """One queued unit inside the batch's transaction, in its caller's context: its result, or its exception once
        its own writes are rolled back."""
        db = self.db
        db.execute("SAVEPOINT unit")
        db.unit_depth, db.unit = 1, "post" if q.fut is None else "write"
        try:
            res = q.ctx.run(q.fn, *q.a, **q.k)
        except Exception as e:  # noqa: BLE001 - its outcome: handed to its caller, or logged for a post
            db.unit_depth, db.unit = 0, None
            if db.in_transaction:
                db.execute("ROLLBACK TO unit")
                db.execute("RELEASE unit")
            return e
        finally:
            db.unit_depth, db.unit = 0, None
        db.execute("RELEASE unit")
        return (res,)

    def _settle(self, q: _Queued, res: Outcome) -> None:
        if isinstance(res, Exception):
            if q.fut is None:
                self.stats["failed_posts"] += 1
                log.error("journal: posted write %s failed: %s", getattr(q.fn, "__qualname__", q.fn), res)
            elif not q.fut.done():
                q.fut.set_exception(res)
        elif q.fut is not None and not q.fut.done():
            q.fut.set_result(res[0])

    def _done(self, q: _Queued) -> None:
        waited = time.monotonic() - q.since
        self.stats["max_wait_s"] = max(self.stats["max_wait_s"], waited)
        if self._logged:
            log.warning("journal: write lock free again after %.1f s", waited)
        self._logged = 0

    def _stalled(self, q: _Queued) -> None:
        waited = time.monotonic() - q.since
        if self._logged < len(STALL_LOG_S) and waited >= STALL_LOG_S[self._logged]:
            self._logged += 1
            t = asyncio.get_running_loop().create_task(self._report(waited))
            self._reports.add(t)
            t.add_done_callback(self._reports.discard)

    async def _report(self, waited: float) -> None:
        who = await asyncio.to_thread(wal_holder.holder, self.path) if self.path else None
        log.warning("journal: %d write(s) waiting %.0f s for the write lock, held by %s", len(self._q), waited,
                    f"pid {who['pid']} ({who['cmd']})" if who else "an unknown process")
        if self.on_stall:
            self.on_stall(waited, who)
