"""cancel-external (entries_cancel.cancel_external): the one path to an order the desk did not place. It cancels a
resting regular order by its id, with a reason, and believes only what filled; the desk's own orders, its targets and
conditional orders (stops) are never touched through it."""

import dataclasses

from shortbot import entry_intents
from shortbot import killswitch as ks
from shortbot.killswitch import ACTIVE, REDUCING
from tests.fakes import SYM
from tests.test_entries import prop
from tests.test_entries_cancel import fail_cancel
from tests.test_entry_intents import alerts, desk, open_limits, row

WHY = "forgotten limit from last week: the owner asked to pull it"


async def hand_limit(fake, price=1.2, **params):
    return await fake.create_order(SYM, "limit", "sell", 50, price, {"timeInForce": "GTC", **params})


async def test_a_swept_external_order_is_cancelled_with_its_reason_and_told():
    d, t, fake = desk()
    o = await hand_limit(fake)
    await entry_intents.reconcile(d)
    (x,) = d._rows("status='external'")
    r = await d.cancel_external(SYM, o["id"], WHY)
    assert r == {"ok": True, "symbol": SYM, "order_id": o["id"], "status": "external_gone", "outcome": "cancelled"}
    assert open_limits(fake) == [] and fake.limits[o["id"]]["status"] == "canceled"
    assert row(d, x["id"])["status"] == "external_gone" and WHY in row(d, x["id"])["reason"]
    assert t.j.decisions()[0]["action"] == "cancel_external" and len(alerts(t, "external_cancelled")) == 1
    assert d.pending(all_orders=True) == []                        # no longer counted as resting exposure
    again = await d.cancel_external(SYM, o["id"], WHY)
    assert not again["ok"] and "off the book already" in again["error"]


async def test_an_order_the_sweep_has_not_seen_yet_is_journalled_then_cancelled():
    d, t, fake = desk()
    o = await hand_limit(fake)
    r = await d.cancel_external(SYM, o["id"], WHY)
    assert r["ok"] and r["status"] == "external_gone" and open_limits(fake) == []
    (x,) = d._rows("group_id='external'")
    assert x["order_id"] == o["id"] and x["status"] == "external_gone" and x["amount"] == 50


async def test_an_order_that_filled_first_explains_its_position_and_a_lost_cancel_changes_nothing():
    d, t, fake = desk()
    o = await hand_limit(fake)
    await entry_intents.reconcile(d)
    (x,) = d._rows("status='external'")
    fail_cancel(fake, o["id"], 1)
    r = await d.cancel_external(SYM, o["id"], WHY)
    assert not r["ok"] and "Call again" in r["error"]
    assert row(d, x["id"])["status"] == "external" and open_limits(fake) == [o["id"]]
    assert [dec["action"] for dec in t.j.decisions()] == ["rejected"]   # the refusal is on the record, no cancel
    fake.fill_limit(o["id"])                                       # it fills before the second try lands
    r = await d.cancel_external(SYM, o["id"], WHY)
    assert r["ok"] and r["outcome"] == "filled_first" and row(d, x["id"])["status"] == "external_filled"
    assert entry_intents.explained_symbols(d) == {SYM}             # the position it made is explained, as today
    assert "adopt it or close it" in alerts(t, "external_cancelled")[0]["message"]


async def test_the_desks_own_orders_its_targets_and_stops_are_never_cancelled_through_it():
    d, t, fake = desk()
    placed = (await d.propose_group([prop(1.05)]))["placed"][0]
    mine = row(d, placed["entry_id"])
    r = await d.cancel_external(SYM, mine["order_id"], WHY)
    assert not r["ok"] and "desk's own entry" in r["error"] and open_limits(fake) == [mine["order_id"]]
    for cid in ("sb-t7-abcd1234", "sb-e1f2e3d4c-00000001"):         # a target's and an entry's id without a row
        o = await hand_limit(fake, price=1.3, clientOrderId=cid)
        r = await d.cancel_external(SYM, o["id"], WHY)
        assert not r["ok"] and "shortbot client id" in r["error"] and fake.limits[o["id"]]["status"] == "open"
    stop_id = next(iter(fake.prearms))                             # a conditional order: the entry's pre-armed stop
    r = await d.cancel_external(SYM, stop_id, WHY)
    assert not r["ok"] and "no regular order" in r["error"] and stop_id in fake.prearms


async def test_no_reason_no_cancel_and_an_order_already_off_the_book_is_not_journalled():
    d, t, fake = desk()
    o = await hand_limit(fake)
    r = await d.cancel_external(SYM, o["id"], " ")
    assert not r["ok"] and "a reason" in r["error"] and open_limits(fake) == [o["id"]]
    await fake.cancel_order(o["id"], SYM)                         # the owner pulled it; the sweep never saw it
    r = await d.cancel_external(SYM, o["id"], WHY)
    assert not r["ok"] and "not resting (canceled)" in r["error"] and d._rows("group_id='external'") == []


async def test_a_part_fill_is_its_own_outcome_and_a_swept_one_explains_its_position_as_today():
    d, t, fake = desk()
    o = await hand_limit(fake)
    await entry_intents.reconcile(d)                               # the sweep recorded it resting
    (x,) = d._rows("status='external'")
    fake.fill_limit(o["id"], qty=20)
    r = await d.cancel_external(SYM, o["id"], WHY)
    assert r["ok"] and r["outcome"] == "partly_filled" and r["status"] == "external_filled", r
    assert open_limits(fake) == [] and "had filled 20 of 50; the rest is cancelled" in row(d, x["id"])["reason"]
    assert "had filled 20 of 50" in alerts(t, "external_cancelled")[0]["message"]
    assert ks.read(t.j)["state"] == ACTIVE and not alerts(t, "untracked_position")   # explained by a swept order


async def test_fills_of_an_order_the_sweep_never_recorded_trip_as_an_untracked_position_would():
    d, t, fake = desk()
    o = await hand_limit(fake)
    fake.fill_limit(o["id"], qty=20)                               # partly filled before any sweep saw it
    r = await d.cancel_external(SYM, o["id"], WHY)
    st = ks.read(t.j)
    assert r["ok"] and r["outcome"] == "partly_filled", r
    assert st["state"] == REDUCING and st["cause"] == "reconciliation" and st["by"] == "cancel_external"
    assert len(alerts(t, "untracked_position")) == 1 and "20" in alerts(t, "untracked_position")[0]["message"]


async def test_cancel_external_is_refused_on_the_live_desk_for_now():
    d, t, fake = desk()
    o = await hand_limit(fake)
    t.ex.settings = dataclasses.replace(t.ex.settings, mode="live")
    r = await d.cancel_external(SYM, o["id"], WHY)
    assert not r["ok"] and "demo desk only" in r["error"] and open_limits(fake) == [o["id"]]
