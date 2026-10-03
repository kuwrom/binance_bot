"""Cancelling limit entries never hides a fill: an order that filled before its cancel landed keeps its stop and is
adopted by the poll; a failed cancel is retried, never relabelled (task #7). Moving `fake.price` fills and triggers
on its own; the two wrappers below cover what no price move can: a fill landing INSIDE one poll, between its
fetch_order and its cancel, and a cancel failing for one chosen order only."""

import ccxt
import pytest

from tests.fakes import SYM
from tests.test_entries import desk, prop

SYM2 = "MOVR/USDT:USDT"


def oid_at(fake, price):
    return next(i for i, o in fake.limits.items() if o["price"] == price)


def alerted(t, text):
    return any(text in a["message"] for a in t.j.alerts())


def recorder(t):
    """Capture (kind, message, symbol, urgent) as the guardian's notifier gets them (the journal drops `urgent`)."""
    sent = []

    async def notify(kind, msg, symbol, urgent):
        sent.append((kind, msg, symbol, urgent))
    t.set_alert(notify)
    return sent


def fill_during_cancel(fake, oid, qty=None):
    """The limit fills between the poll's fetch_order and its cancel (price racing to the level)."""
    real = fake.cancel_order

    async def cancel(i, s, params=None):
        if i == oid and fake.limits[oid]["status"] == "open":
            fake.fill_limit(oid, qty)
        return await real(i, s, params=params)
    fake.cancel_order = cancel


def fail_cancel(fake, oid, times):
    """Cancelling this order times out `times` times; the order stays live."""
    real, left = fake.cancel_order, {"n": times}

    async def cancel(i, s, params=None):
        if i == oid and left["n"]:
            left["n"] -= 1
            raise ccxt.RequestTimeout("binanceusdm DELETE https://demo-fapi.binance.com/fapi/v1/order timed out")
        return await real(i, s, params=params)
    fake.cancel_order = cancel


async def test_cancelling_a_group_never_hides_an_order_that_already_filled():
    d, t, fake = desk()
    fake.price = 1.035
    r = await d.propose_group([prop(1.04)])
    eid = r["placed"][0]["entry_id"]
    fake.price = 1.05                                              # filled; the guardian hasn't polled yet
    res = await d.cancel_group(r["group_id"], "analyst: thesis gone")
    assert res["cancelled"] == 0 and res["filled_first"] == [eid]
    assert [x["id"] for x in d.pending()] == [eid] and len(fake.prearms) == 1   # row and stop both kept
    ev = await d.poll()                                            # the guardian adopts and protects it
    assert [e["event"] for e in ev] == ["filled"] and list(fake.stops.values()) == [1.10]
    assert t.j.get_trade(ev[0]["trade_id"])["status"] == "open"


async def test_an_agent_cancel_that_fails_reports_it_and_leaves_the_order_live():
    d, t, fake = desk()
    fake.price = 1.035
    r = await d.propose_group([prop(1.04)])
    eid = r["placed"][0]["entry_id"]
    fake.drift_before_cancel = 1                                   # -1021: refused before it ran
    res = await d.cancel_group(r["group_id"], "analyst: thesis gone")
    assert res == {"ok": False, "cancelled": 0, "filled_first": [], "cancel_failed": [eid]}
    assert [x["id"] for x in d.pending()] == [eid] and len(fake.prearms) == 1
    assert (await d.cancel_group(r["group_id"], "analyst: thesis gone"))["cancelled"] == 1   # calling again works
    assert d.pending() == [] and fake.prearms == {}


@pytest.mark.parametrize("path", ["cascade", "cancel_below", "ttl", "partial"])
async def test_a_fill_racing_any_cancel_rule_keeps_its_stop_and_is_adopted(path):
    d, t, fake = desk()
    limit, stop = (1.08, 1.15) if path == "cascade" else (1.04, 1.10)
    r = await d.propose_group([prop(limit, stop=stop, cancel_below=0.95)])
    eid, oid = r["placed"][0]["entry_id"], next(iter(fake.limits))
    fake.price = {"cascade": 1.045, "cancel_below": 0.94}.get(path, 1.0)   # ATR(1h) = 4%: 1.045 is a cascade
    if path in ("ttl", "partial"):
        d._update(eid, expires_ms=0)                               # local TTL passed, GTD expiry not applied yet
    fill_during_cancel(fake, oid, fake.limits[oid]["amount"] / 2 if path == "partial" else None)
    ev = await d.poll()
    assert ev == [] and [x["id"] for x in d.pending()] == [eid]    # never labelled cancelled/expired
    assert len(fake.prearms) == 1 and fake.position > 0
    ev = await d.poll()
    assert [e["event"] for e in ev] == ["filled"] and list(fake.stops.values()) == [stop]
    assert abs(t.j.get_trade(ev[0]["trade_id"])["amount"] - fake.position) < 1e-9


async def test_a_sibling_whose_cancel_fails_stays_live_and_is_retried_until_it_dies():
    d, t, fake = desk()
    fake.price = 1.035
    r = await d.propose_group([prop(1.04), prop(1.06, stop=1.12)])
    first, sib = (x["entry_id"] for x in r["placed"])
    fail_cancel(fake, oid_at(fake, 1.06), times=2)               # on the fill, and on the same-pass retry
    fake.price = 1.05
    ev = await d.poll()
    assert [e["event"] for e in ev] == ["filled"] and alerted(t, f"#{sib} cancel failed")
    assert [x["id"] for x in d.pending()] == [sib] and fake.limits[oid_at(fake, 1.06)]["status"] == "open"
    assert len(fake.prearms) == 1                                  # the sibling's stop is untouched
    ev = await d.poll()
    assert ev == [{"entry_id": sib, "event": "cancelled", "why": f"sibling #{first} filled"}]
    assert fake.limits[oid_at(fake, 1.06)]["status"] == "canceled" and fake.prearms == {}


async def test_a_stuck_sibling_cancel_alerts_once_per_streak_of_three_failures():
    d, t, fake = desk()
    fake.price = 1.035
    sent = recorder(t)
    r = await d.propose_group([prop(1.04), prop(1.06, stop=1.12)])
    first, sib = (x["entry_id"] for x in r["placed"])
    fail_cancel(fake, oid_at(fake, 1.06), times=5)               # the fill's cancel_group, then 4 retries
    fake.price = 1.05
    stuck = [(f"entry #{sib}: cancel failing (sibling #{first} filled), order still live", SYM, True)]
    for n in range(4):                                             # retries 1-4, one per pass; the 3rd alerts
        await d.poll()
        assert [(m, s, u) for k, m, s, u in sent if k == "entry_cancel_failing"] == (stuck if n >= 2 else [])
    ev = await d.poll()
    assert ev == [{"entry_id": sib, "event": "cancelled", "why": f"sibling #{first} filled"}] and d._cancel_fails == {}


async def test_siblings_on_two_symbols_that_both_fill_are_two_protected_trades_flagged_over_budget():
    d, t, fake = desk()
    fake.add_symbol(SYM2, 2.0, tick=0.001)
    sent = recorder(t)
    await d.propose_group([prop(1.04), prop(2.08, stop=2.2, symbol=SYM2)])
    fake.price = 1.05
    fake.set_price(SYM2, 2.09)                                     # both fill inside one poll interval
    ev = await d.poll()
    assert [e["event"] for e in ev] == ["filled", "filled"] and d.pending() == []
    first, second = (t.j.get_trade(e["trade_id"]) for e in ev)
    assert (first["symbol"], second["symbol"]) == (SYM, SYM2) and first["status"] == second["status"] == "open"
    assert sorted(fake.stops.values()) == [1.10, 2.2] and fake.prearms == {}     # each its own plan stop
    fills = [(m, s, u) for k, m, s, u in sent if k == "action_fill"]
    assert "#2 filled before its cancel (adopted next poll)" in fills[0][0] and not fills[0][2]
    assert fills[1][1] == SYM2 and fills[1][2] and fills[1][0].startswith(
        "sibling #1 also filled: group over its single-bet budget")


async def test_same_symbol_siblings_that_both_fill_are_one_trade_flagged_over_budget():
    d, t, fake = desk()
    fake.price = 1.035
    await d.propose_group([prop(1.04), prop(1.06, stop=1.12)])
    fake.price = 1.07                                              # a spike through both levels between two polls
    ev = await d.poll()
    assert [e["event"] for e in ev] == ["filled", "sibling_joined"]
    (tr,) = t.j.open_trades()
    assert abs(tr["amount"] - fake.position) < 1e-9 and list(fake.stops.values()) == [1.10] and fake.prearms == {}
    assert {x["status"] for x in d._rows("1=1")} == {"filled"} and {x["trade_id"] for x in d._rows("1=1")} == {tr["id"]}
    assert alerted(t, "sibling #1 also filled: group over its single-bet budget")


async def test_split_legs_that_fill_within_one_poll_are_counted_once():
    d, t, fake = desk()
    fake.price = 1.035
    await d.propose_group([prop(1.04, tranches=[{"limit_price": 1.04}, {"limit_price": 1.07}])])
    fake.price = 1.08                                              # both legs, not the shared 1.10 stop
    ev = await d.poll()
    assert [e["event"] for e in ev] == ["filled", "tranche_joined"]
    (tr,) = t.j.open_trades()
    assert abs(tr["amount"] - fake.position) < 1e-9                # not leg 1 + leg 2 + leg 2 again


async def test_a_leg_that_fills_while_its_trade_closes_is_kept_and_adopted_as_an_urgent_new_trade():
    d, t, fake = desk()
    fake.price = 1.035
    await d.propose_group([prop(1.04, tranches=[{"limit_price": 1.04}, {"limit_price": 1.07}])])
    fake.price = 1.05
    old = (await d.poll())[0]["trade_id"]
    far = oid_at(fake, 1.07)
    fill_during_cancel(fake, far, fake.limits[far]["amount"] / 2)  # leg 2 half-fills while close() cancels it
    fake.price = 1.0
    await t.close(SYM, "target", "lesson")
    leg2 = d._rows("leg=2")[0]
    assert leg2["status"] == "pending" and fake.position > 0       # not 'cancelled': the poll adopts it
    sent = recorder(t)
    ev = await d.poll()
    assert [e["event"] for e in ev] == ["filled"] and list(fake.stops.values()) == [1.10]
    new = ev[0]["trade_id"]
    assert new != old and t.j.get_trade(old)["status"] == "closed" and t.j.get_trade(new)["status"] == "open"
    ((kind, msg, symbol, urgent),) = sent
    assert kind == "action_fill" and urgent and msg.startswith(f"leg filled after its trade closed: new trade #{new}.")
