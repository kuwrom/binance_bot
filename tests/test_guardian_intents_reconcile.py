"""#36 gateway step 2b: the entry reconciler's sends (a dead call's split rolled back, an abandoned or late order
withdrawn, a dead entry's pre-arm, orphaned stops) are intents of its job, actor guardian:entry_reconcile, and the
positions pass pulls a flat symbol's targets in one of its own, actor guardian:positions."""

import asyncio

import pytest

from tests.fakes import SYM
from tests.gateway_rig import guardian_transport  # noqa: F401 - the suite runs locally and through the gateway
from tests.intent_jobs import job, kinds, rows
from tests.test_entries import prop
from tests.test_entry_intents import SPLIT, age, on_send, open_limits
from tests.test_guardian_intents import guardian
from tests.test_orphans import failing_trigger_cancels
from tests.test_target_paths import book, placed

pytestmark = pytest.mark.usefixtures("guardian_transport")

JOB = "guardian:entry_reconcile"


async def test_a_dead_calls_split_is_rolled_back_and_its_lost_leg_withdrawn(sent):
    g, fake = guardian(price=1.035)
    on_send(fake, "limit", 2, die=True)                          # the far leg acks, the call dies sending the near one
    with pytest.raises(asyncio.CancelledError):
        await g.entries.propose_group([prop(1.04, tranches=SPLIT)])
    far, near = g.entries._rows("1=1")
    age(g.entries, near["id"])
    new = await job(g, sent, g.reconcile_entries, JOB)
    assert kinds(new) == [("rollback_split", "done"), ("withdraw_order", "done")]
    assert new[0]["args"] == {"entry_id": near["id"], "tranche": near["tranche"]}
    assert new[1]["args"]["entry_id"] == near["id"] and new[1]["result"] == "unfilled"
    assert [r["status"] for r in g.entries._rows("1=1")] == ["cancelled", "cancelled"] and open_limits(fake) == []


async def test_a_late_order_of_a_dead_row_is_withdrawn_in_an_intent(sent):
    g, fake = guardian(price=1.035)
    fake.ack_lost = 1
    eid = (await g.entries.propose_group([prop(1.04)]))["unknown"][0]["entry_id"]
    assert await g.entries._settle(eid, "unknown", status="rejected")   # the engine queued it past the grace
    (oid,) = open_limits(fake)
    (row,) = await job(g, sent, g.reconcile_entries, JOB)
    assert (row["kind"], row["args"], row["result"]) == ("withdraw_order", {"entry_id": eid, "order_id": oid},
                                                         "unfilled")
    assert fake.limits[oid]["status"] == "canceled"


async def test_a_dead_entrys_prearm_is_cancelled_in_an_intent(sent):
    g, fake = guardian(price=1.035)
    on_send(fake, "STOP_MARKET", 1, arm="ack_lost")              # the pre-arm rests, its id unknown
    r = await g.entries.propose_group([prop(1.04)])
    e = g.entries._rows("id=?", (r["placed"][0]["entry_id"],))[0]
    await g.entries.cancel_group(e["group_id"], "level gone")    # a session's cancel: the pre-arm survives
    age(g.entries, e["id"])
    (row,) = await job(g, sent, g.reconcile_entries, JOB)
    assert row["kind"] == "cancel_prearm" and row["args"]["entry_id"] == e["id"] and fake.prearms == {}


async def test_an_orphaned_prearm_is_swept_in_an_intent_per_symbol(sent):
    g, fake = guardian()
    eid = (await g.entries.propose_group([prop(1.04)]))["placed"][0]["entry_id"]
    failing_trigger_cancels(fake)
    assert await g.entries._cancel_entry(g.entries._rows("id=?", (eid,))[0], "cancelled", "test") == "unfilled"
    g.entries._swept_at = 1e18                                   # this pass: the orphan sweep only
    (row,) = await job(g, sent, g.reconcile_entries, JOB)
    assert (row["kind"], row["args"], row["symbol"]) == ("sweep_orphans", {"symbol": SYM}, SYM)
    assert [e["event"] for e in row["result"]] == ["orphan_cancelled"] and fake.prearms == {}


async def test_a_flat_symbols_targets_are_pulled_in_an_intent_of_the_positions_pass(tmp_path, sent):
    g, fake, tid = await placed(tmp_path)
    fake.ro_expire_on_flat = False                               # left resting at flat: the desk cancels them
    fake.trigger_stop()
    new = await job(g, sent, g.check_positions, "guardian:positions")
    assert kinds(new) == [("pull_targets", "done")] and new[0]["args"] == {"symbol": SYM}
    assert book(fake) == [] and [t["status"] for t in g.j.trades()] == ["closed"]
    assert rows(g.j)[-1]["kind"] == "pull_targets"
