"""A partly filled limit entry keeps its remainder resting (entries_partial.py, owner decision 2026-10-05; BEAMX #43
filled 11% at a touch and the rest was cancelled). The fake shapes partial fills as ccxt does: status "open" with
filled < amount, then "closed" / "canceled" / "expired" keeping what filled."""

import json

import pytest

from shortbot import exposure, reconcile
from shortbot.entry_gates import HALTED_WHY
from shortbot.intents import Intent
from tests.fakes import SYM
from tests.test_entries_cancel import fail_cancel, recorder
from tests.test_entry_races import SYM2, desk, prop


async def partial(frac=0.25, **kw):
    """A resting entry at 1.04 (stop 1.10) filled `frac` of its size, adopted by one poll."""
    d, t, fake = desk()
    fake.price = 1.035
    sent = recorder(t)
    eid = (await d.propose_group([prop(1.04, **kw)], ttl_hours=2))["placed"][0]["entry_id"]
    oid = next(iter(fake.limits))
    amount = fake.limits[oid]["amount"]
    fake.fill_limit(oid, qty=round(amount * frac, 1))
    ev = await d.poll()
    return d, t, fake, sent, eid, oid, amount, ev


def row(d, eid):
    return d._rows("id=?", (eid,))[0]


def fills_alerted(sent):
    return [m for k, m, s, u in sent if k == "action_fill"]


async def test_a_first_partial_fill_opens_the_trade_for_what_filled_and_the_rest_keeps_resting():
    d, t, fake, sent, eid, oid, amount, ev = await partial()
    (e,) = ev
    tr = t.j.get_trade(e["trade_id"])
    assert e["event"] == "filled" and tr["status"] == "open" and tr["amount"] == pytest.approx(fake.position)
    assert fake.limits[oid]["status"] == "open" and fake.limits[oid]["filled"] < amount     # still on the book
    r = row(d, eid)
    assert (r["status"], r["trade_id"], r["filled_qty"]) == ("pending", tr["id"], fake.limits[oid]["filled"])
    assert list(fake.stops.values()) == [1.10] and fake.prearms == {}     # the whole-position stop, the pre-arm gone
    (msg,) = fills_alerted(sent)
    assert "rests at 1.04" in msg and f"of {amount:g}" in msg
    assert "rests until expiry" in d.pending()[0]["hint"]
    left = amount - r["filled_qty"]                                        # exposure counts the remainder only
    assert exposure.pending_units(t.j, [], 1e9) == [(SYM, pytest.approx(left * 1.04))]
    assert not (await d.amend(eid, 1.05, "move it"))["ok"]                 # never moved
    assert (await t.open_short(SYM, 1.10, "pump_fade", "x"))["ok"] is False   # one trade per symbol


async def test_each_further_fill_grows_the_same_trade_under_the_whole_position_stop():
    d, t, fake, sent, eid, oid, amount, ev = await partial()
    tid = ev[0]["trade_id"]
    fake.fill_limit(oid, qty=round(amount * 0.25, 1))                     # a second increment
    (e,) = await d.poll()
    assert e["event"] == "increment" and e["trade_id"] == tid and len(t.j.trades()) == 1
    tr = t.j.get_trade(tid)
    assert tr["amount"] == pytest.approx(fake.position) == pytest.approx(row(d, eid)["filled_qty"])
    assert row(d, eid)["status"] == "pending" and fake.limits[oid]["status"] == "open"
    assert len([o for o in fake.orders if (o[3] or {}).get("closePosition")]) == 1   # one stop covers it all
    assert "filled" in fills_alerted(sent)[-1] and "more" in fills_alerted(sent)[-1]
    (dec,) = t.j._rows("SELECT snapshot FROM decisions WHERE action='entry_increment'")
    assert json.loads(dec["snapshot"])["trade_id"] == tid
    fake.fill_limit(oid)                                                   # the rest fills: the row is done
    (e,) = await d.poll()
    assert e["event"] == "increment" and row(d, eid)["status"] == "filled"
    assert "in full" in row(d, eid)["reason"] and t.j.get_trade(tid)["amount"] == pytest.approx(amount)
    assert len(fills_alerted(sent)) == 3 and d.pending() == []
    fake.trigger_stop()                                                    # the stop buys back the WHOLE position
    assert fake.position == 0


async def test_closing_the_trade_cancels_the_remainder_before_it_buys_back():
    d, t, fake, sent, eid, oid, amount, ev = await partial()
    fake.price = 1.0
    r = await t.close(SYM, "target", "lesson")
    assert r["ok"] and fake.position == 0 and fake.limits[oid]["status"] == "canceled"
    assert t.j.get_trade(ev[0]["trade_id"])["status"] == "closed"
    (e,) = await d.poll()
    assert e["event"] == "remainder_cancelled" and row(d, eid)["status"] == "filled"
    assert "trade closing" in row(d, eid)["reason"] and d.pending() == []


async def test_a_trade_closed_on_the_exchange_has_its_remainder_cancelled_by_the_next_poll():
    d, t, fake, sent, eid, oid, amount, ev = await partial()
    fake.price = 0.95
    await fake.create_order(SYM, "market", "buy", fake.position, None, {"reduceOnly": True})   # a target, by hand
    await reconcile.reconcile(t)
    assert t.j.get_trade(ev[0]["trade_id"])["status"] == "closed"
    (e,) = await d.poll()
    assert e["event"] == "remainder_cancelled" and fake.limits[oid]["status"] == "canceled"
    assert row(d, eid)["status"] == "filled" and f"trade #{ev[0]['trade_id']} closed" in row(d, eid)["reason"]


async def test_a_remainder_that_fills_after_its_trade_closed_is_a_new_protected_trade():
    d, t, fake, sent, eid, oid, amount, ev = await partial()
    fake.price = 0.95
    await fake.create_order(SYM, "market", "buy", fake.position, None, {"reduceOnly": True})
    await reconcile.reconcile(t)
    fake.fill_limit(oid)                                                   # before the poll could cancel it
    (e,) = await d.poll()
    assert e["event"] == "filled" and e["trade_id"] != ev[0]["trade_id"]
    assert t.j.get_trade(e["trade_id"])["status"] == "open" and list(fake.stops.values()) == [1.10]
    ((msg, urgent),) = [(m, u) for k, m, s, u in sent if k == "action_fill" and "fresh position" in m]
    assert urgent and row(d, eid)["status"] == "filled"


async def test_more_filled_into_a_position_already_closed_is_linked_to_its_trade_and_the_rest_goes():
    d, t, fake, sent, eid, oid, amount, ev = await partial()
    fake.fill_limit(oid, qty=round(amount * 0.25, 1))
    await fake.create_order(SYM, "market", "buy", fake.position, None, {"reduceOnly": True})   # closed by hand
    (e,) = await d.poll()
    assert e["event"] == "fill_linked" and e["trade_id"] == ev[0]["trade_id"]
    assert fake.limits[oid]["status"] == "canceled" and row(d, eid)["status"] == "filled"
    assert row(d, eid)["filled_qty"] == pytest.approx(fake.limits[oid]["filled"])
    assert [u for k, m, s, u in sent if k == "action_fill"][-1] is True
    await reconcile.reconcile(t)                                           # its fills are the trade's (own_entries)
    tr = t.j.get_trade(ev[0]["trade_id"])
    assert tr["status"] == "closed" and tr["pnl"] == pytest.approx(sum(float(i["income"]) for i in fake.income))


async def test_expiry_ends_the_remainder_and_settles_the_row():
    d, t, fake, sent, eid, oid, amount, ev = await partial()
    fake.advance(2 * 3600 + 60)                                            # its GTD lapses on the exchange
    (e,) = await d.poll()
    assert e["event"] == "remainder_ended" and "expired" in row(d, eid)["reason"]
    assert row(d, eid)["status"] == "filled" and t.j.get_trade(ev[0]["trade_id"])["status"] == "open"
    d2, t2, fake2, _, eid2, oid2, _, _ = await partial()
    await d2._update(eid2, expires_ms=0)                                   # the desk's TTL first: cancelled
    assert await d2.poll() == [] and fake2.limits[oid2]["status"] == "canceled"
    (e,) = await d2.poll()
    assert e["event"] == "remainder_ended" and row(d2, eid2)["status"] == "filled"


async def test_cancel_below_and_a_halt_withdraw_the_remainder_but_not_the_cascade_rule():
    d, t, fake, sent, eid, oid, amount, ev = await partial(cancel_below=0.98)
    await d._update(eid, ref_price=0.99)                                   # +5% in 15 min: a cascade for a resting
    assert await d.poll() == [] and fake.limits[oid]["status"] == "open"   # entry, not for a remainder at its level
    fake.price = 0.97
    await d.poll()
    assert fake.limits[oid]["status"] == "canceled"
    (e,) = await d.poll()
    assert e["event"] == "remainder_ended" and "without us" in row(d, eid)["reason"]
    d2, t2, fake2, _, eid2, oid2, _, _ = await partial()
    out = await d2.gw.submit(Intent("withdraw_entries", {"reason": HALTED_WHY, "ids": None}))   # the HALTED sweep
    assert out["left_for_fill_poll"] == [eid2] and fake2.limits[oid2]["status"] == "canceled"
    await d2.poll()
    assert row(d2, eid2)["status"] == "filled" and "HALTED" in row(d2, eid2)["reason"]


async def test_the_first_partial_fill_still_cancels_its_oco_siblings():
    d, t, fake = desk()
    fake.price = 1.035
    r = await d.propose_group([prop(1.04), prop(2.08, stop=2.2, symbol=SYM2)])
    oid = next(i for i, o in fake.limits.items() if o["symbol"] == SYM)
    fake.fill_limit(oid, qty=round(fake.limits[oid]["amount"] / 2, 1))
    await d.poll()
    sib = next(o for o in fake.limits.values() if o["symbol"] == SYM2)
    assert sib["status"] == "canceled" and fake.limits[oid]["status"] == "open"
    assert row(d, r["placed"][1]["entry_id"])["status"] == "cancelled"


async def test_a_remainder_that_must_go_and_wont_cancel_is_protected_and_journalled_once_settled():
    d, t, fake = desk()                                    # a sibling on another symbol filled first: over budget
    fake.price = 1.035
    await d.propose_group([prop(2.08, stop=2.2, symbol=SYM2), prop(1.04)])
    oid = next(i for i, o in fake.limits.items() if o["symbol"] == SYM)
    fake.fill_limit(oid, qty=round(fake.limits[oid]["amount"] / 2, 1))
    fake.set_price(SYM2, 2.09)                             # the sibling fills in full, polled first (lower id)
    fail_cancel(fake, oid, times=2)                        # its OCO cancel, then the fill's own pull, fail
    assert [x["event"] for x in await d.poll()] == ["filled", "fill_unsettled"]
    assert sorted(fake.stops.values()) == [1.10, 2.2] and fake.prearms == {}   # protected before anything else
    assert len([tr for tr in t.j.trades() if tr["symbol"] == SYM]) == 0   # still tracked, journalled once settled
    fake.fill_limit(oid, qty=round(fake.limits[oid]["amount"] / 4, 1))    # more fills before the retry
    events = [x["event"] for x in await d.poll()]
    (tr,) = [tr for tr in t.j.trades() if tr["symbol"] == SYM]
    assert "filled" in events and tr["amount"] == pytest.approx(fake.position)
    assert fake.limits[oid]["status"] == "canceled"


async def test_a_split_legs_remainder_and_its_other_leg_both_rest_and_join_one_trade_and_go_with_it():
    d, t, fake = desk()
    fake.price = 1.035
    await d.propose_group([prop(1.04, tranches=[{"limit_price": 1.04}, {"limit_price": 1.07}])])
    leg1 = next(i for i, o in fake.limits.items() if o["price"] == 1.04)
    leg2 = next(i for i, o in fake.limits.items() if o["price"] == 1.07)
    fake.fill_limit(leg1, qty=round(fake.limits[leg1]["amount"] / 2, 1))
    (first,) = await d.poll()
    assert fake.limits[leg1]["status"] == fake.limits[leg2]["status"] == "open"   # a split's other leg stays, as ever
    fake.fill_limit(leg2)
    (ev,) = await d.poll()
    assert ev["event"] == "tranche_joined" and ev["trade_id"] == first["trade_id"] and len(t.j.trades()) == 1
    fake.price = 1.0
    assert (await t.close(SYM, "target", "lesson"))["ok"] and fake.limits[leg1]["status"] == "canceled"
    await d.poll()
    assert d.pending() == [] and {r["status"] for r in d._rows("1=1")} == {"filled"}


async def test_no_new_entry_on_a_symbol_while_a_remainder_still_rests_there():
    d, t, fake, sent, eid, oid, amount, ev = await partial()
    await fake.create_order(SYM, "market", "buy", fake.position, None, {"reduceOnly": True})   # flat, not settled yet
    r = await d.propose_group([prop(1.06, stop=1.12)])
    assert not r["ok"] and "remainder still rests" in r["rejected"][0]["error"]


def test_the_book_counts_a_remainder_as_heat_once_and_its_trade_as_the_bet():
    import sqlite3

    from shortbot.book import pending_book
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, group_id TEXT, symbol TEXT, status TEXT, setup TEXT,"
               " amount REAL, limit_price REAL, stop_price REAL, tranche TEXT, trade_id INTEGER, filled_qty REAL)")
    db.execute("INSERT INTO entries (group_id, symbol, status, setup, amount, limit_price, stop_price, trade_id,"
               " filled_qty) VALUES ('g', 'A/USDT:USDT', 'pending', 'pump_fade', 100, 1.0, 1.1, 7, 25)")
    risks, book = pending_book(db, SYM)
    assert risks == {"A/USDT:USDT": pytest.approx(75 * 0.1)} and book == []
