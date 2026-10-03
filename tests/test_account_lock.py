"""Task #74: the account lock. Two sessions adding risk on DIFFERENT symbols at the same moment must not both pass the
portfolio caps (max_positions, total notional): every risk-adding flow holds one cross-process lock from its cap checks
through the send. Lock order: the account lock, then one symbol lock, never the reverse. Risk-reducing paths and the
guardian never take it."""

import asyncio
import threading

import pytest

from shortbot.config import RiskLimits
from shortbot.entries import EntryDesk
from shortbot.journal import Journal
from shortbot.positioning import PositionOps
from shortbot.symlock import AccountBusy, SymbolLocks
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks
from tests.test_entries import favorable, prop
from tests.test_lock_paths import guardian

SYM2 = "MOVR/USDT:USDT"


def sessions_race(tmp_path, limits: RiskLimits, *jobs):
    """Each job runs as its own session, like two MCP servers: its own thread and event loop, its own journal connection
    and Trader (so its own lock table), on one DB file and one exchange account. A barrier right after each session's
    account read makes both read the account before either sends: the race, unless a lock orders them."""
    fake, db = FakeCcxt(), tmp_path / "shortbot.db"
    fake.add_symbol(SYM2, 1.0)
    Journal(db)   # the schema, once, before the sessions start
    gate, out = threading.Barrier(len(jobs)), [None] * len(jobs)

    def run(i, job):
        async def go():
            t = Trader(FakeEx(fake, limits), Journal(db), prechecks=ok_prechecks, budget=neutral_budget)
            read = t.account_state

            async def account_state():
                acct = await read()
                try:
                    gate.wait(timeout=0.5)   # blocks only this session's loop; the lock holder breaks it after 0.5 s
                except threading.BrokenBarrierError:
                    pass
                return acct
            t.account_state = account_state
            out[i] = await job(t)
        try:
            asyncio.run(go())
        except BaseException as e:  # noqa: BLE001 - reported by the test's assertion
            out[i] = e
    threads = [threading.Thread(target=run, args=(i, job)) for i, job in enumerate(jobs)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    assert not [r for r in out if isinstance(r, BaseException) or r is None], out
    return out, fake


def opener(symbol):
    async def job(t):
        return await t.open_short(symbol, stop_price=1.05, setup="pump_fade", thesis="t", notional=100)
    return job


def proposer(symbol):
    async def job(t):
        return await EntryDesk(t, regime_fn=favorable).propose_group([prop(1.04, symbol=symbol)])
    return job


ONE_SLOT = {"positions": RiskLimits(max_positions=1), "notional": RiskLimits(max_total_notional=150)}


@pytest.mark.parametrize("cap", ONE_SLOT)
def test_two_sessions_opening_different_symbols_with_room_for_one_open_exactly_one(tmp_path, cap):
    (a, b), fake = sessions_race(tmp_path, ONE_SLOT[cap], opener(SYM), opener(SYM2))
    assert sorted([a["ok"], b["ok"]]) == [False, True], (a, b)
    loser = a if not a["ok"] else b
    assert loser.get("rejected_by_risk"), loser
    assert [p for p in [fake.position, fake.sym[SYM2]["qty"]] if p] == [100]


@pytest.mark.parametrize("cap", ONE_SLOT)
def test_an_open_and_a_limit_entry_racing_for_the_last_slot_pass_exactly_once(tmp_path, cap):
    (a, b), fake = sessions_race(tmp_path, ONE_SLOT[cap], opener(SYM), proposer(SYM2))
    assert sorted([a["ok"], b["ok"]]) == [False, True], (a, b)
    resting = [o for o in fake.limits.values() if o["status"] == "open"]
    assert bool(fake.position) == a["ok"] and bool(resting) == b["ok"]


# ------------------------------------------------------------ the lock itself
async def test_lock_order_is_account_then_symbol_never_the_reverse(tmp_path):
    locks = SymbolLocks(tmp_path)
    async with locks.account("open"), locks.hold(SYM, "open"):   # the one allowed order
        assert locks.holds_account() and locks.holds(SYM)
        async with locks.account("again"):                         # reentrant in one task
            pass
    async with locks.hold(SYM, "close"):
        with pytest.raises(RuntimeError, match="lock order"):
            async with locks.account("add while holding a symbol"):
                pass
    assert not locks.holds_account()


async def test_the_account_lock_excludes_another_process_and_times_out_with_a_clear_refusal(tmp_path):
    mine, other = SymbolLocks(tmp_path, wait_s=0.05), SymbolLocks(tmp_path)
    release = asyncio.Event()

    async def hold():
        async with other.account("open short X (pump_fade)"):
            await release.wait()
    h = asyncio.create_task(hold())
    await asyncio.sleep(0.01)
    with pytest.raises(AccountBusy, match="another session is adding risk.*open short X.*retry"):
        async with mine.account("open short Y"):
            pass
    async with mine.hold(SYM, "close"):   # symbols stay free: protection never waits for the account lock
        pass
    release.set()
    await h
    async with mine.account("open short Y"):
        pass


# ------------------------------------------------------------ who takes it, who never does
async def held_account(tr, release: asyncio.Event):
    async with tr.account_lock("another session adding risk"):
        await release.wait()


def two(tmp_path):
    fake, db = FakeCcxt(), tmp_path / "shortbot.db"
    fake.add_symbol(SYM2, 1.0)
    return [Trader(FakeEx(fake), Journal(db), prechecks=ok_prechecks, budget=neutral_budget) for _ in range(2)] + [fake]


async def test_while_another_session_adds_risk_every_risk_adding_path_refuses_and_sends_nothing(tmp_path):
    t, other, fake = two(tmp_path)
    d = EntryDesk(t, regime_fn=favorable)
    assert (await t.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100))["ok"]
    eid = (await d.propose_group([prop(1.04, symbol=SYM2)]))["placed"][0]["entry_id"]
    fake.price = 0.95
    sent = len(fake.orders)
    t.symlocks.wait_s = 0.05
    release = asyncio.Event()
    h = asyncio.create_task(held_account(other, release))
    await asyncio.sleep(0.01)
    refusals = [await t.open_short("ETH/USDT:USDT", stop_price=1.05, setup="pump_fade", thesis="t", notional=50),
                await PositionOps(t).add_to_winner(SYM, 20, 1.0, "add"),
                await PositionOps(t).widen_stop(SYM, 1.08, "noise"),
                await d.amend(eid, 1.03, "lower")]
    for r in refusals:
        assert r["ok"] is False and r["busy"] and "another session is adding risk" in r["error"], r
    p = await d.propose_group([prop(1.04, symbol="ETH/USDT:USDT")])
    assert not p["ok"] and "another session is adding risk" in p["rejected"][0]["error"]
    assert len(fake.orders) == sent and fake.position == 100
    release.set()
    await h


async def test_risk_reducing_paths_and_the_guardian_never_wait_for_the_account_lock(tmp_path):
    t, other, fake = two(tmp_path)
    d, g = EntryDesk(t, regime_fn=favorable), guardian(t)
    assert (await t.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100))["ok"]
    r = await d.propose_group([prop(1.04, symbol=SYM2)])
    eid, gid = r["placed"][0]["entry_id"], r["group_id"]
    t.symlocks.wait_s = 30   # a path that waited for the held account lock would time this test out
    release = asyncio.Event()
    h = asyncio.create_task(held_account(other, release))
    await asyncio.sleep(0.01)
    fake.stops.clear()   # the stop vanished: the guardian restores it
    await asyncio.wait_for(g.check_positions(), 5)
    assert list(fake.stops.values()) == [1.05]
    assert (await asyncio.wait_for(t.move_stop(SYM, 1.04, "tighten"), 5))["ok"]
    assert (await asyncio.wait_for(d.amend(eid, 1.05, "raise: less risk"), 5))["ok"]
    assert (await asyncio.wait_for(t.close(SYM, "trim", fraction=0.5), 5))["ok"]
    await asyncio.wait_for(d.cancel_group(gid, "done"), 5)
    assert d.pending() == [] and fake.position == 50
    release.set()
    await h
