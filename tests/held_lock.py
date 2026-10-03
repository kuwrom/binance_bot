"""Another process holding a journal's write lock (#79): BEGIN IMMEDIATE in a child, released after `hold_s` (or on
exit). `released` is the wall time of its COMMIT."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# a writer kept waiting >= 1 s asks `python -m shortbot.wal_holder` who holds the lock (journal_writer.STALL_LOG_S): a
# read-only child, no network, that tests/net_guard.py refuses unless the test says so
STALL_REPORT = pytest.mark.spawns_shortbot

_CHILD = """
import sqlite3, sys, time
d = sqlite3.connect(sys.argv[1], isolation_level=None)
d.execute('BEGIN IMMEDIATE'); print('held', flush=True)
time.sleep(float(sys.argv[2])); d.execute('COMMIT'); print(repr(time.time()), flush=True)
"""


class HeldLock:
    def __init__(self, db: str | Path, hold_s: float):
        self.db, self.hold_s = str(db), hold_s
        self.released: float | None = None

    def __enter__(self) -> HeldLock:
        self.p = subprocess.Popen([sys.executable, "-c", _CHILD, self.db, str(self.hold_s)], stdout=subprocess.PIPE,
                                  text=True)
        assert self.p.stdout and self.p.stdout.readline().strip() == "held"
        return self

    def wait(self) -> float:
        """Until the child committed: its release time."""
        if self.released is None:
            assert self.p.stdout
            self.released = float(self.p.stdout.readline())
            self.p.wait(10)
        return self.released

    def __exit__(self, *exc: object) -> None:
        if self.released is None:
            self.p.kill()
            self.p.wait(10)
