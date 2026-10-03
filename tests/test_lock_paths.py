"""Order paths under the per-symbol lock (task #21): re-checks once the lock is held, the guardian's short wait, lock
order (no deadlock), no SQLite write transaction across an exchange call, and the audit that guards it all."""

import asyncio
from types import SimpleNamespace

from shortbot import killswitch as ks
from shortbot import ladder_apply, tranches
from shortbot.entries import EntryDesk
from shortbot.guardian import Guardian
from shortbot.journal import Journal
from shortbot.positioning import PositionOps
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks
from tests.test_entries import favorable, prop

STOP = {"closePosition": True, "triggerPrice": 1.05, "workingType": "MARK_PRICE"}


def two(tmp_path, fake=None):
    """Two processes (own journal connection, own lock table) on one DB and one account."""
    fake, db = fake or FakeCcxt(), tmp_path / "shortbot.db"
    ex = FakeEx(fake)
    return [Trader(ex, Journal(db), prechecks=ok_prechecks, budget=neutral_budget) for _ in range(2)] + [fake]


def guardian(tr) -> Guardian:
    g = Guardian(SimpleNamespace(settings=tr.ex.settings, journal=tr.j, ex=tr.ex, trader=tr))

    async def quiet(*a, **k):
        return None
    g._squeeze_and_funding = g._news = quiet
    return g


async def short(tr, **kw):
    r = await tr.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100, **kw)
    assert r["ok"], r
    return r


async def held_by(tr, release: asyncio.Event, action="a session action"):
    async with tr.symbol_lock(SYM, action):
        await release.wait()


def kinds(j):
    return [a["kind"] for a in j.alerts(200)]


async def test_the_audit_catches_an_order_sent_outside_its_lock(order_lock_audit, monkeypatch, tmp_path):
    t, _, _ = two(tmp_path)

    class NoLock:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return None
    monkeypatch.setattr(Trader, "symbol_lock", lambda self, *a, **k: NoLock())
    await short(t)
    assert any("open_short" in b for b in order_lock_audit)
    order_lock_audit.clear()


async def test_cancel_legs_holds_no_write_transaction_across_an_exchange_call(monkeypatch, tmp_path):
    t, _, _ = two(tmp_path)
    t.j.db.executescript("CREATE TABLE IF NOT EXISTS entries (id INTEGER PRIMARY KEY, symbol TEXT, status TEXT, "
                         "reason TEXT)")
    t.j.db.executemany("INSERT INTO entries (id, symbol, status) VALUES (?, ?, 'pending')", [(1, SYM), (2, SYM)])
    t.j.db.commit()
    seen = []

    async def withdraw(_t, e, alert=None):
        seen.append(t.j.db.in_transaction)   # an open write here would span this (network) await
        return "unfilled"
    monkeypatch.setattr("shortbot.entries_cancel.withdraw", withdraw)
    monkeypatch.setattr(tranches, "own_legs", lambda j, tid: j._rows("SELECT * FROM entries ORDER BY id"))
    got = await tranches.cancel_legs(t, 7, "trade closed")
    assert seen == [False, False] and len(got["unfilled"]) == 2 and not t.j.db.in_transaction
    assert {r["status"] for r in t.j._rows("SELECT status FROM entries")} == {"cancelled"}


async def test_a_limit_entry_never_re_levers_an_open_position():
    d = EntryDesk(Trader(FakeEx(fake := FakeCcxt()), Journal(":memory:"), prechecks=ok_prechecks,
                         budget=neutral_budget), regime_fn=favorable)
    await short(d.tr, leverage=2)
    assert fake.sym[SYM]["lev"] == 2
    r = await d.propose_group([prop(1.04, leverage=3)])
    assert not r["ok"] and "already short" in r["rejected"][0]["error"] and fake.sym[SYM]["lev"] == 2


async def test_a_position_that_appears_after_the_risk_check_stops_the_placement_inside_the_lock(order_lock_audit):
    d = EntryDesk(Trader(FakeEx(fake := FakeCcxt()), Journal(":memory:"), prechecks=ok_prechecks,
                         budget=neutral_budget), regime_fn=favorable)
    real = fake.prod.fetch_ohlcv

    async def fill_meanwhile(*a, **k):   # the read right after the risk check: another order fills now
        await fake.create_order(SYM, "market", "sell", 50)
        fake.sym[SYM]["lev"] = 2
        return await real(*a, **k)
    fake.prod.fetch_ohlcv = fill_meanwhile
    r = await d.propose_group([prop(1.04, leverage=3)])
    assert not r["ok"] and "opened since the checks" in r["rejected"][0]["error"]
    assert fake.sym[SYM]["lev"] == 2 and fake.limits == {} and not d._rows("1=1")
    assert order_lock_audit == [f"create_order({SYM}) via entries_place.propose_group outside its symbol lock"]
    order_lock_audit.clear()   # that is the simulated fill (another order), sent from inside this call chain


async def test_a_trip_while_waiting_for_the_lock_refuses_the_entry(tmp_path):
    t, s, fake = two(tmp_path)
    release = asyncio.Event()
    h = asyncio.create_task(held_by(s, release))
    await asyncio.sleep(0.01)
    task = asyncio.create_task(short_or_refusal(t))
    await asyncio.sleep(0.2)
    await ks.trip(s.j, s.ex.settings, ks.REDUCING, "test", "news shock")
    release.set()
    r = await task
    await h
    assert not r["ok"] and r["kill_switch"] == ks.REDUCING and fake.position == 0 and fake.orders == []


async def short_or_refusal(t):
    return await t.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100)


async def test_guardian_forced_close_rechecks_that_the_position_is_still_naked(tmp_path, monkeypatch, order_lock_audit):
    t, _, fake = two(tmp_path)
    g = guardian(t)
    await short(t)
    fake.stops.clear()
    g.stop_fails[SYM] = 2

    async def restore_fails_but_a_session_set_a_stop(sym, pct):
        await fake.create_order(SYM, "STOP_MARKET", "buy", None, None, STOP)
        raise RuntimeError("-1021 again")
    monkeypatch.setattr(t, "ensure_stop", restore_fails_but_a_session_set_a_stop)
    await g.check_positions()
    assert order_lock_audit == [f"create_order({SYM}) via guardian_positions.check_positions outside its symbol lock"]
    order_lock_audit.clear()   # that stop is the simulated other process's, sent from inside this call chain
    assert fake.position == 100 and SYM not in g.stop_fails
    assert "NOT closed" in next(a["message"] for a in t.j.alerts(50) if a["kind"] == "forced_close")


async def test_a_busy_symbol_is_not_a_failed_stop_restore(tmp_path):
    t, s, fake = two(tmp_path)
    g = guardian(t)
    await short(t)
    fake.stops.clear()
    t.symlocks.wait_s = 0.05
    release = asyncio.Event()
    h = asyncio.create_task(held_by(s, release))
    await asyncio.sleep(0.01)
    for _ in range(4):
        await g.check_positions()
    assert g.stop_fails == {} and fake.position == 100 and "forced_close" not in kinds(t.j)
    release.set()
    await h
    await g.check_positions()
    assert list(fake.stops.values()) == [1.05]


async def test_a_fill_on_a_busy_symbol_stays_pending_and_is_not_a_poll_failure(tmp_path):
    t, s, fake = two(tmp_path)
    d = EntryDesk(t, regime_fn=favorable)
    r = await d.propose_group([prop(1.04)])
    t.symlocks.wait_s = 0.05
    release = asyncio.Event()
    h = asyncio.create_task(held_by(s, release))
    await asyncio.sleep(0.01)
    fake.fill_limit(next(iter(fake.limits)))
    assert await d.poll() == [] and d._poll_fails == {} and "entry_poll_failing" not in kinds(t.j)
    assert d.pending()[0]["id"] == r["placed"][0]["entry_id"]
    release.set()
    await h
    assert [e["event"] for e in await d.poll()] == ["filled"] and list(fake.stops.values()) == [1.10]


async def test_ladder_cut_is_refused_when_the_size_changed_since_its_snapshot(tmp_path, monkeypatch):
    t, other, fake = two(tmp_path)
    tid = (await short(t))["trade_id"]
    t.j.update_trade(tid, plan={**t.j.get_trade(tid)["plan"], "ladder_live": ladder_apply.TAG, "full_size": 100})
    fake.price = 0.97

    async def ladder_then_guardian_acts(ex, j, pos, log=True):   # between the ladder's snapshot and its cut
        assert (await other.close(SYM, "plan/target 0.97 hit", fraction=0.5, expect_contracts=100))["ok"]
        return {"variants": {"base": {"action": "reduce", "x_virtual": 0.5, "score": -1.0},
                             "adaptive": {}}, "x_actual": 1, "x_star": 0.5, "levels": {}, "parts": {}, "score": -1.0}
    monkeypatch.setattr(ladder_apply, "build_ladder", ladder_then_guardian_acts)
    r = await ladder_apply.apply(t, SYM)
    assert not r["results"][0]["ok"] and "position is 50, you expected 100" in r["results"][0]["error"]
    assert fake.position == 50


async def test_concurrent_order_paths_in_one_process_never_deadlock(tmp_path):
    """Lock order: the account lock (risk-adding flows only, #74) -> one symbol lock. Every other path takes only its
    symbol lock, so an open, a fill, a guardian pass and session actions on the same symbols interleave without a
    deadlock."""
    t, _, fake = two(tmp_path)
    fake.add_symbol("ETH/USDT:USDT", 1.0)
    fake.add_symbol("SOL/USDT:USDT", 1.0)
    d, g = EntryDesk(t, regime_fn=favorable), guardian(t)
    await short(t)
    await d.propose_group([prop(1.04, symbol="ETH/USDT:USDT")])
    fake.fill_limit(next(iter(fake.limits)))
    fake.price = 0.97
    jobs = [d.poll(), g.check_positions(), t.move_stop(SYM, 1.03, "tighten"),
            PositionOps(t).add_to_winner(SYM, 20, 1.02, "add"), t.close(SYM, "trim", fraction=0.5, of_original=True),
            t.open_short("SOL/USDT:USDT", stop_price=1.05, setup="pump_fade", thesis="t", notional=50)]
    res = await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), 10)
    assert not [r for r in res if isinstance(r, BaseException)], res
