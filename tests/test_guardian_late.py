"""#36 gateway step 2b under another process's journal write lock (#79): the late_ok kinds (a fill's protection, the
HALTED sweep's withdrawal) send about CLAIM_WAIT_S in, before their rows land (journal_late); a strict kind (a flat
symbol's target pull) journals first and sends only once the lock is free."""

import asyncio
import time

import pytest

from shortbot import killswitch as ks
from shortbot.journal import Journal
from tests.gateway_rig import guardian_transport  # noqa: F401 - the suite runs locally and through the gateway
from tests.held_lock import STALL_REPORT, HeldLock
from tests.test_entries import favorable, prop
from tests.test_entries_cancel import oid_at
from tests.test_guardian_offloop import guardian_on
from tests.test_killswitch import SETTINGS
from tests.test_target_paths import book, placed

pytestmark = pytest.mark.usefixtures("guardian_transport")

HOLD_S = 4.0


def row(j: Journal, kind: str) -> dict:
    (r,) = j._rows("SELECT * FROM intents WHERE kind=?", (kind,))
    return r


async def until(cond, limit_s: float) -> float:
    """Seconds until cond() held (the job runs meanwhile), or the limit."""
    t0 = time.monotonic()
    while not cond() and time.monotonic() - t0 < limit_s:  # noqa: ASYNC110 - a fake's state: no event to wait on
        await asyncio.sleep(0.02)
    return time.monotonic() - t0


@STALL_REPORT
async def test_a_fill_is_protected_under_a_held_lock_and_its_intent_lands_late(tmp_path):
    g = guardian_on(tmp_path / "j.db")
    fake = g.app.ex.fake
    g.entries._regime_fn = favorable
    g.j.set(ks.HEARTBEAT_KEY, str(time.time()))
    assert (await g.entries.propose_group([prop(1.04)]))["ok"]
    fake.fill_limit(oid_at(fake, 1.04))                          # the first pass sees the fill: a cold ladder cache
    with HeldLock(tmp_path / "j.db", HOLD_S) as held:
        task = asyncio.create_task(g.check_entries())
        took = await until(lambda: fake.stops, HOLD_S - 0.5)
        assert list(fake.stops.values()) == [1.10] and 1.5 < took < 3.0   # protected, the journal still locked
        held.wait()
        await task                                               # its journalling waited for the lock
    await g.j.flush()
    r = row(g.j, "protect_fill")
    assert (r["status"], r["journal_late"], r["actor"] is not None) == ("done", 1, True)
    assert [t["status"] for t in g.j.trades()] == ["open"]


@STALL_REPORT
async def test_the_halted_sweep_withdraws_under_a_held_lock_and_its_intent_lands_late(tmp_path):
    g = guardian_on(tmp_path / "j.db")
    fake = g.app.ex.fake
    g.entries._regime_fn = favorable
    g.j.set(ks.HEARTBEAT_KEY, str(time.time()))
    assert (await g.entries.propose_group([prop(1.04)]))["ok"]
    await ks.trip(g.j, SETTINGS, ks.HALTED, "agent (MCP)", "halt")   # by a process that couldn't withdraw them
    with HeldLock(tmp_path / "j.db", HOLD_S) as held:
        task = asyncio.create_task(g.check_entries())
        took = await until(lambda: not [o for o in fake.limits.values() if o["status"] == "open"], HOLD_S - 0.5)
        assert 1.5 < took < 3.0 and fake.prearms == {}           # withdrawn, the journal still locked
        held.wait()
        await task
    await g.j.flush()
    r = row(g.j, "withdraw_entries")
    assert (r["status"], r["journal_late"]) == ("done", 1) and g.entries.pending() == []


@STALL_REPORT
async def test_a_strict_target_pull_journals_before_it_sends_under_a_held_lock(tmp_path):
    g, fake, tid = await placed(tmp_path)
    fake.ro_expire_on_flat = False
    fake.trigger_stop()
    with HeldLock(tmp_path / "shortbot.db", 2.0) as held:
        task = asyncio.create_task(g.check_positions())
        await until(lambda: book(fake) == [], 1.5)
        assert book(fake) != []                                  # nothing cancelled while its row can't land
        released = held.wait()
        await task
    assert book(fake) == [] and time.time() >= released
    await g.j.flush()
    r = row(g.j, "pull_targets")
    assert (r["status"], r["journal_late"]) == ("done", None)
