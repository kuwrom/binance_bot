"""Journal units (#79, journal_writer.py): a write on an event loop never waits for another process's write lock; units
are atomic, land in the order they were made, in their caller's context; a long wait names the lock's holder."""

import asyncio
import logging
import sqlite3
import time

import pytest

from shortbot import audit, journal_writer, wal_holder
from shortbot.journal import Journal
from tests.held_lock import STALL_REPORT, HeldLock


async def ticking(gaps: list[float], stop: asyncio.Event) -> None:
    last = time.monotonic()
    while not stop.is_set():
        await asyncio.sleep(0.005)
        now = time.monotonic()
        gaps.append(now - last)
        last = now


def kv(j, k):
    return j.get(k)


async def test_a_unit_is_atomic_even_when_its_helpers_commit(tmp_path):
    j = Journal(tmp_path / "j.db")

    def two(j):
        j.set("a", "1")                                          # commits: held until the unit's own COMMIT
        j.db.execute("INSERT INTO kv VALUES ('a', 'dup')")       # IntegrityError: the whole unit rolls back
    with pytest.raises(sqlite3.IntegrityError):
        await j.write(two)
    assert j.get("a") is None and not j.db.in_transaction
    assert await j.write(lambda j: j.set("b", "2") or 7) == 7 and j.get("b") == "2"


@STALL_REPORT
async def test_the_loop_never_waits_for_another_processs_write_lock(tmp_path):
    j = Journal(tmp_path / "j.db")
    gaps, stop = [], asyncio.Event()
    with HeldLock(tmp_path / "j.db", 2.0) as held:
        tick = asyncio.create_task(ticking(gaps, stop))
        await asyncio.sleep(0.05)
        await j.write(Journal.set, "k", "v")
        landed = time.time()
        stop.set()
        await tick
        released = held.wait()
    assert j.get("k") == "v"
    assert max(gaps) < 0.1                    # today: the loop slept the whole 2 s in SQLite's busy handler
    assert 0 <= landed - released < 0.15      # retried every POLL_S: committed right after the release


async def test_posts_and_writes_land_in_order(tmp_path):
    j = Journal(tmp_path / "j.db")
    with HeldLock(tmp_path / "j.db", 0.5):
        for i in range(20):
            assert j.post(Journal.set, "n", str(i)) is None          # queued: the lock is held
            assert j.log_decision("t", f"#{i}") is None
        assert j.w.pending() == 40
        await j.write(Journal.set, "last", "x")                      # waits for every post ahead of it
    assert j.w.pending() == 0 and j.get("n") == "19" and j.get("last") == "x"
    assert [d["reasoning"] for d in j.decisions(limit=50)] == [f"#{i}" for i in reversed(range(20))]


@STALL_REPORT
async def test_a_queued_unit_runs_in_its_callers_context_and_keeps_its_time(tmp_path):
    j = Journal(tmp_path / "j.db")
    with HeldLock(tmp_path / "j.db", 1.2) as held:
        called = time.time()
        with audit.acting("guardian:positions"), audit.within("01INTENT"):
            j.log_decision("plan_step", "moved", "BTC/USDT:USDT")
            j.log_alert("stop_near", "close", "BTC/USDT:USDT")
        await j.flush()
        released = held.wait()
    (d,), (a,) = j.decisions(), j.alerts()
    assert (d["actor"], d["intent_id"], a["actor"], a["intent_id"]) == ("guardian:positions", "01INTENT") * 2
    for row in (d, a):   # stamped at the call, not when the lock freed
        assert abs(_ts(row["ts"]) - called) < 1.0 < released - called


def _ts(iso: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(iso).timestamp()


async def test_a_cancelled_write_never_runs(tmp_path):
    j = Journal(tmp_path / "j.db")
    with HeldLock(tmp_path / "j.db", 0.3):
        t = asyncio.create_task(j.write(Journal.set, "gone", "1"))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        await j.write(Journal.set, "kept", "1")
    assert j.get("gone") is None and j.get("kept") == "1"


async def test_a_failing_post_is_logged_and_the_queue_goes_on(tmp_path, caplog):
    j = Journal(tmp_path / "j.db")

    def bad(j):
        raise ValueError("boom")
    with HeldLock(tmp_path / "j.db", 0.3):
        j.post(bad)
        j.post(Journal.set, "after", "1")
        await j.flush()
    assert j.get("after") == "1" and j.w.stats["failed_posts"] == 1 and "boom" in caplog.text
    j.post(bad)                                                      # the lock free: it fails at once, logged too
    assert j.w.stats["failed_posts"] == 2


async def test_an_awaited_write_raises_its_units_error_after_the_wait(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.set("a", "1")
    with HeldLock(tmp_path / "j.db", 0.3):
        with pytest.raises(sqlite3.IntegrityError):
            await j.write(lambda j: j.db.execute("INSERT INTO kv VALUES ('a', 'x')"))


def test_off_a_loop_a_post_is_a_plain_write():
    j = Journal(":memory:")
    assert isinstance(j.log_decision("t", "no loop"), int) and j.run(Journal.set, "k", "v") is None
    assert j.get("k") == "v"


async def test_an_in_memory_journal_writes_on_its_own_connection():
    j = Journal(":memory:")
    assert await j.write(lambda j: j._insert("INSERT INTO kv VALUES ('m', '1')", ())) == 1
    assert isinstance(j.log_alert("x", "y"), int) and j.get("m") == "1"


@STALL_REPORT
async def test_a_long_wait_is_logged_with_the_process_holding_the_lock(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(journal_writer, "STALL_LOG_S", (0.2,))
    j = Journal(tmp_path / "j.db")
    told = []
    j.w.on_stall = lambda waited, who: told.append((waited, who))
    caplog.set_level(logging.WARNING, "shortbot.journal")
    with HeldLock(tmp_path / "j.db", 1.0) as held:
        await j.write(Journal.set, "k", "v")
        await asyncio.sleep(0.2)   # the report runs in a thread
    ((waited, who),) = told
    assert waited >= 0.2 and who["pid"] == held.p.pid and "python" in who["cmd"].lower()
    assert f"held by pid {held.p.pid}" in caplog.text and "free again" in caplog.text


@STALL_REPORT
def test_the_holder_query_takes_no_lock(tmp_path):
    j = Journal(tmp_path / "j.db")
    assert wal_holder.holder(tmp_path / "j.db") is None          # free
    with HeldLock(tmp_path / "j.db", 5.0) as held:
        assert wal_holder.holder(tmp_path / "j.db")["pid"] == held.p.pid
    j.set("still", "mine")                                       # this process's own locks were never touched
    assert wal_holder.holder(tmp_path / "missing.db") is None
