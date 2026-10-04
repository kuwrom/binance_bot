"""#36 gateway step 2b: every exchange write of the guardian's fill poll and its withdrawal sweeps runs inside an
intent of its job (actor guardian:entries): the poll and the sweeps decide from reads, a handler sends
(entries_acts.py).
Each result keeps today's shape, each stored request re-binds its handler (step 6 replays it), a quiet pass submits
nothing, the guard in conftest catches a send outside an intent, and a withdrawal that keeps failing backs off."""

import time

import pytest

from shortbot import audit, commands, entry_gates, gateway
from shortbot import killswitch as ks
from shortbot.gateway import LocalGateway
from tests.fakes import FakeCcxt
from tests.gateway_rig import (  # noqa: F401 - the suite runs locally and through the gateway
    guardian_transport,
    transport,
)
from tests.intent_jobs import job, kinds, rows
from tests.test_entries import favorable, prop
from tests.test_entries_cancel import SYM2, fail_cancel, oid_at
from tests.test_entry_intents import SPLIT, on_send
from tests.test_guardian import guardian_with, open_trade
from tests.test_killswitch import SETTINGS

pytestmark = pytest.mark.usefixtures("guardian_transport")

JOB = "guardian:entries"
LATE_OK = {"restore_stop", "force_close", "cap_stop", "protect_fill", "cancel_entry", "withdraw_entries"}


def guardian(price=1.0):
    """A guardian whose desk proposes in a favorable regime, its heartbeat fresh."""
    fake = FakeCcxt(price=price)
    g = guardian_with(fake)
    g.entries._regime_fn = favorable
    g.j.set(ks.HEARTBEAT_KEY, str(time.time()))
    return g, fake


# ------------------------------------------------------------ the fill poll
async def test_a_fill_is_protected_journalled_and_its_oco_siblings_cancelled_in_one_intent(sent):
    g, fake = guardian()
    r = await g.entries.propose_group([prop(1.04), prop(1.06, stop=1.12)])
    first, sib = (x["entry_id"] for x in r["placed"])
    fake.fill_limit(oid_at(fake, 1.04))
    (row,) = await job(g, sent, g.check_entries)
    assert (row["kind"], row["status"], row["symbol"]) == ("protect_fill", "done", "PUMP/USDT:USDT")
    assert row["args"]["entry_id"] == first and row["args"]["order"]["filled"] > 0
    assert row["result"]["event"] == "filled" and row["trade_id"] == row["result"]["trade_id"]
    assert list(fake.stops.values()) == [1.10] and fake.prearms == {}         # its stop; both pre-arms gone
    assert g.entries._rows("id=?", (sib,))[0]["status"] == "cancelled"          # the sibling, inside the same intent
    assert {n for n, _ in sent} == {"create_order", "cancel_order"}


async def test_a_split_entrys_second_leg_joins_the_trade_in_its_own_intent(sent):
    g, fake = guardian(price=1.035)                              # a slow approach: the fast-approach rule stays quiet
    await g.entries.propose_group([prop(1.04, tranches=SPLIT)])
    fake.fill_limit(oid_at(fake, 1.04))
    assert kinds(await job(g, sent, g.check_entries)) == [("protect_fill", "done")]
    fake.fill_limit(oid_at(fake, 1.07))
    (row,) = await job(g, sent, g.check_entries)
    assert row["kind"] == "protect_fill" and row["result"]["event"] == "tranche_joined"


async def expired(g, fake):
    eid = (await g.entries.propose_group([prop(1.04)]))["placed"][0]["entry_id"]
    await g.entries._update(eid, expires_ms=1)
    return "cancel_entry", "expired"


async def ran_away(g, fake):
    await g.entries.propose_group([prop(1.04, cancel_below=0.95)])
    fake.price = 0.94
    return "cancel_entry", "cancelled"


async def sibling_retry(g, fake):
    await g.entries.propose_group([prop(1.04), prop(1.06, stop=1.12)])
    fail_cancel(fake, oid_at(fake, 1.06), times=2)              # on the fill, and on the same-pass retry
    fake.fill_limit(oid_at(fake, 1.04))
    with audit.acting(JOB):
        await g.check_entries()
    return "cancel_entry", "cancelled"


async def forgotten(g, fake):
    eid = (await g.entries.propose_group([prop(1.04)], ttl_hours=1))["placed"][0]["entry_id"]
    fake.advance(3 * 86400 + 3700)                              # GTD lapsed, then Binance forgot it
    await g.entries._update(eid, expires_ms=0)
    return "forget_entry", "expired"


async def abandoned(g, fake):
    on_send(fake, "limit", 2, arm="ack_lost")                   # the near leg rests, its answer lost: abandoned
    await g.entries.propose_group([prop(1.04, tranches=SPLIT)])
    return "withdraw_order", "cancelled"


@pytest.mark.parametrize("case", [expired, ran_away, sibling_retry, forgotten, abandoned])
async def test_each_cancel_the_poll_decides_is_an_intent_of_its_job(case, sent):
    g, fake = guardian(price=1.035 if case is abandoned else 1.0)
    kind, status = await case(g, fake)
    n = len(sent)
    new = await job(g, sent, g.check_entries)
    assert [(r["kind"], r["status"]) for r in new] == [(kind, "done")] and len(sent) > n
    assert g.entries._rows("1=1")[-1]["status"] == status
    if kind == "cancel_entry":
        assert new[0]["args"]["status"] == status and new[0]["result"] == "unfilled"


# ------------------------------------------------------------ the withdrawal sweeps
async def test_the_halted_and_governor_sweeps_withdraw_in_intents_of_the_fill_poll(sent):
    g, fake = guardian()
    ids = [x["entry_id"] for x in (await g.entries.propose_group([prop(1.04), prop(1.06)]))["placed"]]
    entry_gates.publish_governor(g.j, {"halt": True, "reasons": ["daily loss cap hit"]}, ".env baseline")
    (row,) = await job(g, sent, g.check_entries)
    assert (row["kind"], row["status"]) == ("withdraw_entries", "done")
    assert row["args"] == {"reason": "governor halt: daily loss cap hit", "ids": ids}
    assert row["result"] == {"cancelled": ids, "left_for_fill_poll": []}
    g2, fake2 = guardian()
    ids = [x["entry_id"] for x in (await g2.entries.propose_group([prop(1.04)]))["placed"]]
    await ks.trip(g2.j, SETTINGS, ks.HALTED, "agent (MCP)", "halt")
    (row,) = await job(g2, sent, g2.check_entries)
    assert row["kind"] == "withdraw_entries" and row["args"]["reason"] == "kill switch HALTED (guardian sweep)"
    assert row["result"]["cancelled"] == ids and "kill_switch_sweep" in [a["kind"] for a in g2.j.alerts()]
    assert await job(g2, sent, g2.check_entries) == []           # nothing rests: no intent


async def test_a_withdrawal_that_keeps_failing_backs_off_and_resets_on_success(sent, monkeypatch):
    g, fake = guardian()
    (eid,) = [x["entry_id"] for x in (await g.entries.propose_group([prop(1.04)]))["placed"]]
    fail_cancel(fake, oid_at(fake, 1.04), times=10_000)
    await ks.trip(g.j, SETTINGS, ks.HALTED, "agent (MCP)", "halt")
    clock = [1000.0]
    monkeypatch.setattr(entry_gates, "_now", lambda: clock[0])
    for _ in range(10):                                          # 10 passes, 5 s apart: 45 s of a stuck cancel
        await job(g, sent, g.check_entries)
        clock[0] += 5
    tried = [r for r in rows(g.j) if r["kind"] == "withdraw_entries"]
    assert len(tried) <= 5 and all(r["result"]["left_for_fill_poll"] == [eid] for r in tried)
    assert [a["kind"] for a in g.j.alerts(200)].count("kill_switch_sweep") == 0   # nothing withdrawn: never alerted
    del fake.cancel_order                                        # the exchange answers again
    clock[0] += entry_gates.RETRY_MAX_S
    (row,) = await job(g, sent, g.check_entries)
    assert row["result"]["cancelled"] == [eid] and g.entries.retry.fails == {}


# ------------------------------------------------------------ quiet passes, the guard, the registry
async def test_a_quiet_pass_submits_no_intent(sent):
    g, fake = guardian()
    await open_trade(g)
    fake.add_symbol(SYM2, 2.0, tick=0.001)
    assert (await g.entries.propose_group([prop(2.08, stop=2.2, symbol=SYM2)]))["ok"]
    for _ in range(2):
        assert await job(g, sent, g.check_entries) == []
        assert await job(g, sent, g.check_positions, "guardian:positions") == []
        assert await job(g, sent, g.reconcile_entries, "guardian:entry_reconcile") == []


async def test_the_guard_catches_a_guardian_send_outside_an_intent(monkeypatch, order_lock_audit, request):
    if transport(request) == "gateway":
        pytest.skip("an in-process regression: through the gateway, the guardian sends nothing itself")

    async def bypass(self, intent, refusal=None):              # a regression: the handler runs, no intent around it
        return await gateway.handler_for(intent.kind)(self.ctx, **intent.args)
    monkeypatch.setattr(LocalGateway, "submit", bypass)
    g, fake = guardian()
    await g.entries.propose_group([prop(1.04)])
    fake.fill_limit(oid_at(fake, 1.04))
    await g.check_entries()
    assert order_lock_audit and all("outside an intent (from entries.poll)" in b for b in order_lock_audit)
    order_lock_audit.clear()


def test_the_late_ok_kinds_are_the_protective_idempotent_ones():
    from shortbot import guardian  # noqa: F401 - registers the guardian's handlers
    assert gateway.LATE_OK == LATE_OK


def test_no_handler_kind_shadows_a_write_command():
    from shortbot import guardian  # noqa: F401
    assert not set(gateway.HANDLERS) & {c.kind for c in commands.load() if c.write}
