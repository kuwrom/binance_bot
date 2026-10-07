"""Guard (#79): a write the guardian runs on its event loop outside a non-waiting unit (journal_writer.py) is a write
that can sleep in SQLite's busy handler and stall every lane. `loop_writes(*dbs)` collects each such statement on
these connections, with the code that ran it; `connections(g)` are the journal and every store the guardian holds."""

from __future__ import annotations

import asyncio
import os
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from shortbot import weight_ledger

WRITES = ("INSERT", "UPDATE", "DELETE", "REPLACE")
STORES = ("journal.py", "journal_research.py", "journal_learnings.py", "journal_writer.py", "intents.py")


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _site() -> str:
    for f in reversed(traceback.extract_stack()[:-3]):
        if "/shortbot/" in f.filename and os.path.basename(f.filename) not in STORES:
            return f"{os.path.basename(f.filename)}:{f.lineno} {f.name}"
    return "?"


def connections(g: Any) -> list[Any]:
    out = [g.j.db]
    for name in ("liq_store", "social_store"):
        store = getattr(g, name, None)
        if store is not None:
            out.append(store.db)
    led = weight_ledger.get()   # the host-wide weight ledger: the guardian writes it on the loop, never waiting
    if led is not None:
        out.append(led.connection())
    return out


@contextmanager
def loop_writes(*dbs: Any) -> Iterator[list[str]]:
    bad: list[str] = []

    def watch(db: Any):
        def cb(sql: str) -> None:
            verb = sql.lstrip().split(None, 1)[0].upper()
            unit = getattr(db, "unit", None) if getattr(db, "unit_depth", 0) else None
            if verb in WRITES and _on_loop() and unit in (None, "run"):
                bad.append(f"{_site()}: {' '.join(sql.split())[:70]}")
        return cb
    for db in dbs:
        if db.execute("PRAGMA busy_timeout").fetchone()[0] == 0:
            continue   # busy_timeout 0 (the weight ledger in the guardian): a write there fails, it never sleeps
        db.set_trace_callback(watch(db))
    try:
        yield bad
    finally:
        for db in dbs:
            db.set_trace_callback(None)
