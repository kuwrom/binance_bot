"""A full close cancels only this trade's own stops, and only once the symbol is flat (task #56): another group's
resting entry keeps its pre-armed stop, a leg kept by `withdraw` keeps its own, and a remainder (a close that
filled short, or an order that filled mid-close) keeps its stop or gets one at once, never waiting for the guardian."""

import ccxt
import pytest

from tests.fakes import SYM
from tests.test_entries import desk, prop
from tests.test_entries_cancel import fail_cancel, fill_during_cancel, oid_at, recorder

TRIG = {"trigger": True}


async def market_short(t, stop=1.05):
    r = await t.open_short(SYM, stop_price=stop, setup="pump_fade", thesis="lower high after +60%", notional=100)
    assert r["ok"], r
    return r["trade_id"]


def close_fills_short(fake, part, times=1):
    """The reduce-only market close fills only `part` and the rest expires: Binance FAQ "Summary of Failed Orders in
    Binance Futures" (unfilled market orders expire past the Market Order Price Cap/Floor Ratio). ccxt sends
    newOrderRespType=RESULT for swaps, so the answer carries status 'expired' and filled < amount."""
    real, left = fake.create_order, {"n": times}

    async def create(s, type_, side, amount, price=None, params=None):
        if left["n"] and type_ == "market" and side == "buy" and (params or {}).get("reduceOnly"):
            left["n"] -= 1
            o = await real(s, type_, side, amount * part, price, params)
            return {**o, "amount": amount, "remaining": amount - o["filled"], "status": "expired"}
        return await real(s, type_, side, amount, price, params)
    fake.create_order = create


def fill_after_flat_read(fake, qty, stop):
    """Right after close() reads the symbol flat, another entry fills and the guardian (its own process, its own
    lock) protects it with a closePosition stop: that stop is not this trade's."""
    real, armed = fake.fetch_positions, {"on": True}

    async def fetch():
        rows = await real()
        if armed["on"] and not fake.position:
            armed["on"] = False
            await fake.create_order(SYM, "market", "sell", qty)
            await fake.create_order(SYM, "STOP_MARKET", "buy", None, None,
                                    {"closePosition": True, "triggerPrice": stop, "workingType": "MARK_PRICE"})
        return rows
    fake.fetch_positions = fetch


async def test_closing_a_trade_leaves_another_groups_resting_split_entry_and_its_stops_alone():
    d, t, fake = desk()
    r = await d.propose_group([prop(1.06, stop=1.12, tranches=[{"limit_price": 1.06}, {"limit_price": 1.08}])])
    other, prearms = {x["entry_id"] for x in r["placed"]}, dict(fake.prearms)
    await market_short(t)                                          # open_short doesn't see resting entries
    fake.price = 0.95
    res = await t.close(SYM, "target", "lesson")
    assert res["ok"] and fake.position == 0 and fake.stops == {}
    assert {x["id"] for x in d.pending()} == other and fake.prearms == prearms
    assert all(o["status"] == "open" for o in fake.limits.values())


async def test_a_close_that_fills_short_keeps_its_stop_and_the_trade_open_until_closed_again():
    d, t, fake = desk()
    tid = await market_short(t)
    sent = recorder(t)
    close_fills_short(fake, 0.5)
    fake.price = 0.95
    res = await t.close(SYM, "target", "lesson")
    assert not res["ok"] and res["remaining"] == fake.position == 50 and list(fake.stops.values()) == [1.05]
    assert t.j.get_trade(tid)["status"] == "open"
    ((msg, urgent),) = [(m, u) for k, m, s, u in sent if k == "close_incomplete"]
    assert urgent and "filled 50" in msg and "trade stays open" in msg
    res = await t.close(SYM, "target", "lesson")                   # the rest: flat, stop gone, trade finalized
    assert res["ok"] and fake.position == 0 and fake.stops == {}
    tr = t.j.get_trade(tid)
    assert tr["status"] == "closed" and tr["gross_pnl"] == pytest.approx(5.0) and tr["lesson"] == "lesson"


async def leg_fills_mid_close(prearm=True, fail_stop=0):
    """A split entry's leg 1 is a trade; leg 2 half-fills while close() withdraws it, after the market close (the
    fake's closePosition stop leaves with the position the moment it reads flat)."""
    d, t, fake = desk()
    fake.price = 1.035
    await d.propose_group([prop(1.04, tranches=[{"limit_price": 1.04}, {"limit_price": 1.07}])], prearm_stop=prearm)
    fake.price = 1.05
    old = (await d.poll())[0]["trade_id"]
    far = oid_at(fake, 1.07)
    fill_during_cancel(fake, far, fake.limits[far]["amount"] / 2)
    sent = recorder(t)
    fake.price = 1.0
    fake.fail_stop = fail_stop
    res = await t.close(SYM, "target", "lesson")
    return t, fake, old, res, sent


async def test_a_leg_that_fills_mid_close_keeps_the_prearm_that_covers_it():
    t, fake, old, res, sent = await leg_fills_mid_close()
    assert res["ok"] and res["remaining"] == pytest.approx(fake.position) and fake.position > 0
    assert list(fake.prearms.values()) == [1.10] and fake.stops == {}   # the poll adds the whole-position stop
    assert t.j.get_trade(old)["status"] == "closed"                # our size was bought back: the poll adopts the rest
    ((msg, urgent),) = [(m, u) for k, m, s, u in sent if k == "close_incomplete"]
    assert urgent and "filled during the close" in msg and "pre-armed stop(s) at [1.1] cover it" in msg


async def test_a_remainder_with_no_stop_left_gets_a_close_position_stop_at_once():
    t, fake, old, res, sent = await leg_fills_mid_close(prearm=False)
    assert res["ok"] and fake.position > 0 and list(fake.stops.values()) == [1.10] and fake.prearms == {}
    ((msg, urgent),) = [(m, u) for k, m, s, u in sent if k == "close_incomplete"]
    assert urgent and "closePosition stop placed at 1.1" in msg


async def test_a_remainder_whose_stop_cannot_be_placed_alerts_naked_position():
    t, fake, old, res, sent = await leg_fills_mid_close(prearm=False, fail_stop=1)
    assert fake.position > 0 and fake.stops == {} and fake.prearms == {}
    ((msg, urgent),) = [(m, u) for k, m, s, u in sent if k == "NAKED_POSITION"]
    assert urgent and "stop rejected" in msg and "Pre-armed stops: none" in msg


async def test_flatten_keeps_the_stop_of_a_limit_that_fills_mid_close_and_raises_no_false_naked_alert():
    from shortbot import killswitch_ops as ops
    d, t, fake = desk()
    await d.propose_group([prop(1.06, stop=1.12)])
    lim = oid_at(fake, 1.06)
    await market_short(t)
    fail_cancel(fake, lim, times=1)                                # HALTED's withdraw: outcome unknown, still rests
    real = fake.fetch_open_orders

    async def listing(s, params=None):
        if fake.limits[lim]["status"] == "open" and not fake.position:
            fake.fill_limit(lim)                                   # it fills mid-flatten, above the old 1.05 stop
        return await real(s, params=params)
    fake.fetch_open_orders = listing
    sent = recorder(t)
    r = await ops.flatten(t, "agent (MCP)", "BTC squeeze")
    assert not r["ok"] and r["still_open"] == [SYM] and list(fake.prearms.values()) == [1.12]
    assert [k for k, m, s, u in sent if k in ("close_incomplete", "NAKED_POSITION")] == ["close_incomplete"]


async def test_this_trades_leftover_prearm_and_resting_leg_go_when_it_closes_flat():
    d, t, fake = desk()
    fake.price = 1.035
    await d.propose_group([prop(1.04, tranches=[{"limit_price": 1.04}, {"limit_price": 1.07}])])
    fail_cancel(fake, d._rows("leg=1")[0]["prearm_id"], times=1)   # the fill's pre-arm cancel times out
    fake.price = 1.05
    tid = (await d.poll())[0]["trade_id"]
    assert len(fake.prearms) == 2
    fake.price = 1.0
    res = await t.close(SYM, "target", "lesson")
    assert res["ok"] and fake.position == 0 and fake.stops == {} and fake.prearms == {}
    assert d.pending() == [] and d._rows("leg=2")[0]["status"] == "cancelled"
    assert t.j.get_trade(tid)["status"] == "closed"


async def test_a_stop_placed_for_a_new_fill_after_the_flat_read_is_not_cancelled():
    d, t, fake = desk()
    await market_short(t)
    fake.price = 0.95
    fill_after_flat_read(fake, 50, 1.2)
    res = await t.close(SYM, "target", "lesson")
    assert res["ok"] and fake.position == 50 and list(fake.stops.values()) == [1.2]


async def test_a_reread_that_fails_cancels_nothing_and_alerts():
    d, t, fake = desk()
    r = await d.propose_group([prop(1.06, stop=1.12)])
    tid = await market_short(t)
    sent = recorder(t)
    real = fake.fetch_positions

    async def flaky():
        if not fake.position:
            raise ccxt.RequestTimeout("binanceusdm GET https://demo-fapi.binance.com/fapi/v3/positionRisk timed out")
        return await real()
    fake.fetch_positions = flaky
    fake.price = 0.95
    res = await t.close(SYM, "target", "lesson")
    assert res["ok"] and res["remaining"] is None and t.j.get_trade(tid)["status"] == "closed"   # our size: bought back
    assert len(fake.prearms) == 1 and [x["id"] for x in d.pending()] == [r["placed"][0]["entry_id"]]
    ((msg, urgent),) = [(m, u) for k, m, s, u in sent if k == "close_incomplete"]
    assert urgent and "could not re-read" in msg and "every stop kept" in msg


async def test_a_fraction_that_rounds_to_the_whole_size_is_a_full_close():
    d, t, fake = desk()
    tid = await market_short(t)
    fake.amount_to_precision = lambda s, a: "100"   # ccxt truncates; a step that rounds up must not strand the trade
    res = await t.close(SYM, "time stop", "lesson", fraction=0.9999)
    assert res["ok"] and res["result"]["trade_id"] == tid and fake.position == 0
    tr = t.j.get_trade(tid)
    assert tr["status"] == "closed" and tr["exit_reason"] == "time stop" and tr["lesson"] == "lesson"


async def test_a_partial_close_touches_no_stop():
    d, t, fake = desk()
    r = await d.propose_group([prop(1.06, stop=1.12)])
    await market_short(t)
    before = (dict(fake.stops), dict(fake.prearms))
    res = await t.close(SYM, "first target", fraction=0.5)
    assert res["ok"] and fake.position == 50 and (fake.stops, fake.prearms) == before
    assert [x["id"] for x in d.pending()] == [r["placed"][0]["entry_id"]]


async def test_a_prearm_whose_send_answer_was_lost_is_matched_by_client_id_when_its_trade_closes():
    from tests.test_entry_intents import on_send
    d, t, fake = desk()
    fake.price = 1.035
    on_send(fake, "STOP_MARKET", 1, arm="ack_lost")               # it rests; the row has prearm_cid, no prearm_id
    eid = (await d.propose_group([prop(1.04)]))["placed"][0]["entry_id"]
    assert d._rows("id=?", (eid,))[0]["prearm_id"] is None and len(fake.prearms) == 1
    fake.price = 1.05
    (ev,) = await d.poll()                                         # the fill can't cancel a pre-arm it has no id for
    assert ev["event"] == "filled" and len(fake.prearms) == 1
    fake.price = 1.0
    res = await t.close(SYM, "target", "lesson")
    assert res["ok"] and fake.position == 0 and fake.prearms == {} and fake.stops == {}
