"""The per-symbol order lock (symlock.py, task #21): real flock(2) across processes, never blocking the event loop,
reentrant per task, no second symbol, released on cancel, and a timeout that says who holds it and sends nothing."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from shortbot.journal import Journal
from shortbot.symlock import SymbolBusy, SymbolLocks, lock_dir_for

STX, ETH = "STX/USDT:USDT", "ETH/USDT:USDT"
SRC = str(Path(__file__).resolve().parents[1] / "src")
HOLDER = """
import asyncio, sys
from shortbot.symlock import SymbolLocks
async def main():
    async with SymbolLocks(sys.argv[1]).hold("STX", "guardian plan close"):
        print("held", flush=True)
        await asyncio.get_running_loop().run_in_executor(None, sys.stdin.read)   # until the test lets go
asyncio.run(main())
"""


def holder_process(lock_dir) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock_dir)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True, env={**os.environ, "PYTHONPATH": SRC})
    assert p.stdout.readline().strip() == "held"
    return p


async def test_another_process_holding_the_symbol_makes_us_wait_without_blocking_the_loop(tmp_path):
    p = holder_process(tmp_path)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    tick = asyncio.create_task(ticker())
    try:
        with pytest.raises(SymbolBusy) as e:
            async with SymbolLocks(tmp_path).hold(STX, "close", wait_s=0.3):
                pytest.fail("entered while another process held the lock")
        assert f"pid {p.pid}" in str(e.value) and "guardian plan close" in str(e.value) and "sent nothing" in str(e.value)
        assert ticks >= 10   # the event loop kept running while we waited
        p.stdin.close()      # the holder lets go...
        p.wait(10)
        async with SymbolLocks(tmp_path).hold(STX, "close", wait_s=2):   # ...and we get it
            pass
    finally:
        tick.cancel()
        p.kill()


async def test_a_killed_holder_releases_the_lock(tmp_path):
    p = holder_process(tmp_path)
    p.kill()
    p.wait(10)
    async with SymbolLocks(tmp_path).hold(STX, "close", wait_s=1):
        pass


async def test_two_lock_tables_on_one_dir_exclude_each_other_like_two_processes(tmp_path):
    a, b = SymbolLocks(tmp_path), SymbolLocks(tmp_path)   # independent open()s: flock treats them separately
    order = []
    release = asyncio.Event()

    async def first():
        async with a.hold(STX, "guardian target close"):
            order.append("a in")
            await release.wait()
            order.append("a out")

    t = asyncio.create_task(first())
    await asyncio.sleep(0.01)
    with pytest.raises(SymbolBusy, match="guardian target close"):
        async with b.hold(STX, "cli close", wait_s=0.2):
            pass

    async def second():
        async with b.hold(STX, "cli close", wait_s=5):
            order.append("b in")

    s = asyncio.create_task(second())
    await asyncio.sleep(0.1)
    async with b.hold(ETH, "other symbol", wait_s=0.2):   # other symbols are not blocked
        order.append("eth")
    release.set()
    await asyncio.gather(t, s)
    assert order == ["a in", "eth", "a out", "b in"]


async def test_reentrant_in_one_task_exclusive_across_tasks(tmp_path):
    locks = SymbolLocks(tmp_path)
    async with locks.hold(STX, "add_to_winner"):
        async with locks.hold(STX, "move_stop"):   # add_to_winner -> move_stop: same task, same symbol
            assert locks.holds(STX)
        assert locks.holds(STX)

        async def other_task():
            async with locks.hold(STX, "close", wait_s=0.1):
                pass
        with pytest.raises(SymbolBusy, match="another task of this process"):
            await asyncio.create_task(other_task())
    assert not locks.holds(STX)


async def test_a_second_symbol_in_the_same_task_is_a_bug(tmp_path):
    locks = SymbolLocks(tmp_path)
    async with locks.hold(STX, "close"):
        with pytest.raises(RuntimeError, match="nested symbol locks"):
            async with locks.hold(ETH, "cancel"):
                pass
    async with locks.hold(ETH, "cancel"):   # one after the other is fine
        pass


async def test_cancelling_a_holder_or_a_waiter_leaks_nothing(tmp_path):
    locks, other = SymbolLocks(tmp_path), SymbolLocks(tmp_path)

    async def hold_forever():
        async with locks.hold(STX, "close"):
            await asyncio.sleep(3600)

    h = asyncio.create_task(hold_forever())
    await asyncio.sleep(0.01)
    w = asyncio.create_task(other.hold(STX, "waiter", wait_s=30).__aenter__())
    await asyncio.sleep(0.1)
    w.cancel()                                  # a cancelled waiter
    h.cancel()                                  # a cancelled holder (e.g. a guardian job timeout)
    await asyncio.gather(h, w, return_exceptions=True)
    for lk in (locks, other):
        async with lk.hold(STX, "after", wait_s=0.5):
            pass


def test_locks_live_next_to_the_journal_file(tmp_path):
    j = Journal(tmp_path / "data" / "shortbot.db")
    assert lock_dir_for(j.db) == tmp_path / "data" / "locks"
    assert lock_dir_for(Journal(":memory:").db).is_dir()   # tests: a private dir, real flocks


def readonly_lock(tmp_path, content=b'{"pid": 1, "process": "old", "action": "x", "since": "then"}'):
    from shortbot.symlock import _key
    f = tmp_path / f"{_key(STX)}.lock"
    f.write_bytes(content)
    f.chmod(0o444)
    return f


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a 0444 file")
async def test_a_lock_file_this_user_cannot_write_still_excludes_other_processes(tmp_path):
    """#58: flock works on a read-only descriptor (flock(2): 'regardless of the mode in which the file was opened';
    checked on macOS). Only the holder info is lost: the file isn't rewritten."""
    f = readonly_lock(tmp_path)
    p = holder_process(tmp_path)          # the child falls back to O_RDONLY too
    try:
        with pytest.raises(SymbolBusy):
            async with SymbolLocks(tmp_path).hold(STX, "close", wait_s=0.2):
                pytest.fail("entered while another process held the read-only lock")
    finally:
        p.stdin.close()
        p.wait(10)
    async with SymbolLocks(tmp_path).hold(STX, "close", wait_s=2):
        assert b'"pid": 1' in f.read_bytes()


async def test_read_only_fallback_when_open_for_writing_is_refused(tmp_path, monkeypatch):
    from shortbot import symlock
    f = readonly_lock(tmp_path)
    real_open = os.open

    def no_write(path, flags, *a):
        if flags & (os.O_RDWR | os.O_WRONLY):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, *a)
    monkeypatch.setattr(symlock.os, "open", no_write)
    a, b = SymbolLocks(tmp_path), SymbolLocks(tmp_path)
    async with a.hold(STX, "close"):
        with pytest.raises(SymbolBusy):
            async with b.hold(STX, "other", wait_s=0.1):
                pass
    assert b'"pid": 1' in f.read_bytes()
    with pytest.raises(PermissionError):    # no file and no way to create it: the open error, not a silent no-lock
        async with a.hold(ETH, "close"):
            pass
