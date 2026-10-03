"""The guardian's event loop never waits for the journal's write lock (#79). Another process holding it for 5 s delays
no protective pass; a missing stop is still restored within ~2 s (its intent journalled late, marked); strict intents
still journal before they send; and no guardian job writes on the loop outside a unit (journal_writer.py)."""

import asyncio
import sqlite3
import time
from types import SimpleNamespace

import pytest

from shortbot import gateway, intents
from shortbot import guardian as gmod
from shortbot.guardian import Guardian
from shortbot.intents import Intent
from shortbot.journal import Journal
from shortbot.trading import Trader
from tests.fakes import FakeCcxt, FakeEx, neutral_budget, ok_prechecks
from tests.held_lock import HeldLock
from tests.loop_guard import loop_writes
from tests.test_entry_races import favorable, prop
from tests.test_guardian import open_trade
from tests.test_guardian_loop import SLOW_JOBS, NoStream, noop


def guardian_on(path, fake: FakeCcxt | None = None) -> Guardian:
    """test_guardian.guardian_with on a journal FILE: another process can hold its write lock."""
    ex = FakeEx(fake or FakeCcxt())
    j = Journal(path)
    tr = Trader(ex, j, prechecks=ok_prechecks, budget=neutral_budget)
    g = Guardian(SimpleNamespace(settings=ex.settings, journal=j, ex=ex, trader=tr))

    async def quiet(*a, **k):
        return None
    g._squeeze_and_funding = quiet
    g._news = quiet
    return g


async def test_a_held_write_lock_delays_no_protective_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(gmod, "TICK_S", 0.01)
    monkeypatch.setattr(gmod, "RESEARCH_START_DELAY_S", 3600)
    monkeypatch.setattr(gmod, "PROTECTIVE", {"entries": (0.01, 5), "positions": (0.01, 5)})
    monkeypatch.setattr(gmod, "WATCH", {"watch": (0.01, 5)})
    monkeypatch.setattr(gmod, "LiquidationRecorder", NoStream)
    g = guardian_on(tmp_path / "j.db")
    await open_trade(g)                                       # a protected short: every pass checks it
    for name in SLOW_JOBS:
        if name != "watch_positions":
            setattr(g, name, noop)
    passes: dict[str, list[float]] = {"entries": [], "positions": [], "watch": []}
    for job, fn in (("entries", g.check_entries), ("positions", g.check_positions), ("watch", g.watch_positions)):
        async def timed(job=job, fn=fn):
            await fn()
            passes[job].append(time.monotonic())
        setattr(g, {"entries": "check_entries", "positions": "check_positions", "watch": "watch_positions"}[job], timed)
    hb0 = g.j.get("guardian_heartbeat")
    with HeldLock(tmp_path / "j.db", 5.0) as held:
        task = asyncio.create_task(g.run())
        await asyncio.sleep(5.3)
        held.wait()
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    for job, ts in passes.items():
        assert len(ts) > 100, job                              # ~every 13 ms, for 5.6 s
        assert max(b - a for a, b in zip(ts, ts[1:])) < 0.1, job
    assert g.j.w.pending() == 0 and g.j.get("guardian_heartbeat") != hb0   # the bookkeeping landed after the release
    assert not [a for a in g.j.alerts(100) if a["kind"].startswith("guardian_error")]


async def test_a_missing_stop_is_restored_under_a_held_lock_and_its_intent_lands_late(tmp_path):
    fake = FakeCcxt()
    g = guardian_on(tmp_path / "j.db", fake)
    await open_trade(g)
    fake.stops.clear()
    with HeldLock(tmp_path / "j.db", 5.0) as held:
        t0 = time.monotonic()
        await g.check_positions()
        took = time.monotonic() - t0
        assert list(fake.stops.values()) == [1.05]             # protected now, the journal still locked
        assert 1.5 < took < 3.0                                # waited CLAIM_WAIT_S for the journal, not 5 s
        released = held.wait()
    await g.j.flush()
    (row,) = [r for r in _intents(g.j) if r["kind"] == "restore_stop"]
    assert (row["status"], row["journal_late"]) == ("done", 1)
    assert intents.orders(g.j, row["id"])                      # its stop's client id, linked after the fact
    assert row["received_at"] < _iso(released)                 # stamped when it ran, not when it landed


async def test_a_strict_intent_journals_before_it_sends_even_under_a_held_lock(tmp_path):
    j = Journal(tmp_path / "j.db")
    seen = []

    @gateway.handler("probe_79_strict")
    async def probe(ctx):
        ro = sqlite3.connect(f"file:{tmp_path / 'j.db'}?mode=ro", uri=True)
        seen.append(ro.execute("SELECT status FROM intents WHERE kind='probe_79_strict'").fetchone())
        return {"ok": True}
    try:
        with HeldLock(tmp_path / "j.db", 1.0) as held:
            res = await gateway.LocalGateway(lambda: j).submit(Intent("probe_79_strict", {}))
            ran = time.time()
            released = held.wait()
    finally:
        gateway.HANDLERS.pop("probe_79_strict")
    assert res == {"ok": True} and seen == [("running",)] and ran >= released   # waited, then journalled, then ran
    await j.flush()
    assert [r["status"] for r in _intents(j)] == ["done"]


async def test_no_protective_or_watch_job_writes_on_the_loop_outside_a_unit(tmp_path):
    fake = FakeCcxt()
    g = guardian_on(tmp_path / "a.db", fake)
    await open_trade(g, breakeven_at_pct=2)
    with loop_writes(g) as bad:
        for i in range(3):                                     # quiet passes, a move down (MAE/MFE, breakeven)
            fake.price = 1.0 - 0.02 * i
            await g.check_entries()
            await g.check_positions()
            await g.reconcile_entries()
            await g.watch_positions()
        fake.stops.clear()                                     # restore a stop
        await g.check_positions()
        fake.price = 1.2
        fake.trigger_stop()                                    # stopped out: reconciled with real P&L
        await g.check_positions()
    assert [t["status"] for t in g.j.trades()] == ["closed"]
    fake2 = FakeCcxt()
    g2 = guardian_on(tmp_path / "b.db", fake2)
    g2.entries._regime_fn = favorable
    assert (await g2.entries.propose_group([prop(1.04)]))["ok"]   # a session's limit entry (not the guardian's)
    with loop_writes(g2) as bad2:
        fake2.price = 1.05                                     # it fills: the fill poll journals and protects it
        await g2.check_entries()
        await g2.check_positions()
        await g2.reconcile_entries()
    assert [t["status"] for t in g2.j.trades()] == ["open"]
    with loop_writes(g2) as bad3:                              # the whole guardian: startup, every lane, its tasks
        await running(g2, 0.5)
    assert not bad + bad2 + bad3, "\n".join(sorted({b.split(': ')[0] for b in bad + bad2 + bad3}))


async def running(g: Guardian, seconds: float) -> None:
    for name in SLOW_JOBS:                                     # jobs that read the internet (test_guardian_loop.py)
        if name != "watch_positions":
            setattr(g, name, noop)
    task = asyncio.create_task(g.run())
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _intents(j):
    return [dict(r) for r in j.db.execute("SELECT * FROM intents ORDER BY received_at")]


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")
