"""The orphan sweep (orphans.py, #37): a pre-armed stop or bridge of ours whose cancel failed is cancelled on the next
pass once nothing owns it and nothing needs it as protection; a live entry's pre-arm, a bridge that is a position's only
stop, and every order without our prefix stay."""

from shortbot import entry_intents, orphans
from tests import fake_errors as err
from tests.fakes import SYM
from tests.test_entries import desk, prop

TRIG = {"trigger": True}


def kinds(t):
    return [a["kind"] for a in t.j.alerts(200)]


async def placed(d, limit=1.04):
    return (await d.propose_group([prop(limit)]))["placed"][0]["entry_id"]


def failing_trigger_cancels(fake, n=1):
    real, left = fake.cancel_order, [n]

    async def flaky(i, s, params=None):
        if params and left[0]:
            left[0] -= 1
            raise err.status_unknown()
        return await real(i, s, params)
    fake.cancel_order = flaky


async def sweep(d):
    d._orphans_at = 0.0
    return await orphans.sweep(d)


async def test_a_prearm_left_by_a_failed_cancel_goes_on_the_next_pass():
    d, t, fake = desk(price=1.0)
    eid = await placed(d)
    failing_trigger_cancels(fake)
    assert await d._cancel_entry(d._rows("id=?", (eid,))[0], "cancelled", "test") == "unfilled"
    assert len(fake.prearms) == 1 and "prearm_left" in kinds(t)
    d._orphans_at = 0.0
    ev = await entry_intents.reconcile(d)   # the guardian's reconcile job runs the sweep
    assert fake.prearms == {} and [e["event"] for e in ev] == ["orphan_cancelled"]
    assert kinds(t).count("orphan_stop_cancelled") == 1 and f"its entry #{eid} is cancelled" in ev[0]["why"]
    assert await sweep(d) == [] and kinds(t).count("orphan_stop_cancelled") == 1   # once


async def test_a_live_entrys_prearm_stays():
    d, t, fake = desk(price=1.0)
    await placed(d)
    assert await sweep(d) == [] and len(fake.prearms) == 1 and "orphan_stop_cancelled" not in kinds(t)


async def test_only_our_prefixes_are_ever_touched():
    d, t, fake = desk(price=1.0)
    await fake.create_order(SYM, "STOP_MARKET", "buy", 10, None,   # a human's reduce-only stop: no client id of ours
                            {"triggerPrice": 1.2, "reduceOnly": True, "clientOrderId": "web_abc"})
    await fake.create_order(SYM, "STOP_MARKET", "buy", 10, None,
                            {"triggerPrice": 1.3, "reduceOnly": True, "clientOrderId": "sb-p999-deadbeef"})
    await sweep(d)
    assert [a["cid"] for a in (fake.algo[i] for i in fake.prearms)] == ["web_abc"]   # sb-p999: no entry row owns it


async def test_a_bridge_left_on_a_flat_symbol_goes_one_that_is_the_only_stop_stays():
    d, t, fake = desk(price=1.0)
    await fake.create_order(SYM, "market", "sell", 100)
    params = {"triggerPrice": 1.05, "reduceOnly": True, "clientOrderId": "sb-bPUMP-0badc0de"}
    bridge = (await fake.create_order(SYM, "STOP_MARKET", "buy", 100, None, params))["id"]
    assert await sweep(d) == [] and bridge in fake.prearms   # short, no closePosition stop: maybe its only stop
    assert kinds(t).count("orphan_stop_kept") == 1
    await sweep(d)
    assert kinds(t).count("orphan_stop_kept") == 1   # told once
    await fake.create_order(SYM, "STOP_MARKET", "buy", None, None, {"triggerPrice": 1.08, "closePosition": True})
    assert [e["order_id"] for e in await sweep(d)] == [bridge] and bridge not in fake.prearms   # covered now
    assert fake.position == 100 and len(fake.stops) == 1


async def test_a_filled_entrys_leftover_prearm_goes_once_the_whole_position_stop_rests():
    d, t, fake = desk(price=1.0)
    eid = await placed(d)
    failing_trigger_cancels(fake)   # _protect's pre-arm cancel fails after the closePosition stop is placed
    fake.fill_limit(d._rows("id=?", (eid,))[0]["order_id"])
    await d.poll()
    assert d._rows("id=?", (eid,))[0]["status"] == "filled" and len(fake.prearms) == 1 and len(fake.stops) == 1
    assert "prearm_left" in kinds(t)
    assert [e["event"] for e in await sweep(d)] == ["orphan_cancelled"] and fake.prearms == {}
    assert len(fake.stops) == 1 and fake.position > 0   # the position's own stop is never touched
