"""Task #14: one make-before-break stop swap (stops.set_stop) for move_stop, widen_stop, the guardian's restore and
plan moves. Every instant has a stop on the exchange; only this position's closePosition stop and our own bridges are
ever cancelled; an interrupted swap still ends with a stop."""

import asyncio

import ccxt
import pytest

from shortbot.entries import EntryDesk
from shortbot.journal import Journal
from shortbot.positioning import PositionOps
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks


def trader(**kw):
    fake = FakeCcxt(**kw)
    t = Trader(FakeEx(fake), Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
    return t, fake


async def opened(**kw):
    t, fake = trader(**kw)
    r = await t.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100)
    assert r["ok"], r
    return t, fake


def kinds(t):
    return [a["kind"] for a in t.j.alerts(100)]


def decisions(t, action):
    return [d for d in t.j.decisions(SYM, 100) if d["action"] == action]


def plant(fake, i, trigger, close, cid=None, qty=None):
    """A conditional order already resting on SYM (left by an earlier position or a dead swap)."""
    (fake.stops if close else fake.prearms)[i] = trigger
    fake.algo[i] = {"symbol": SYM, "qty": qty, "trigger": trigger, "cid": cid, "status": "NEW", "close": close,
                    "ts": fake.now_ms()}


def wrap_create(fake, hook):
    real = fake.create_order

    async def co(s, type_, side, amount, price=None, params=None):
        return await hook(real, s, type_, side, amount, price, params or {})
    fake.create_order = co


# ---- bug 1a (ONDO #5 shape): the cancel executed, then answered -1021: the swap still ends with the new stop
async def test_widen_stop_cancel_drift_still_ends_protected():
    t, fake = await opened()
    fake.drift_on_cancel = 1
    r = await PositionOps(t).widen_stop(SYM, 1.10, "noise")
    assert r["ok"], r
    assert list(fake.stops.values()) == [1.10] and fake.prearms == {} and t.ex.resyncs >= 1
    assert fake.position == pytest.approx(49.9) and decisions(t, "widen_stop")


# ---- bug 1b: new stop and restore both refused: the bridge (at the tighter, old level) stays and a human is told
async def test_widen_stop_place_and_restore_fail_keeps_bridge_and_alerts():
    t, fake = await opened()
    fake.fail_stop = 2
    r = await PositionOps(t).widen_stop(SYM, 1.10, "noise")
    assert not r["ok"] and fake.stops == {} and list(fake.prearms.values()) == [1.05]
    assert fake.position == 100                     # stop first: no size cut while the stop isn't moved
    assert "NAKED_POSITION" in kinds(t) and not (await t.positions())[0]["UNPROTECTED"]
    assert "widened" not in (t.j.active_trade(SYM)["notes"] or "") and decisions(t, "widen_stop_failed")


async def test_widen_stop_moves_the_stop_before_cutting_size():
    t, fake = await opened()
    r = await PositionOps(t).widen_stop(SYM, 1.10, "noise")
    assert r["ok"] and r["size_cut"] == pytest.approx(50.1)
    sent = [(o[0], o[1], o[3].get("closePosition"), o[3].get("triggerPrice")) for o in fake.orders]
    placed = sent.index(("STOP_MARKET", "buy", True, 1.10))
    cut = max(i for i, o in enumerate(sent) if o[0] == "market" and o[1] == "buy")
    assert placed < cut and list(fake.stops.values()) == [1.10] and fake.prearms == {}
    d = decisions(t, "widen_stop")
    assert len(d) == 1 and t.j.active_trade(SYM)["stop_price"] == 1.10


async def test_widen_stop_cut_refused_moves_the_stop_back():
    t, fake = await opened()
    fake.fail_close = 3                              # the size cut never goes through
    r = await PositionOps(t).widen_stop(SYM, 1.10, "noise")
    assert not r["ok"] and fake.position == 100 and list(fake.stops.values()) == [1.05] and fake.prearms == {}
    assert t.j.active_trade(SYM)["stop_price"] == 1.05 and "widened" not in (t.j.active_trade(SYM)["notes"] or "")


# ---- bug 2: a stop move touches only this position's closePosition stop, never a resting leg's pre-arm
async def test_move_stop_keeps_resting_legs_prearm():
    t, fake = trader()
    d = EntryDesk(t, regime_fn=_fav)
    g = await d.propose_group([{"symbol": SYM, "limit_price": None, "stop_price": 1.10, "setup": "pump_fade",
                                "thesis": "t", "level_type": "prior_high", "level_reason": "r", "leverage": 3,
                                "tranches": [{"limit_price": 1.03, "share": 0.5}, {"limit_price": 1.06, "share": 0.5}]}])
    assert g["ok"], g
    near = next(i for i, o in fake.limits.items() if o["price"] == 1.03)
    fake.fill_limit(near)
    fake.price = 1.0
    assert (await d.poll())[0]["event"] == "filled"
    far_prearm = dict(fake.prearms)
    assert len(far_prearm) == 1
    fake.price = 0.98
    m = await t.move_stop(SYM, 1.035, "breakeven")
    assert m["ok"], m
    assert fake.prearms == far_prearm and list(fake.stops.values()) == [1.035]


async def _fav():
    return "favorable"


# ---- bug 3: a split leg's fill never moves a widened stop back to the leg's original stop
async def test_leg_fill_keeps_a_widened_stop():
    t, fake = trader()
    d = EntryDesk(t, regime_fn=_fav)
    g = await d.propose_group([{"symbol": SYM, "limit_price": None, "stop_price": 1.10, "setup": "pump_fade",
                                "thesis": "t", "level_type": "prior_high", "level_reason": "r", "leverage": 3,
                                "tranches": [{"limit_price": 1.03, "share": 0.5}, {"limit_price": 1.06, "share": 0.5}]}])
    assert g["ok"], g
    near = next(i for i, o in fake.limits.items() if o["price"] == 1.03)
    far = next(i for i, o in fake.limits.items() if o["price"] == 1.06)
    fake.fill_limit(near)
    fake.price = 1.0
    await d.poll()
    w = await PositionOps(t).widen_stop(SYM, 1.15, "sweep level")
    assert w["ok"], w
    fake.fill_limit(far)
    await d.poll()
    assert list(fake.stops.values()) == [1.15] and t.j.active_trade(SYM)["stop_price"] == 1.15


# ---- -4130: decided by the listing, with the code or without it
async def test_cancel_refused_by_drift_is_retried_and_the_move_completes():
    t, fake = await opened()
    fake.drift_before_cancel = 1                    # the first cancel never ran: the place meets -4130
    r = await t.move_stop(SYM, 1.02, "trail")
    assert r["ok"] and list(fake.stops.values()) == [1.02] and fake.prearms == {}


async def test_one_close_position_refusal_without_its_code():
    t, fake = await opened()
    fake.drift_before_cancel = 1

    async def hook(real, s, type_, side, amount, price, params):
        if params.get("closePosition") and fake.stops:   # a code Binance no longer documents: answered differently
            raise ccxt.InvalidOrder('binanceusdm {"code":-4999,"msg":"closePosition order exists."}')
        return await real(s, type_, side, amount, price, params)
    wrap_create(fake, hook)
    r = await t.move_stop(SYM, 1.02, "trail")
    assert r["ok"], r
    assert list(fake.stops.values()) == [1.02] and fake.prearms == {}


async def test_second_close_position_stop_accepted_is_cleaned_up():
    t, fake = await opened()
    fake.drift_before_cancel = 1                    # old stays; Binance (no -4130 any more) takes a second one

    async def hook(real, s, type_, side, amount, price, params):
        if not params.get("closePosition"):
            return await real(s, type_, side, amount, price, params)
        held = dict(fake.stops)
        fake.stops.clear()
        try:
            return await real(s, type_, side, amount, price, params)
        finally:
            fake.stops.update(held)
    wrap_create(fake, hook)
    r = await t.move_stop(SYM, 1.02, "trail")
    assert r["ok"] and list(fake.stops.values()) == [1.02] and fake.prearms == {}


# ---- an interrupted swap (guardian job timeout, shutdown) still ends with a stop, and no bridge
async def test_move_stop_timed_out_mid_swap_ends_with_the_old_stop():
    t, fake = await opened()
    blocked = []

    async def hook(real, s, type_, side, amount, price, params):
        if params.get("closePosition") and not blocked:   # the new stop's send hangs, after the old was cancelled
            blocked.append(1)
            await asyncio.sleep(30)
        return await real(s, type_, side, amount, price, params)
    wrap_create(fake, hook)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(t.move_stop(SYM, 1.02, "trail"), 0.2)
    assert blocked and list(fake.stops.values()) == [1.05] and fake.prearms == {}
    assert t.j.active_trade(SYM)["stop_price"] == 1.05


async def test_move_stop_cancelled_while_sending_the_bridge_keeps_the_old_stop():
    t, fake = await opened()

    async def hook(real, s, type_, side, amount, price, params):
        if params.get("reduceOnly") and params.get("triggerPrice"):
            o = await real(s, type_, side, amount, price, params)   # it lands, the answer never comes back
            await asyncio.sleep(30)
            return o
        return await real(s, type_, side, amount, price, params)
    wrap_create(fake, hook)
    task = asyncio.create_task(t.move_stop(SYM, 1.02, "trail"))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(fake.stops.values()) == [1.05] and fake.prearms == {}


# ---- sign flip: a swap that ends with a LONG position alarms at once
async def test_flip_after_a_swap_alarms():
    t, fake = await opened()
    real, n = fake.fetch_positions, []

    async def fp():
        n.append(1)
        rows = await real()
        return [{**r, "side": "long"} for r in rows] if len(n) > 1 else rows
    fake.fetch_positions = fp
    r = await t.move_stop(SYM, 1.02, "trail")
    assert "position_flip" in kinds(t) and r.get("warnings")


# ---- stale closePosition stop on a flat symbol: the next entry swaps it out instead of closing the new short
async def test_open_short_replaces_a_stale_close_position_stop():
    t, fake = trader()
    plant(fake, "900", 1.20, close=True)
    r = await t.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100)
    assert r["ok"], r
    assert fake.position == 100 and list(fake.stops.values()) == [1.05] and fake.prearms == {}


# ---- leftover bridges (a swap killed mid-way): swept once a closePosition stop rests, never before
async def test_guardian_restores_a_bridge_only_position_and_sweeps_the_bridge():
    from tests.test_guardian import guardian_with, open_trade
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    fake.stops.clear()
    plant(fake, "901", 1.04, close=False, cid="sb-bPUMP-deadbeef", qty=100)
    await g.check_positions()
    assert list(fake.stops.values()) == [1.05] and fake.prearms == {}


async def test_guardian_restores_a_position_covered_only_by_a_prearm():
    from tests.test_guardian import guardian_with, open_trade
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    fake.stops.clear()
    plant(fake, "902", 1.10, close=False, cid="sb-p7-deadbeef", qty=50)   # another entry's pre-arm: not ours to cancel
    await g.check_positions()
    assert list(fake.stops.values()) == [1.05] and list(fake.prearms.values()) == [1.10]


async def test_full_close_sweeps_a_leftover_bridge():
    t, fake = await opened()
    plant(fake, "903", 1.06, close=False, cid="sb-bPUMP-deadbeef", qty=100)
    res = await t.close(SYM, "exit")
    assert res["ok"] and fake.position == 0 and fake.prearms == {} and fake.stops == {}


async def test_guardian_sweeps_a_leftover_bridge_next_to_the_stop():
    from tests.test_guardian import guardian_with, open_trade
    from tests.test_guardian import kinds as g_kinds
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    plant(fake, "904", 1.04, close=False, cid="sb-bPUMP-deadbeef", qty=100)   # it would cut before the 1.05 stop
    await g.check_positions()
    assert list(fake.stops.values()) == [1.05] and fake.prearms == {} and "bridge_swept" in g_kinds(g)


async def test_bridge_that_will_not_cancel_is_reported():
    t, fake = await opened()
    real = fake.cancel_order

    async def cancel(i, s, params=None):
        if fake.algo.get(i, {}).get("cid", "") and fake.algo[i]["cid"].startswith("sb-b"):
            raise ccxt.NetworkError("timeout")
        return await real(i, s, params)
    fake.cancel_order = cancel
    r = await t.move_stop(SYM, 1.02, "trail")
    assert r["ok"] and list(fake.stops.values()) == [1.02] and "bridge_left" in kinds(t)
