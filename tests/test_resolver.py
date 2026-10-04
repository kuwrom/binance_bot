"""The gateway's resolver (#36 step 3, GATEWAY.md §5): an intent left unknown (or interrupted) is settled from the
exchange by its orders' client ids, one outcome per order, and finalised done, refused or partial. It never re-sends."""

import time

import pytest

from shortbot import audit, clientid, gateway, intents, resolver
from shortbot.intents import Intent
from tests.fakes import SYM, FakeCcxt
from tests.test_add_rules import ROOMY, desk
from tests.test_guardian import guardian_with, open_trade
from tests.test_intent_status import blind_lookups, outcomes, submit, unsent_sell
from tests.test_trading import open_default, trader

LATER = resolver.GRACE_S + 1


def kinds(j):
    return [a["kind"] for a in j.alerts(100)]


async def test_an_unknown_close_that_ran_resolves_done_and_nothing_is_sent_again(monkeypatch):
    t, fake = trader()
    assert (await open_default(t))["ok"]
    fake.ack_lost = 1
    blind = blind_lookups(fake)
    res, row = await submit(t, monkeypatch, lambda: t.close(SYM, "half", fraction=0.5))
    sent = len(fake.orders)
    assert row["status"] == "unknown" and await resolver.resolve(t) == []   # Query Order still times out
    blind["on"] = False
    (ev,) = await resolver.resolve(t)
    cid = intents.orders(t.j, row["id"])[0]["client_id"]
    assert ev == {"intent_id": row["id"], "kind": "t_flow", "was": "unknown", "status": "done",
                  "orders": {cid: "filled"}}
    now = intents.get(t.j, row["id"])
    assert now["status"] == "done" and now["resolved_at"] and now["result"] == res   # the caller's answer is kept
    assert outcomes(t.j, now) == [("close", "filled")] and len(fake.orders) == sent and fake.position == 50
    assert "intent_resolved" in kinds(t.j) and t.j.decisions(SYM)[0]["action"] == "intent_resolved"
    assert await resolver.resolve(t) == []                                   # once
    again = gateway.answer(t.j, now)                                         # what a resubmitted id gets now
    assert (again["status"], again["resolved"]["orders"]) == ("done", {cid: "filled"}) and "unknown" not in again
    assert again["resolved"]["steps"] == [{"step": "close", "ref": cid, "outcome": "filled"}]


async def test_an_unknown_close_that_never_ran_waits_out_the_grace_then_resolves_refused(monkeypatch):
    t, fake = trader()
    assert (await open_default(t))["ok"]
    fake.ack_lost_unsent = 1                     # -1007, and it did not run
    res, row = await submit(t, monkeypatch, lambda: t.close(SYM, "half", fraction=0.5))
    assert res["unknown"] and row["status"] == "unknown"
    assert await resolver.resolve(t) == []                                   # not found yet: it may still land
    (ev,) = await resolver.resolve(t, now=time.time() + LATER)
    assert ev["status"] == "refused" and list(ev["orders"].values()) == ["not_found"] and fake.position == 100


async def test_an_add_never_placed_after_its_stop_tighten_resolves_partial(monkeypatch):
    ops, t, fake = await desk(**ROOMY)
    fake.price = 0.9
    unsent_sell(fake)
    res, row = await submit(t, monkeypatch, lambda: ops.add_to_winner(SYM, 44, 0.95, "press"))
    sent = len(fake.orders)
    (ev,) = await resolver.resolve(t, now=time.time() + LATER)
    assert ev["status"] == "partial" and outcomes(t.j, intents.get(t.j, row["id"]))[-1] == ("add", "not_found")
    assert len(fake.orders) == sent and fake.position == 100 and list(fake.stops.values()) == [0.95]


async def test_a_whole_close_whose_tries_all_timed_out_resolves_refused_and_is_never_resent(monkeypatch):
    t, fake = trader()
    assert (await open_default(t))["ok"]
    fake.fail_close = 3
    _, row = await submit(t, monkeypatch, lambda: t.close(SYM, "out"))
    sent = len(fake.orders)
    (ev,) = await resolver.resolve(t, now=time.time() + LATER)
    assert ev["status"] == "refused" and set(ev["orders"].values()) == {"not_found"} and len(ev["orders"]) == 3
    assert len(fake.orders) == sent and fake.position == 100


def o(kind: str, outcome: str | None, n: int = 0) -> dict:
    return {"client_id": f"sb-{clientid.KINDS[kind]}{n}-0", "kind": kind, "outcome": outcome}


def x(op: str, outcome: str | None, n: int = 0) -> dict:
    """A cancel or an edit of order n (intent_changes.py)."""
    return {"ref": f"{op[0]}-{n}-0", "op": op, "order_id": str(n), "outcome": outcome}


RAISED, OK, FAILED, GROUP = None, {"ok": True}, {"ok": False, "error": "stop not moved: its bridge was not placed"}, \
    {"ok": False, "cancelled": 0, "filled_first": [], "cancel_failed": [3]}


@pytest.mark.parametrize("was, body, orders, changes, doubted, status", [
    # raised, or a body that says what is in doubt: by the doubted units
    ("unknown", RAISED, [o("open", "filled")], [], {0}, "done"),
    ("unknown", RAISED, [o("open", "part_filled")], [], {0}, "partial"),
    ("unknown", RAISED, [o("open", "not_found")], [], {0}, "refused"),
    ("unknown", RAISED, [o("open", "unfilled")], [], {0}, "refused"),
    ("unknown", RAISED, [o("stop", "placed"), o("add", "not_found")], [], {1}, "partial"),   # the tighten ran
    ("unknown", RAISED, [o("stop", "placed"), o("add", "filled")], [], {1}, "done"),
    ("unknown", RAISED, [o("close", "not_found", 1), o("close", "filled", 2), o("close", "not_found", 3)], [],
     {0, 1, 2}, "done"),                                                                     # a close's tries: one
    ("unknown", RAISED, [o("close", "not_found", 1), o("close", "filled", 2)], [], {0}, "done"),   # incl. a settled
    ("unknown", RAISED, [o("close", "not_found", 1), o("close", "rejected", 2)], [], {0}, "refused"),
    ("unknown", {"ok": True, "unknown": [{"entry_id": 2}]}, [o("entry", "placed", 1), o("entry", "not_found", 2)], [],
     {1}, "partial"),                                                                        # a leg never landed
    ("unknown", RAISED, [o("entry", "placed", 1), o("entry", "placed", 2)], [], {1}, "done"),
    ("unknown", GROUP, [], [x("cancel", "cancelled")], {0}, "done"),                         # its cancel ran
    ("unknown", GROUP, [], [x("cancel", "missed")], {0}, "refused"),
    ("unknown", GROUP, [], [x("cancel", "filled")], {0}, "refused"),                         # it traded first
    ("unknown", "unknown", [], [x("cancel", "off_book")], {0}, "done"),                      # cancel_entry's answer
    # a body definite about its goal: its own status; the doubted acts were side steps
    ("unknown", OK, [o("close", "not_found", 1), o("close", "filled", 2)], [], {0}, "done"),
    ("unknown", {"ok": True, "done": []}, [o("close", "filled"), o("target", "not_found")], [], {1}, "done"),
    ("unknown", OK, [o("open", "part_filled"), o("stop", "placed")], [x("cancel", "cancelled")], {2},
     "partial"),
    ("unknown", OK, [o("stop", "placed")], [x("cancel", "missed")], {1}, "done"),            # a bridge_left
    ("unknown", FAILED, [o("bridge", "not_found")], [], {0}, "refused"),                     # nothing ran
    ("unknown", FAILED, [o("bridge", "placed")], [], {0}, "partial"),                        # it did, after all
    # interrupted: it stopped before its end, so never done
    ("interrupted", RAISED, [o("bridge", "placed")], [], {0}, "partial"),
    ("interrupted", RAISED, [o("close", "filled")], [], {0}, "partial"),
    ("interrupted", RAISED, [o("open", "not_found")], [], {0}, "refused"),
    ("interrupted", RAISED, [], [x("cancel", "cancelled")], {0}, "partial"),
    ("interrupted", RAISED, [], [x("cancel", "missed")], {0}, "refused"),
])
def test_the_verdict(was, body, orders, changes, doubted, status):
    acts = resolver.acts(orders, changes)
    assert resolver.verdict(was, body, acts, {acts[i]["ref"] for i in doubted}) == status


async def test_an_interrupted_intent_is_settled_like_an_unknown_one():
    t, fake = trader()
    assert (await open_default(t))["ok"]
    it = Intent("close_position", {"symbol": SYM, "reason": "x"}, SYM)
    intents.receive(t.j, it)
    intents.start(t.j, it.id)
    cid = clientid.new("close", clientid.tag(SYM))
    with audit.within(it.id):
        intents.link(t.j, cid, SYM)
    await fake.create_order(SYM, "market", "buy", 100, None, {"reduceOnly": True, "clientOrderId": cid})
    await t.j.flush()   # its process died here, before its answer was journalled
    (swept,) = intents.sweep(t.j, alive=lambda pid: False)
    assert swept["status"] == "interrupted"
    (ev,) = await resolver.resolve(t)
    assert (ev["was"], ev["status"], ev["orders"]) == ("interrupted", "partial", {cid: "filled"})   # never done
    assert intents.get(t.j, it.id)["status"] == "partial"


async def test_the_guardians_reconcile_job_resolves_a_sessions_unknown_intent(monkeypatch):
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    fake.ack_lost_unsent = 1
    _, row = await submit(g.app.trader, monkeypatch, lambda: g.app.trader.close(SYM, "half", fraction=0.5))
    monkeypatch.setattr(resolver, "GRACE_S", 0.0)
    with audit.acting("guardian:entry_reconcile"):
        await g.reconcile_entries()
    assert intents.get(g.j, row["id"])["status"] == "refused"
    (a,) = [a for a in g.j.alerts(100) if a["kind"] == "intent_resolved"]
    assert "unknown -> refused" in a["message"] and "not_found" in a["message"]
