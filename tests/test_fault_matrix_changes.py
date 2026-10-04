"""#86 fault matrix, part 3 (parts 1 and 2 and the cell invariants: test_fault_matrix*.py): the changes Binance gives
no id of their own, each run as the desk runs it (an intent) and meeting every answer of unknown outcome
(tests/fake_faults.py): an entry's amend (Modify Order) and a plan target's cancel inside a partial close's pull and a
flat symbol's pull. Each cell asserts, besides part 1's invariants:
- the change is recorded against its order and never sent again blind under that intent (a later pass re-lists the
  book and may cancel again: a cancel is idempotent, -2011 once gone)
- the intent stays unknown until the resolver reads the order, then ends by what the change did
- the journal matches the book (the entry's limit, the target rows), and the resting targets never exceed the position

| op                       | 1007_ran | 1007_unsent | 503_unknown | 503_unavailable | 1008 | 1021_after |
| amend (edit an entry)    | new      | new         | new         | new             | new  | new        |
| target cancel, partial   | new      | new         | new         | new             | new  | new        |
| target cancel, flat pull | new      | new         | new         | new             | new  | new        |
-1008 stays in: a cancel or an edit is not a reduce-only order (test_fault_matrix.EXEMPT)."""

import time

import pytest

from shortbot import audit, intent_changes, intents, resolver, targets
from tests.fake_faults import FAULTS, RAN, FaultyCcxt
from tests.fakes import SYM
from tests.test_entries import prop
from tests.test_fault_matrix import cids, close_stops, resolutions, settle
from tests.test_fault_matrix_entries import desk, row
from tests.test_intent_status import submit
from tests.test_target_orders import PLAN, guardian, placed_by_the_guardian, resting

LATER = resolver.GRACE_S + 1


def changes(j, it) -> list[tuple]:
    return [(c["op"], c["outcome"]) for c in intent_changes.of_intent(j, it["id"])]


# ------------------------------------------------------------ an entry's amend
@pytest.mark.parametrize("fault", FAULTS)
async def test_amend(fault, monkeypatch):
    d, t, fake = desk()
    (p,) = (await d.propose_group([prop(1.04)]))["placed"]
    e = row(d, p["entry_id"])
    fake.arm("edit_order", fault, "entry")
    res, it = await submit(t, monkeypatch, lambda: d.amend(e["id"], 1.05, "closer to the level"))
    ran = fault in RAN
    assert isinstance(res, Exception) and it["status"] == "unknown" and changes(t.j, it) == [("edit", None)]
    assert fake.limits[e["order_id"]]["price"] == (1.05 if ran else 1.04)
    await d.poll()                                         # the poll reads the order: the book's price is journalled
    now = row(d, e["id"])
    assert (now["status"], now["limit_price"], now["replaced"]) == ("pending", 1.05 if ran else 1.04, int(ran))
    await settle(t, monkeypatch)
    assert changes(t.j, it) == [("edit", "edited" if ran else "missed")]
    assert intents.get(t.j, it["id"])["status"] == ("done" if ran else "refused")
    assert resolutions(t.j) == [f"unknown -> {'done' if ran else 'refused'}"]
    again = await d.amend(e["id"], 1.06, "chase")          # the amend-once rule holds on what the book shows
    assert bool(again["ok"]) is not ran and fake.limits[e["order_id"]]["price"] == (1.05 if ran else 1.06)
    assert fake.fired == [("edit_order", "entry", fault)]


# ------------------------------------------------------------ a target's cancel in a pull
async def targets_resting(tmp_path, monkeypatch):
    placed_by_the_guardian(monkeypatch)
    fake = FaultyCcxt()
    g = guardian(fake, tmp_path / "shortbot.db")
    r = await g.app.trader.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100, plan=PLAN)
    assert r["ok"]
    with audit.acting("guardian:positions"):
        await g.check_positions()
    assert [(px, q) for px, q, _ in resting(fake)] == [(0.95, 50), (0.9, 50)]
    return g, fake


def book(fake) -> list[tuple]:
    return [(px, q) for px, q, _ in resting(fake)]


@pytest.mark.parametrize("fault", FAULTS)
async def test_target_cancel_in_a_partial_close(fault, tmp_path, monkeypatch):
    g, fake = await targets_resting(tmp_path, monkeypatch)
    t = g.app.trader
    fake.arm("cancel_order", fault, "target")
    res, it = await submit(t, monkeypatch, lambda: t.close(SYM, "trim", fraction=0.5, expect_contracts=100))
    # the close goes on (reduce-only can't flip the position); the target's cancel is in doubt: told, then re-listed
    assert res["ok"] and res["closed_amount"] == 50 and fake.position == 50 and len(cids(fake, "close")) == 1
    assert it["status"] == "unknown" and "target_cancel_failed" in [a["kind"] for a in t.j.alerts(50)]
    assert book(fake) == [(0.9, 50)]                       # the plan's share of what is left: T2's 50, never more
    assert sum(q for _, q in book(fake)) <= fake.position
    await settle(t, monkeypatch)
    assert intents.get(t.j, it["id"])["status"] == "done" and resolutions(t.j) == ["unknown -> done"]
    assert [x["status"] for x in targets.live(t.j)] == ["resting"] and close_stops(fake) == [1.05]
    assert fake.fired == [("cancel_order", "target", fault)]


@pytest.mark.parametrize("fault", FAULTS)
async def test_target_cancel_in_a_flat_pull(fault, tmp_path, monkeypatch):
    g, fake = await targets_resting(tmp_path, monkeypatch)
    fake.ro_expire_on_flat = False                         # Binance may leave them: the desk doesn't rely on it
    fake.trigger_stop()                                    # stopped out on the exchange
    assert fake.position == 0 and len(book(fake)) == 2
    fake.arm("cancel_order", fault, "target")
    with audit.acting("guardian:positions"):
        await g.check_positions()                          # pull_targets: one cancel in doubt, told
        assert "target_cancel_failed" in [a["kind"] for a in g.j.alerts(50)]
        await g.check_positions()                          # the next pass re-lists and pulls what still rests
    await g.j.flush()
    assert book(fake) == [] and targets.live(g.j) == [] and fake.position == 0
    (pull, *_) = g.j._rows("SELECT * FROM intents WHERE kind='pull_targets' ORDER BY rowid")
    assert pull["status"] == "unknown"
    await resolver.resolve(g.app.trader, now=time.time() + LATER)
    await g.j.flush()
    assert intents.get(g.j, pull["id"])["status"] == "done" and resolutions(g.j) == ["unknown -> done"]
    assert intents.unsettled(g.j) == [] and fake.fired == [("cancel_order", "target", fault)]
