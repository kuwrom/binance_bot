"""STX 2026-10-01 13:29 UTC (task #21): the guardian closed target 1 (461 of 922), and seconds later a session's CLI ran
`close STX --fraction 0.5` on the remaining 461, so the runner ended at 231. Order-changing sequences on a symbol are
now serialised across processes (symlock.py), and a partial close re-checks the caller's view inside the lock."""

import asyncio
from types import SimpleNamespace

import pytest

from shortbot.guardian import Guardian
from shortbot.journal import Journal
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks

PLAN = {"targets": [{"price": 0.95, "fraction": 0.5}, {"price": 0.9, "fraction": 0.5}], "auto": {"targets": True}}


def desk(tmp_path):
    """The guardian and a session: two processes (own journal connection, own lock table) on one DB and one
    exchange account."""
    fake, db = FakeCcxt(), tmp_path / "shortbot.db"
    ex = FakeEx(fake)
    guard = Trader(ex, Journal(db), prechecks=ok_prechecks, budget=neutral_budget)
    session = Trader(ex, Journal(db), prechecks=ok_prechecks, budget=neutral_budget)
    g = Guardian(SimpleNamespace(settings=ex.settings, journal=guard.j, ex=ex, trader=guard))

    async def quiet(*a, **k):
        return None
    g._squeeze_and_funding = g._news = quiet
    return g, session, fake


@pytest.fixture
def market_fallback(monkeypatch):
    """No target rests at entry (as when its sync there fails, #71), so a crossed target is the guardian's market
    close: the path these races are about."""
    async def none(*a, **k):
        return None
    monkeypatch.setattr("shortbot.targets_sync.at_entry", none)


async def short_with_targets(session):
    r = await session.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100, plan=PLAN)
    assert r["ok"] and r["amount"] == 100, r
    return r["trade_id"]


def closes(fake):
    return [o for o in fake.orders if o[0] == "market" and o[1] == "buy"]


@pytest.mark.usefixtures("market_fallback")
async def test_partial_close_right_after_the_guardian_took_the_target_is_refused(tmp_path):
    g, s, fake = desk(tmp_path)
    await short_with_targets(s)
    fake.price = 0.949
    await g.check_positions()                         # the guardian takes target 1: 50 of 100
    assert fake.position == 50
    r = await s.close(SYM, "take T1 per plan", fraction=0.5)   # the session saw 100 a moment ago
    assert not r["ok"] and "target already taken by the guardian since you looked" in r["error"], r
    assert "plan/target 0.95 hit" in r["error"] and "expect_contracts=50" in r["error"]
    assert fake.position == 50 and len(closes(fake)) == 1     # the runner is intact


async def test_a_stated_view_is_checked_against_the_live_position(tmp_path):
    g, s, fake = desk(tmp_path)
    await short_with_targets(s)
    fake.price = 0.949
    await g.check_positions()
    r = await s.close(SYM, "half", fraction=0.5, expect_contracts=100)
    assert not r["ok"] and "position is 50" in r["error"] and fake.position == 50
    r = await s.close(SYM, "half of what is left, on purpose", fraction=0.5, expect_contracts=50)
    assert r["ok"] and r["closed_amount"] == 25 and fake.position == 25


async def test_fraction_of_the_original_is_idempotent(tmp_path):
    g, s, fake = desk(tmp_path)
    await short_with_targets(s)
    fake.price = 0.949
    await g.check_positions()
    r = await s.close(SYM, "T1 by hand", fraction=0.5, of_original=True)   # 50% of 100 is already gone
    assert not r["ok"] and r["noop"] and fake.position == 50, r
    r = await s.close(SYM, "down to a quarter", fraction=0.75, of_original=True)
    assert r["ok"] and r["closed_amount"] == 25 and fake.position == 25, r


async def test_full_close_and_an_old_change_are_not_blocked(tmp_path, monkeypatch):
    g, s, fake = desk(tmp_path)
    await short_with_targets(s)
    fake.price = 0.949
    await g.check_positions()
    monkeypatch.setattr("shortbot.close_intent.RECENT_CHANGE_S", 0)   # the guardian's close is old news
    r = await s.close(SYM, "trim", fraction=0.5)
    assert r["ok"] and r["closed_amount"] == 25
    r = await s.close(SYM, "done", "lesson")          # a full close is idempotent: flat is flat
    assert r["ok"] and fake.position == 0


async def test_another_sessions_partial_counts_too(tmp_path):
    g, s, fake = desk(tmp_path)
    await short_with_targets(s)
    other = Trader(s.ex, Journal(tmp_path / "shortbot.db"), prechecks=ok_prechecks, budget=neutral_budget)
    assert (await other.close(SYM, "trim from the other session", fraction=0.5, expect_contracts=100))["ok"]
    r = await s.close(SYM, "trim", fraction=0.5)
    assert not r["ok"] and "changed since you looked" in r["error"] and fake.position == 50


async def test_the_session_waits_for_the_guardian_then_rechecks(tmp_path):
    g, s, fake = desk(tmp_path)
    await short_with_targets(s)
    guard, held = g.app.trader, asyncio.Event()

    async def guardian_sequence():
        async with guard.symbol_lock(SYM, "guardian plan"):
            held.set()
            await asyncio.sleep(0.2)                   # the session's close arrives meanwhile and must wait
            assert len(closes(fake)) == 0
            await guard.close(SYM, "plan/target 0.95 hit", fraction=0.5, expect_contracts=100)

    task = asyncio.create_task(guardian_sequence())
    await held.wait()
    r = await s.close(SYM, "half", fraction=0.5, expect_contracts=100)
    await task
    assert not r["ok"] and "position is 50" in r["error"] and fake.position == 50 and len(closes(fake)) == 1


@pytest.mark.usefixtures("market_fallback")
async def test_guardian_rechecks_its_plan_under_the_lock(tmp_path):
    """The guardian's snapshot says target 1 is due; while it waits for the lock the session takes T1 by hand. Under
    the lock the guardian re-reads and re-plans: the target is done, nothing more is closed."""
    g, s, fake = desk(tmp_path)
    tid = await short_with_targets(s)
    fake.price = 0.949
    session_in, real_positions = asyncio.Event(), g.app.trader.positions

    async def snapshot_then_let_session_in():
        rows = await real_positions()
        session_in.set()
        await asyncio.sleep(0.05)
        return rows
    g.app.trader.positions = snapshot_then_let_session_in

    async def session():
        await session_in.wait()
        async with s.symbol_lock(SYM, "close"):
            await asyncio.sleep(0.2)                     # the guardian is now waiting for the lock
            assert (await s.close(SYM, "T1 by hand", fraction=0.5, of_original=True))["ok"]

    await asyncio.gather(g.check_positions(), session())
    assert fake.position == 50 and len(closes(fake)) == 1
    assert 0.95 in s.j.get_trade(tid)["targets_hit"]


@pytest.mark.usefixtures("market_fallback")
async def test_guardian_skips_a_busy_symbol_and_escalates_only_when_it_stays_busy(tmp_path, monkeypatch):
    g, s, fake = desk(tmp_path)
    await short_with_targets(s)
    fake.price = 0.949
    g.app.trader.symlocks.wait_s = 0.05
    release = asyncio.Event()

    async def hold():
        async with s.symbol_lock(SYM, "a long session action"):
            await release.wait()

    h = asyncio.create_task(hold())
    await asyncio.sleep(0.01)
    for n in range(3):
        await g.check_positions()                     # skipped: not a job failure, no close sent
        kinds = [a["kind"] for a in g.j.alerts(100)]
        assert "position_check_error" not in kinds and fake.position == 100
        assert ("guardian_failing:position" in kinds) == (n == 2)
    release.set()
    await h
    await g.check_positions()
    assert fake.position == 50


@pytest.mark.parametrize("kw", [{"fraction": 0}, {"fraction": 1.5}])
async def test_bad_fraction(tmp_path, kw):
    _, s, _ = desk(tmp_path)
    assert not (await s.close(SYM, "x", **kw))["ok"]


async def test_a_close_below_one_step_is_refused_not_raised(tmp_path):
    """ccxt raises InvalidOrder when an amount truncates to 0 (the fake does too): sized() answers instead."""
    g, s, fake = desk(tmp_path)
    await short_with_targets(s)
    r = await s.close(SYM, "a sliver", fraction=0.0001, expect_contracts=100)
    assert not r["ok"] and "too small" in r["error"] and fake.position == 100
