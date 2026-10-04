"""Cancels and edits an intent sends (#86, intent_changes.py). Binance gives neither an id of its own: Cancel Order and
Modify Order take the order's orderId or origClientOrderId, Cancel Algo Order its algoId or clientAlgoId. So each one
is recorded against the order it targets, before it is sent, with what Binance answered; one whose answer was lost is
settled later from that order (Query Order / Query Algo Order: reads only, never sent again). The requests are as
before: by the order's id."""

import sqlite3
import time
from datetime import UTC, datetime

import pytest

from shortbot import audit, clientid, intent_changes, intents, resolver
from shortbot.intents import Intent
from tests import fake_errors as err
from tests.fake_faults import FaultyCcxt
from tests.fakes import SYM, FakeEx, neutral_budget, ok_prechecks
from tests.test_entries import prop
from tests.test_fault_matrix_entries import desk, row
from tests.test_intent_status import submit
from tests.test_trading import open_default, trader


def changes(j, it) -> list[tuple]:
    return [(c["op"], c["order_id"], c["outcome"]) for c in intent_changes.of_intent(j, it["id"])]


# ------------------------------------------------------------ what each answer records
async def test_a_cancel_is_recorded_against_the_order_it_targets_with_what_binance_answered(monkeypatch):
    t, fake = trader()
    assert (await open_default(t))["ok"]
    (sid,) = fake.stops

    async def flow():
        async with t.symbol_lock(SYM, "test"):
            await t.cancel(SYM, sid, trigger=True)                   # cancelled
            for _ in range(2):
                try:
                    await t.cancel(SYM, sid, trigger=True)           # -2011: off the book already
                except Exception:  # noqa: BLE001 - the row says what it was
                    pass
                fake.cancel_order = refuse                           # then a definite refusal
        return {"ok": True}

    async def refuse(i, s, params=None):
        raise err.margin_insufficient()
    res, it = await submit(t, monkeypatch, flow)
    assert changes(t.j, it) == [("cancel", sid, "cancelled"), ("cancel", sid, "off_book"), ("cancel", sid, "rejected")]
    c = intent_changes.of_intent(t.j, it["id"])[0]
    assert (c["symbol"], c["trigger"], c["want"], c["intent_id"]) == (SYM, 1, None, it["id"])


async def test_a_cancel_whose_answer_was_lost_stays_in_doubt_and_is_sent_once(monkeypatch):
    fake = FaultyCcxt()
    t = trader_on(fake)
    assert (await open_default(t))["ok"]
    (sid,) = fake.stops
    fake.arm("cancel_order", "1007_ran", "stop")
    led = {}

    async def flow():
        try:
            async with t.symbol_lock(SYM, "test"):
                await t.cancel(SYM, sid, trigger=True)
        finally:
            led["changes"] = dict(intents.ledger().changes)
        return {"ok": True}
    _, it = await submit(t, monkeypatch, flow)
    assert list(led["changes"].values()) == [None] and changes(t.j, it) == [("cancel", sid, None)]
    assert fake.stops == {} and fake.fired == [("cancel_order", "stop", "1007_ran")]   # it ran, once


async def test_an_edit_is_recorded_with_the_price_it_asked_for(monkeypatch):
    d, t, fake = desk()
    (p,) = (await d.propose_group([prop(1.04)]))["placed"]
    e = row(d, p["entry_id"])
    res, it = await submit(t, monkeypatch, lambda: d.amend(e["id"], 1.05, "closer to the level"))
    assert res["ok"] and changes(t.j, it) == [("edit", e["order_id"], "edited")]
    assert intent_changes.of_intent(t.j, it["id"])[0]["want"] == 1.05


async def test_an_entry_cancel_carries_the_status_and_reason_it_was_sent_for(monkeypatch):
    d, t, fake = desk()
    r = await d.propose_group([prop(1.04)])
    e = row(d, r["placed"][0]["entry_id"])
    res, it = await submit(t, monkeypatch, lambda: d.cancel_group(r["group_id"], "level gone"))
    assert res["ok"] and changes(t.j, it) == [("cancel", e["order_id"], "cancelled"),
                                              ("cancel", e["prearm_id"], "cancelled")]
    limit, prearm = intent_changes.of_intent(t.j, it["id"])
    assert limit["note"] == {"status": "cancelled", "reason": "level gone"} and prearm["note"] is None
    assert intent_changes.latest(t.j, SYM, e["order_id"])["intent_id"] == it["id"]


def test_change_rows_are_append_only_and_their_outcome_is_set_once(tmp_path):
    from shortbot.journal import Journal
    j = Journal(tmp_path / "j.db")
    it = Intent("t_act", {"symbol": SYM}, SYM)
    intents.receive(j, it)
    intents.start(j, it.id)
    intent_changes.link(j, "x-7-1", it.id, "cancel", SYM, "7", False, None, None, intents.now_ms())
    assert intent_changes.record(j, "x-7-1", "cancelled") and not intent_changes.record(j, "x-7-1", "missed")
    for sql in ("UPDATE intent_changes SET outcome='missed'", "UPDATE intent_changes SET order_id='8'",
                "DELETE FROM intent_changes"):
        with pytest.raises(sqlite3.DatabaseError, match="change"):
            j.db.execute(sql)


# ------------------------------------------------------------ settling one from the order it targets
T0 = 1_800_000_000.0


def iso(s: float) -> str:
    return datetime.fromtimestamp(s, UTC).isoformat(timespec="milliseconds")


def ch(op="cancel", trigger=0, want=None) -> dict:
    return {"op": op, "trigger": trigger, "want": want, "at": iso(T0)}


def found(status, upd=T0 + 1, price=1.04, filled=0.0) -> dict:
    return {"status": status, "lastUpdateTimestamp": None if upd is None else int(upd * 1000), "price": price,
            "filled": filled}


@pytest.mark.parametrize("c, o, later, now, outcome", [
    (ch(), found("canceled"), None, T0 + 5, "cancelled"),                   # it left the book after our send
    (ch(), found("canceled", upd=T0 - 60), None, T0 + 5, "off_book"),       # it was gone before we sent it
    (ch(), found("canceled", upd=T0 + 60), T0 + 30, T0 + 90, "missed"),     # a later cancel of ours took it
    (ch(), found("canceled", upd=T0 + 10), T0 + 30, T0 + 90, "cancelled"),  # ours took it, before the later one
    (ch(), found("canceled", upd=T0 + 31), T0 + 30, T0 + 90, "missed"),     # it left as the later one was sent
    (ch(), found("canceled", upd=T0 + 1), T0 + 1, T0 + 90, "off_book"),     # both sent within the skew: can't tell
    (ch(), found("canceled", upd=None), None, T0 + 5, "off_book"),
    (ch(), found("expired"), None, T0 + 5, "off_book"),                     # its GTD, not our cancel
    (ch(), None, None, T0 + 5, "off_book"),                                 # unknown to Binance: not resting
    (ch(), found("open"), None, T0 + 5, None),                              # may still land: wait
    (ch(), found("open"), None, T0 + 91, "missed"),                         # still resting past the grace
    (ch(), found("closed", filled=10.0), None, T0 + 5, "filled"),           # it traded first
    (ch(trigger=1), found("closed"), None, T0 + 5, "triggered"),            # the stop fired first
    (ch("edit", want=1.05), found("open", price=1.05), None, T0 + 5, "edited"),
    (ch("edit", want=1.05), found("closed", price=1.05), None, T0 + 5, "edited"),
    (ch("edit", want=1.05), found("open"), None, T0 + 5, None),
    (ch("edit", want=1.05), found("open"), None, T0 + 91, "missed"),
    (ch("edit", want=1.05), found("closed"), None, T0 + 5, "filled"),
    (ch("edit", want=1.05), found("canceled"), None, T0 + 5, "cancelled"),  # Modify Order cancels in two cases
    (ch("edit", want=1.05), None, None, T0 + 5, "off_book"),
])
def test_what_a_change_did_is_read_from_the_order_it_targets(c, o, later, now, outcome):
    assert intent_changes.looked_up(c, o, later, now) == outcome


async def test_the_resolver_records_what_a_lost_cancel_did_and_never_sends_it_again():
    t, fake = trader()
    assert (await open_default(t))["ok"]
    (sid,) = fake.stops
    it = Intent("close_position", {"symbol": SYM, "reason": "x"}, SYM)
    intents.receive(t.j, it)
    intents.start(t.j, it.id)
    real = fake.cancel_order

    async def lost(i, s, params=None):
        await real(i, s, params)
        raise err.status_unknown()
    fake.cancel_order = lost
    cid = clientid.new("close", clientid.tag(SYM))
    with audit.within(it.id), intents.recording():
        async with t.symbol_lock(SYM, "test"):
            with pytest.raises(Exception, match="-1007"):
                await t.cancel(SYM, sid, trigger=True)
            intents.link(t.j, cid, SYM)
    await fake.create_order(SYM, "market", "buy", 100, None, {"reduceOnly": True, "clientOrderId": cid})
    await t.j.flush()   # its process died here
    intents.sweep(t.j, alive=lambda pid: False)
    sent, calls = len(fake.orders), []

    async def counted(i, s, params=None):
        calls.append(i)
        return await real(i, s, params)
    fake.cancel_order = counted
    (ev,) = await resolver.resolve(t, now=time.time() + resolver.GRACE_S + 1)
    assert changes(t.j, {"id": it.id}) == [("cancel", sid, "cancelled")] and ev["orders"] == {cid: "filled"}
    assert len(fake.orders) == sent and calls == []                     # read, never sent again


def trader_on(fake):
    from shortbot.journal import Journal
    from shortbot.trading import Trader
    return Trader(FakeEx(fake), Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
