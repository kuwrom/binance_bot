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
  post. While another process holds the lock, this process's own reads lag what it posted (by the hold, at most).
- A unit runs fn synchronously, in the caller's context (audit stamp, intent id), atomically: commit() inside it is held
  (UnitConnection), so helpers that commit after each statement become one transaction; an exception rolls it back.
  A write or post made inside a unit is part of it.
- One unit per loop iteration: a backlog drained after a long hold never holds the loop.
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
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Callable, Literal, TypeVar

from . import wal_holder

log = logging.getLogger("shortbot.journal")
T = TypeVar("T")
POLL_S = 0.005                      # a queued unit tries the lock again this often
STALL_LOG_S = (1.0, 10.0, 60.0)     # a wait this long is logged, with the lock's holder


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
            q = self._q[0]
            if q.fut is not None and q.fut.done():   # its caller gave up (cancelled, timed out)
                self._q.popleft()
                continue
            kind = "post" if q.fut is None else "write"
            try:
                res = q.ctx.run(self._unit, kind, q.fn, q.a, q.k)
            except Exception as e:  # noqa: BLE001 - handed to the writer that waits for it, or logged for a post
                self._q.popleft()
                self._done(q)
                if q.fut is not None:
                    q.fut.set_exception(e)
                else:
                    self.stats["failed_posts"] += 1
                    log.error("journal: posted write %s failed: %s", getattr(q.fn, "__qualname__", q.fn), e)
                await asyncio.sleep(0)
                continue
            if res is None:
                self._stalled(q)
                await asyncio.sleep(POLL_S)
                continue
            self._q.popleft()
            self._done(q)
            if q.fut is not None:
                q.fut.set_result(res[0])
            await asyncio.sleep(0)   # one unit per loop iteration

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
