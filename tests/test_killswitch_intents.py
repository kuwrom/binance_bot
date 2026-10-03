"""#36 gateway step 2b: the session watchdog's stale-heartbeat halt (killswitch.watch_guardian) trips the switch in the
journal, then withdraws the resting entries inside an intent: one of its own, actor the reader's, when it runs from a
read (trading_state, the brief); the running intent's when it runs from a write's opening gate."""

import time

import pytest

from shortbot import audit, gateway
from shortbot import killswitch as ks
from shortbot.gateway import LocalGateway
from shortbot.intents import Intent
from shortbot.killswitch import HALTED, STALE
from tests.intent_jobs import rows
from tests.test_killswitch_ops import desk_with_two_entries


def stale(t):
    t.j.set(ks.HEARTBEAT_KEY, str(time.time() - ks.STALE_HEARTBEAT_S - 60))   # the laptop slept


@pytest.mark.parametrize("reader", ["mcp:trading_state", "cli:brief"])
async def test_a_read_that_finds_the_guardian_stale_withdraws_in_an_intent_of_its_own(reader, sent):
    t, fake, d, ids, oids = await desk_with_two_entries()
    stale(t)
    n = len(sent)
    with audit.acting(reader):
        why = await ks.watch_guardian(t)
    st = ks.read(t.j)
    assert "unguarded" in why and (st["state"], st["cause"], st["by"]) == (HALTED, STALE, "session watchdog")
    (row,) = rows(t.j)
    assert (row["kind"], row["actor"], row["status"]) == ("withdraw_entries", reader, "done")
    assert row["args"]["reason"].startswith("kill switch HALTED: guardian heartbeat")
    assert sorted(row["result"]["cancelled"]) == sorted(ids.values()) and d.pending() == [] and fake.prearms == {}
    assert len(sent) > n and {i for _, i in sent[n:]} == {row["id"]}


async def test_a_gate_inside_a_running_intent_withdraws_inside_that_intent(sent, monkeypatch):
    t, fake, d, ids, oids = await desk_with_two_entries()
    stale(t)
    n = len(sent)

    async def opening(ctx):                                      # a write whose opening gate finds the guardian stale
        return await ks.gate(t, "open_short")
    monkeypatch.setitem(gateway.HANDLERS, "t_opening", opening)
    outer = Intent("t_opening", {})
    with audit.acting("mcp:open_short"):
        res = await LocalGateway(lambda: t.j).submit(outer)
    assert res["kill_switch"] == HALTED and d.pending() == []
    assert [r["kind"] for r in rows(t.j)] == ["t_opening"] and {i for _, i in sent[n:]} == {outer.id}
