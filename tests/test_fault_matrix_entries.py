"""#46 fault matrix, part 2 (part 1 and the cell invariants: test_fault_matrix.py): a limit entry's send, its cancel
and a plan target's send, each run as the desk runs it (an intent), each meeting every answer of unknown outcome.

| op           | 1007_ran              | 1007_unsent          | 503_unknown           | 503_unavailable       | 1008 | 1021_after |
| limit entry  | entry_intents::unknown_send_is_never_resent (ack_lost) | entry_intents::unknown_send_that_never_reached (ack_lost_unsent) | entry_intents (ack_lost_503) | entry_intents (unavailable_503) | new | new |
| cancel entry | new                   | new                  | new                   | new                   | new  | new (entries_cancel::an_agent_cancel_that_fails: -1021 before it ran) |
| target       | target_orders::a_lost_answer_is_resolved | target_orders::a_send_that_never_landed | new | new | skip | new |
"covered": the row's resolution is pinned there (without the intent, the resolver or these invariants); skip: -1008
on a reduce-only order is not a Binance answer (test_fault_matrix.EXEMPT)."""

import pytest

from shortbot import audit, entry_intents, intents, targets
from shortbot.entries import EntryDesk
from shortbot.journal import Journal
from shortbot.trading import Trader
from tests.fake_faults import RAN, FaultyCcxt
from tests.fakes import FakeEx, neutral_budget, ok_prechecks
from tests.test_entries import favorable, prop
from tests.test_entry_intents import age, open_limits
from tests.test_fault_matrix import bridges, cids, close_stops, faults, resolutions, settle
from tests.test_intent_status import submit
from tests.test_target_orders import PLAN, guardian, placed_by_the_guardian, resting


def desk() -> tuple[EntryDesk, Trader, FaultyCcxt]:
    fake = FaultyCcxt(price=1.035)   # a slow approach (the fast-approach rule stays quiet)
    t = Trader(FakeEx(fake), Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
    return EntryDesk(t, regime_fn=favorable), t, fake


def row(d: EntryDesk, eid: int) -> dict:
    return d._rows("id=?", (eid,))[0]


# ------------------------------------------------------------ the limit entry's send
@pytest.mark.parametrize("fault", faults())
async def test_limit_entry(fault, monkeypatch):
    d, t, fake = desk()
    fake.arm("create_order", fault, "entry")
    res, it = await submit(t, monkeypatch, lambda: d.propose_group([prop(1.04)]))
    ran = fault in RAN
    (u,) = res["unknown"]
    assert it["status"] == "unknown" and not res["placed"] and fake.prearms == {}   # no pre-arm before an ack
    assert row(d, u["entry_id"])["status"] == "unknown"
    await entry_intents.reconcile(d)                       # its first tick: found resting, or not yet (in flight?)
    assert row(d, u["entry_id"])["status"] == ("pending" if ran else "unknown")
    age(d, u["entry_id"])
    await entry_intents.reconcile(d)
    await settle(t, monkeypatch)
    assert len(cids(fake, "entry")) == 1 and len(fake.limits) == (1 if ran else 0)   # never re-sent
    assert row(d, u["entry_id"])["status"] == ("pending" if ran else "rejected")
    assert intents.get(t.j, it["id"])["status"] == ("done" if ran else "refused")
    assert resolutions(t.j) == [f"unknown -> {'done' if ran else 'refused'}"]
    if ran:   # adopted without a pre-arm: its fill gets the whole-position stop from the poll
        fake.fill_limit(open_limits(fake)[0])
        assert [e["event"] for e in await d.poll()] == ["filled"]
        assert close_stops(fake) == [1.10] and fake.prearms == {} and fake.position > 0
    assert len(cids(fake, "entry")) == 1 and fake.fired == [("create_order", "entry", fault)]


# ------------------------------------------------------------ cancelling it
@pytest.mark.parametrize("fault", faults())
async def test_cancel_entry(fault, monkeypatch):
    d, t, fake = desk()
    r = await d.propose_group([prop(1.04)])
    (p,) = r["placed"]
    e = row(d, p["entry_id"])
    assert e["prearm_id"] in fake.prearms
    fake.arm("cancel_order", fault, "entry")
    res, it = await submit(t, monkeypatch, lambda: d.cancel_group(r["group_id"], "level gone"))
    assert res["cancel_failed"] == [e["id"]] and it["status"] == "failed"   # in doubt: the row and its stop stay
    assert row(d, e["id"])["status"] == "pending" and e["prearm_id"] in fake.prearms
    ran = fault in RAN
    assert open_limits(fake) == ([] if ran else [e["order_id"]])
    await d.poll()   # the guardian's next pass: an order Binance shows dead is retired with its pre-arm
    if not ran:      # still live: the agent was told cancel_failed and asks again
        res2, it2 = await submit(t, monkeypatch, lambda: d.cancel_group(r["group_id"], "level gone"))
        assert res2["ok"] and res2["cancelled"] == 1 and it2["status"] == "done"
    now = row(d, e["id"])
    assert now["status"] == ("expired" if ran else "cancelled") and "canceled" in fake.limits[e["order_id"]]["status"]
    assert open_limits(fake) == [] and fake.prearms == {} and fake.position == 0 and fake.stops == {}
    assert resolutions(t.j) == [] and fake.fired == [("cancel_order", "entry", fault)]


# ------------------------------------------------------------ a plan target's send (the guardian's pass)
@pytest.mark.parametrize("fault", faults(reduce_only=True))
async def test_target(fault, tmp_path, monkeypatch):
    placed_by_the_guardian(monkeypatch)
    fake = FaultyCcxt()
    g = guardian(fake, tmp_path / "shortbot.db")
    r = await g.app.trader.open_short("PUMP/USDT:USDT", stop_price=1.05, setup="pump_fade", thesis="t", notional=100,
                                      plan=PLAN)
    assert r["ok"] and resting(fake) == []
    fake.arm("create_order", fault, "target")
    with audit.acting("guardian:positions"):
        await g.check_positions()
        ran = fault in RAN
        assert [x["status"] for x in targets.rows(g.j)] == ["resting" if ran else "unknown", "resting"]
        await g.check_positions()                           # within the grace: never re-sent
        assert len(cids(fake, "target")) == 2
        monkeypatch.setattr("shortbot.targets_sync.GRACE_S", -1)
        await g.check_positions()                           # not found past the grace: never placed, so placed again
    await g.j.flush()
    assert [(px, q) for px, q, _ in resting(fake)] == [(0.95, 50), (0.9, 50)]
    assert len(cids(fake, "target")) == (2 if ran else 3)
    assert [x["status"] for x in targets.rows(g.j)][:2] == ["resting" if ran else "lost", "resting"]
    assert close_stops(fake) == [1.05] and bridges(fake) == [] and fake.position == 100
    assert {x["status"] for x in g.j._rows("SELECT status FROM intents")} == {"done"} and resolutions(g.j) == []
    assert fake.fired == [("create_order", "target", fault)]
