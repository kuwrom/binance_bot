"""One order-changing sequence per symbol at a time, across every process on this host (task #21; STX 2026-10-01
13:29 UTC: the guardian took target 1, and seconds later a session's partial close halved the runner).

The guardian, each MCP server and every CLI call are separate processes. Each order-changing sequence on a symbol
(close, stop moves, entries, cancels, amends, plan actions...) runs inside `Trader.symbol_lock(symbol)`:
- cross-process: flock(2) on `<db dir>/locks/<SYMBOL>.lock`. A flock belongs to the open file description, so a
  crash or kill releases it; a second open() conflicts even in the same process (Linux flock(2), checked on macOS).
  fcntl/lockf record locks can't do this: a process never conflicts with itself, and closing ANY descriptor of the
  file drops all of them (fcntl(2)). Lock files are never deleted (unlinking a held lock file breaks exclusion).
- in-process: an asyncio.Lock per symbol in front of the flock, so tasks queue in order instead of polling.
- never blocks the event loop: LOCK_NB try-lock with async backoff until a deadline, then SymbolBusy (nothing sent).
  A blocking flock in a thread couldn't be cancelled, and would take the lock after its waiter was gone.
- reentrant per asyncio task (add_to_winner -> move_stop, close -> cancel_legs, guardian plan -> close). A task
  never holds two symbols: asking for a second one is a bug (RuntimeError), so symbols can't deadlock each other.
Lock order: Trader.lock (process-wide, open_short only) -> one symbol lock -> SQLite writes. Never wait for a symbol
lock while holding an open SQLite write transaction: autocommit writes inside the lock are fine.
Limits: advisory and same-host only (not the Binance app, not a guardian on another host or behind a VM mount);
the real fix is one gateway process that owns every exchange write (#36).
"""

from __future__ import annotations

import asyncio
import atexit
import errno
import fcntl
import json
import os
import re
import shutil
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from .exchange import to_raw

LOCK_WAIT_S = 60.0       # session callers: a close can hold it ~1-10 s, a degraded network far longer
GUARDIAN_WAIT_S = 5.0    # guardian protective lane: skip the symbol and retry next pass, never stall the others
_BUSY = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}   # LOCK_NB refusals (Python fcntl docs: check them all)


class SymbolBusy(RuntimeError):
    """Another order-changing sequence holds this symbol: nothing was sent. Re-read the state, then retry."""

    def __init__(self, symbol: str, waited_s: float, holder: str):
        super().__init__(f"{symbol} is busy: {holder}; waited {waited_s:g} s and sent nothing. Re-read the "
                         "position and orders, then retry")
        self.symbol, self.holder = symbol, holder


@dataclass
class _Hold:
    task: asyncio.Task | None
    action: str
    depth: int = 1


_MEM_ROOT: str | None = None


def lock_dir_for(db) -> Path:
    """Next to the journal file the processes share (SHORTBOT_DB may be relative to each one's working directory).
    An in-memory journal (tests) gets a private directory, with real flocks all the same, removed at exit."""
    global _MEM_ROOT
    path = next((r[2] for r in db.execute("PRAGMA database_list").fetchall() if r[1] == "main"), "")
    if path:
        return Path(path).parent / "locks"
    if _MEM_ROOT is None:
        _MEM_ROOT = tempfile.mkdtemp(prefix="shortbot-locks-")
        atexit.register(shutil.rmtree, _MEM_ROOT, True)
    return Path(tempfile.mkdtemp(dir=_MEM_ROOT))


def _key(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", to_raw(symbol))


def _process() -> str:
    return " ".join([Path(sys.argv[0]).name, *sys.argv[1:2]]) if sys.argv and sys.argv[0] else "python"


def _holder(fd: int) -> str:
    """Who holds it, from what the holder wrote into the file (informational: flock is advisory, reads aren't)."""
    try:
        h = json.loads(os.pread(fd, 1024, 0) or b"{}")
        return f"held by pid {h['pid']} ({h['process']}) for {h['action'] or 'an order action'} since {h['since']}"
    except (OSError, ValueError, KeyError, TypeError):
        return "held by another process"


class SymbolLocks:
    def __init__(self, lock_dir: Path | str, wait_s: float = LOCK_WAIT_S):
        self.dir, self.wait_s = Path(lock_dir), wait_s
        self._local: dict[str, asyncio.Lock] = {}
        self._held: dict[str, _Hold] = {}

    def holds(self, symbol: str) -> bool:
        """True if the current task holds this symbol's lock (the lock-audit tests use it)."""
        h = self._held.get(_key(symbol))
        return bool(h) and h.task is asyncio.current_task()

    @asynccontextmanager
    async def hold(self, symbol: str, action: str = "", wait_s: float | None = None) -> AsyncIterator[None]:
        key, task = _key(symbol), asyncio.current_task()
        mine = self._held.get(key)
        if mine and mine.task is task:   # reentrant: this task already runs a sequence on this symbol
            mine.depth += 1
            try:
                yield
            finally:
                mine.depth -= 1
            return
        other = next((k for k, h in self._held.items() if h.task is task), None)
        if other:
            raise RuntimeError(f"nested symbol locks: this task holds {other} and asked for {key} ({action}); "
                               "finish one symbol's sequence before starting another")
        wait = self.wait_s if wait_s is None else wait_s
        deadline = time.monotonic() + wait
        local = self._local.setdefault(key, asyncio.Lock())
        try:
            async with asyncio.timeout(max(deadline - time.monotonic(), 0.001)):
                await local.acquire()
        except TimeoutError:
            raise SymbolBusy(symbol, wait, "held by another task of this process") from None
        try:
            async with self._flock(symbol, key, action, wait, deadline):
                self._held[key] = _Hold(task, action)
                try:
                    yield
                finally:
                    self._held.pop(key, None)
        finally:
            local.release()

    @asynccontextmanager
    async def _flock(self, symbol: str, key: str, action: str, wait: float, deadline: float) -> AsyncIterator[None]:
        self.dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.dir / f"{key}.lock", os.O_RDWR | os.O_CREAT, 0o664)   # non-inheritable (PEP 446)
        try:
            delay = 0.05
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as e:
                    if e.errno not in _BUSY:
                        raise
                left = deadline - time.monotonic()
                if left <= 0:
                    raise SymbolBusy(symbol, wait, _holder(fd))
                await asyncio.sleep(min(delay, left))
                delay = min(delay * 2, 0.5)
            _describe(fd, action)
            try:
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass   # closing the descriptor below releases it anyway
        finally:
            os.close(fd)


def _describe(fd: int, action: str) -> None:
    info: dict[str, Any] = {"pid": os.getpid(), "process": _process(), "action": action,
                            "since": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps(info).encode(), 0)
    except OSError:
        pass   # diagnostics only: the lock is held either way
