"""Query Algo Order by clientAlgoId can miss a conditional order that rests (#89). Demo probe 8, 2026-10-04 13:30Z: the
open conditional listing carried our client id while the lookup answered -2013 "Order does not exist."; 1 of 4 lookups
on demo missed. #55 called that not-found answer unverified, so every path that looks a conditional order up by its
client id falls back to the listing: Trader.find_stop (a first stop, a bridge, an ambiguous stop send) and the resolver.
These tests pin that fallback with the fault tests/fake_faults.py `2013_resting` (one lookup misses; the order rests)."""

import time

import pytest

from shortbot import audit, clientid, intents, resolver
from shortbot.intents import Intent
from shortbot.trading import STOP_PARAMS
from tests.fakes import SYM
from tests.test_fault_matrix import bridges, cids, close_stops, trader
from tests.test_intent_status import outcomes, submit
from tests.test_trading import open_default

LATER = resolver.GRACE_S + 1


def stop_sends(fake) -> list[str]:
    """The client id of every closePosition stop sent, re-sends included."""
    return [p["clientOrderId"] for *_, p in fake.orders if (p or {}).get("closePosition")]


async def test_find_stop_finds_in_the_listing_what_the_lookup_misses_and_settles_its_send_placed():
    t, fake = trader()
    assert (await open_default(t))["ok"]
    fake.stops.clear()                                          # the position's stop is gone: a new one may rest
    it = Intent("t_find", {}, SYM)
    intents.receive(t.j, it)
    intents.start(t.j, it.id)
    cid = clientid.new("stop", clientid.tag(SYM))
    fake.arm("fetch_order", "2013_resting", "stop")
    with audit.within(it.id):                                   # the intent running in this task
        intents.link(t.j, cid, SYM)
        await fake.create_order(SYM, "STOP_MARKET", "buy", None, None,   # it rests; its answer never came back
                                {**STOP_PARAMS, "triggerPrice": 1.05, "clientOrderId": cid})
        found = await t.find_stop(SYM, cid)
    await t.j.flush()
    assert found is not None and found["clientOrderId"] == cid and close_stops(fake) == [1.05]
    assert fake.fired == [("fetch_order", "stop", "2013_resting")]
    assert outcomes(t.j, intents.get(t.j, it.id)) == [("stop", "placed")] and intents.unsettled(t.j) == []


async def test_a_bridge_whose_answer_is_lost_and_whose_lookup_misses_is_found_in_the_listing(monkeypatch):
    t, fake = trader()
    assert (await open_default(t))["ok"]
    fake.price = 0.95
    fake.arm("create_order", "1007_ran", "bridge")              # the bridge rests, its answer is lost
    fake.arm("fetch_order", "2013_resting", "bridge")           # and Query Algo Order says it doesn't exist
    res, row = await submit(t, monkeypatch, lambda: t.move_stop(SYM, 1.02, "trail"))
    assert res["ok"] and row["status"] == "done", res
    assert fake.fired == [("create_order", "bridge", "1007_ran"), ("fetch_order", "bridge", "2013_resting")]
    assert len(cids(fake, "bridge")) == 1 and bridges(fake) == [] and fake.prearms == {}   # one bridge, gone again
    assert close_stops(fake) == [1.02] and fake.position == 100 and t.j.active_trade(SYM)["stop_price"] == 1.02
    assert outcomes(t.j, row) == [("bridge", "placed"), ("stop", "placed")] and intents.unsettled(t.j) == []


async def test_a_first_stop_whose_answer_is_lost_and_whose_lookup_misses_is_never_sent_twice(monkeypatch):
    """Binance answers -4130 to a second closePosition stop while the position is open (probe 1): the lost one is found
    in the listing, so the second send never goes out."""
    t, fake = trader()
    fake.arm("create_order", "1007_ran", "stop")
    fake.arm("fetch_order", "2013_resting", "stop")
    res, row = await submit(t, monkeypatch, lambda: open_default(t))
    assert res["ok"] and row["status"] == "done", res
    assert fake.fired == [("create_order", "stop", "1007_ran"), ("fetch_order", "stop", "2013_resting")]
    assert len(stop_sends(fake)) == 1 and close_stops(fake) == [1.05] and fake.position == 100
    assert ("stop", "placed") in outcomes(t.j, row) and intents.unsettled(t.j) == []
    assert "NAKED_POSITION" not in [a["kind"] for a in t.j.alerts(100)]


@pytest.mark.parametrize("kind", ["bridge", "prearm"])
async def test_the_resolver_settles_a_conditional_send_the_lookup_misses_from_the_listing(kind, monkeypatch):
    t, fake = trader()
    assert (await open_default(t))["ok"]
    cid = clientid.new(kind, clientid.tag(SYM))
    fake.arm("create_order", "1007_ran", kind)                  # it rests; nothing looked it up: the intent is unknown

    async def send():
        async with t.symbol_lock(SYM, f"a {kind} sent"):
            return await t.place_reduce_stop(SYM, 100.0, 1.04, cid)
    _, row = await submit(t, monkeypatch, send)
    assert row["status"] == "unknown" and outcomes(t.j, row) == [(kind, None)]
    sent = len(fake.orders)
    fake.arm("fetch_order", "2013_resting", kind)
    (ev,) = await resolver.resolve(t, now=time.time() + LATER)   # past the grace: unfound would be `not_found`
    assert ev["orders"] == {cid: "placed"} and ev["status"] == "done"
    assert fake.fired[-1] == ("fetch_order", kind, "2013_resting") and len(fake.orders) == sent   # nothing re-sent
    assert [fake.algo[i]["cid"] for i in fake.prearms] == [cid]   # it still rests
