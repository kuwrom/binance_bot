"""Kill switch actions: HALTED withdraws entries (never a filled leg's stop), flatten-all, the guardian watchdog with its
stale-heartbeat auto-clear, guardian auto-trip thresholds, brief/status, CLI and MCP surface."""

import argparse
import json
import time
from types import SimpleNamespace

import pytest

from shortbot import killswitch as ks
from shortbot import killswitch_ops as ops
from shortbot.config import RiskLimits, Settings
from shortbot.entries import EntryDesk
from shortbot.journal import Journal
from shortbot.killswitch import ACTIVE, HALTED, REDUCING, STALE
from shortbot.params import ResearchParams
from tests.fakes import SYM
from tests.test_killswitch import SETTINGS, favorable, open_default, prop, trader


async def desk_with_two_entries():
    t, fake = trader()
    d = EntryDesk(t, regime_fn=favorable)
    r = await d.propose_group([prop(1.04), prop(1.06, stop=1.12)])
    ids = {x["limit_price"]: x["entry_id"] for x in r["placed"]}
    oids = {o["price"]: i for i, o in fake.limits.items()}
    return t, fake, d, ids, oids


# ------------------------------------------------------------ HALTED sweep / flatten
async def test_halted_withdraws_unfilled_entries_but_a_part_filled_one_keeps_its_stop_for_the_poll():
    t, fake, d, ids, oids = await desk_with_two_entries()
    fake.fill_limit(oids[1.06], qty=10)                     # part-filled a moment before the halt
    r = await ops.halt(t, HALTED, "agent (MCP)", "news shock")
    assert r["resting"] == {"cancelled": [ids[1.04]], "left_for_fill_poll": [ids[1.06]]}
    assert list(fake.prearms.values()) == [1.12]            # the filled leg's pre-armed stop stays
    ev = [e for e in await d.poll() if e["event"] == "filled"]
    assert ev and list(fake.stops.values()) == [1.12] and fake.position == 10   # protected and journalled
    assert t.j.get_trade(ev[0]["trade_id"])["status"] == "open"


async def test_halted_rejects_a_send_that_never_reached_the_book_and_waits_on_a_young_one():
    from tests.test_entry_intents import age
    t, fake = trader()
    d = EntryDesk(t, regime_fn=favorable)
    fake.ack_lost_unsent = 2                                  # two sends whose answers were lost; neither ran
    old, young = [x["entry_id"] for x in (await d.propose_group([prop(1.04), prop(1.06, stop=1.12)]))["unknown"]]
    age(d, old)                                               # GRACE_S since its send: it never reached the book
    r = await ops.halt(t, HALTED, "agent (MCP)", "news shock")
    assert r["resting"] == {"cancelled": [old], "left_for_fill_poll": [young]}
    rows = {e["id"]: e for e in d._rows("1=1")}
    assert rows[old]["status"] == "rejected" and "never placed" in rows[old]["reason"]   # not 'cancelled' (#66)
    assert rows[old]["reason"].startswith("kill switch HALTED: news shock")
    assert rows[young]["status"] == "unknown"                 # may still be in flight: the next sweep retries


async def test_reducing_leaves_resting_entries_alone():
    t, fake, d, ids, oids = await desk_with_two_entries()
    r = await ops.halt(t, REDUCING, "guardian", "daily loss")
    assert "resting" not in r and len(d.pending()) == 2 and len(fake.prearms) == 2


async def test_flatten_closes_every_short_reduce_only_and_stays_halted():
    t, fake = trader()
    assert (await open_default(t))["ok"]
    r = await ops.flatten(t, "agent (MCP)", "BTC squeeze", "flattened into a squeeze")
    assert r["ok"] and fake.position == 0 and ks.read(t.j)["state"] == HALTED and ks.read(t.j)["cause"] == "flatten"
    side, qty, params = fake.orders[-1][1:]
    assert (side, qty, params["reduceOnly"]) == ("buy", 100, True) and params["clientOrderId"].startswith("sb-c")
    tr = t.j.trades()[0]
    assert tr["status"] == "closed" and "flatten all" in tr["exit_reason"] and tr["lesson"]
    assert t.j.decisions()[0]["action"] == "flatten_all"


async def test_a_failed_flatten_keeps_the_stop_and_alerts():
    t, fake = trader()
    assert (await open_default(t))["ok"]
    fake.fail_close = 5
    r = await ops.flatten(t, "shortbot CLI", "panic")
    assert not r["ok"] and r["still_open"] == [SYM] and list(fake.stops.values()) == [1.05]
    assert any(a["kind"] == "flatten_incomplete" for a in t.j.alerts())


# ------------------------------------------------------------ guardian watchdog
async def test_no_heartbeat_ever_blocks_without_tripping():
    t, fake = trader()
    t.j.db.execute("DELETE FROM kv WHERE k=?", (ks.HEARTBEAT_KEY,))
    r = await open_default(t)
    assert not r["ok"] and "never run" in r["error"] and fake.orders == []
    assert ks.read(t.j)["state"] == ACTIVE


async def test_stale_heartbeat_halts_withdraws_entries_then_lifts_itself_when_the_guardian_is_back():
    t, fake, d, ids, oids = await desk_with_two_entries()
    t.j.set(ks.HEARTBEAT_KEY, str(time.time() - ks.STALE_HEARTBEAT_S - 60))   # laptop slept
    r = await open_default(t)
    st = ks.read(t.j)
    assert not r["ok"] and st["state"] == HALTED and st["cause"] == STALE and st["by"] == "session watchdog"
    assert d.pending() == [] and fake.prearms == {} and fake.orders[-1][0] != "market"
    t.j.set(ks.HEARTBEAT_KEY, str(time.time() - 1))                            # guardian pass 1
    assert (await ks.gate(t, "x"))["kill_switch"] == HALTED
    assert (await ks.gate(t, "x"))["kill_switch"] == HALTED                    # same pass, re-read: no count
    t.j.set(ks.HEARTBEAT_KEY, str(time.time()))                                # guardian pass 2
    assert await ks.gate(t, "x") is None and ks.read(t.j)["state"] == ACTIVE   # lifted
    assert t.j.decisions()[0]["action"] == "kill_switch_auto_clear" and d.pending() == []


async def test_a_sticky_trip_during_a_stale_halt_outlives_the_auto_clear():
    now = time.time()
    for order in ("before", "during"):
        j = Journal(":memory:")
        j.set(ks.HEARTBEAT_KEY, str(now))
        if order == "before":
            await ks.trip_on_daily_loss(j, SETTINGS, 9_600, -400)
        await ks.trip(j, SETTINGS, HALTED, "session watchdog", "stale", cause=STALE)
        if order == "during":
            assert not (await ks.trip_on_daily_loss(j, SETTINGS, 9_600, -400))["changed"]
            assert ks.read(j)["state"] == HALTED
        await ks.auto_clear_stale(j, SETTINGS, now)
        j.set(ks.HEARTBEAT_KEY, str(now - 1))   # the next guardian pass
        back = await ks.auto_clear_stale(j, SETTINGS, now)
        assert back["state"] == REDUCING and back["cause"] == "daily_loss" and ks.read(j)["by"] == "guardian"


async def test_auto_clear_counts_distinct_guardian_passes_not_checks_from_any_process():
    now = time.time()
    j = Journal(":memory:")
    await ks.trip(j, SETTINGS, HALTED, "session watchdog", "stale", cause=STALE)
    j.set(ks.HEARTBEAT_KEY, str(now - 5))
    for _ in range(4):   # one good pass, read by the guardian, the MCP server, the brief, a CLI call...
        await ks.auto_clear_stale(j, SETTINGS, now)
    assert ks.read(j)["state"] == HALTED
    j.set(ks.HEARTBEAT_KEY, str(now - 1))   # a second, different pass
    assert (await ks.auto_clear_stale(j, SETTINGS, now))["state"] == ACTIVE


async def test_auto_clear_needs_consecutive_fresh_passes_and_never_lifts_other_causes():
    now = time.time()
    j = Journal(":memory:")
    await ks.trip(j, SETTINGS, HALTED, "session watchdog", "stale", cause=STALE)
    for age, expect in ((5, HALTED), (ks.VERIFIED_S + 20, HALTED), (4, HALTED), (3, ACTIVE)):   # unverified: reset
        j.set(ks.HEARTBEAT_KEY, str(now - age))
        await ks.auto_clear_stale(j, SETTINGS, now)
        assert ks.read(j)["state"] == expect
    await ks.trip(j, SETTINGS, HALTED, "agent (MCP)", "manual halt")
    for _ in range(3):
        await ks.auto_clear_stale(j, SETTINGS, now)
    assert ks.read(j)["state"] == HALTED and ks.read(j)["cause"] == "manual"


# ------------------------------------------------------------ auto-trip thresholds
async def test_daily_loss_trip_follows_the_effective_limit_and_records_which():
    j = Journal(":memory:")
    assert await ks.trip_on_daily_loss(j, SETTINGS, 9_750, -250) is None          # -2.5% vs 3% baseline
    assert await ks.trip_on_daily_loss(j, SETTINGS, 9_705, -295) is None   # -2.95% of START equity (not -3.04%)
    assert (await ks.trip_on_daily_loss(j, SETTINGS, 9_650, -350))["changed"]
    assert json.loads(j.decisions()[0]["snapshot"])["evidence"]["limit_source"] == ".env baseline"
    j2 = Journal(":memory:")
    ResearchParams(SETTINGS, j2).set({"daily_loss_limit_pct": 5}, "wider day for fades", "dl5")
    assert await ks.trip_on_daily_loss(j2, SETTINGS, 9_650, -350) is None
    r = await ks.trip_on_daily_loss(j2, SETTINGS, 9_400, -600)
    assert r["cause"] == "daily_loss" and "research override (dl5)" in r["reason"]
    live = Settings("live", "k", "s", ":memory:", "", "", RiskLimits())          # AI_PARAMS_IN_LIVE off
    j3 = Journal(":memory:")
    j3.set("research_params", json.dumps({"daily_loss_limit_pct": 5}))
    assert (await ks.trip_on_daily_loss(j3, live, 9_650, -350))["changed"]


async def test_drawdown_stop_trips_reducing_for_a_human_review():
    j = Journal(":memory:")
    assert await ks.trip_on_drawdown(j, SETTINGS, 11.9) is None
    r = await ks.trip_on_drawdown(j, SETTINGS, 12.0)
    assert r["state"] == REDUCING and r["cause"] == "drawdown"


# ------------------------------------------------------------ visibility, CLI, MCP
async def test_brief_line_says_what_tripped_it_and_how_it_clears():
    j, now = Journal(":memory:"), time.time()
    j.set(ks.HEARTBEAT_KEY, str(now - 4))
    assert ks.brief_line(j, now).startswith("### kill switch: ACTIVE; guardian heartbeat 4 s ago")
    assert "UNVERIFIED" in ks.brief_line(j, now + 200)
    await ks.trip_on_daily_loss(j, SETTINGS, 9_600, -400)
    line = ks.brief_line(j, now)
    assert "REDUCING" in line and "guardian (daily_loss): daily loss -4.00%" in line
    assert "shortbot-admin clear" in line and "not via Claude" in line
    j2 = Journal(":memory:")
    await ks.trip(j2, SETTINGS, HALTED, "session watchdog", "stale", cause=STALE)
    assert "lifts itself" in ks.brief_line(j2, now)


async def test_cli_shows_and_trips_but_cannot_clear(monkeypatch):
    from shortbot import commands, mcp_kill
    t, fake = trader()
    monkeypatch.setattr(mcp_kill, "get_app", lambda: SimpleNamespace(trader=t, journal=t.j))
    p = commands.build_parser()

    async def cli(*argv):
        cmd = commands.by_cli("kill-switch")
        return await cmd.body(**cmd.cli_kwargs(p.parse_args(["kill-switch", *argv])))
    assert (await cli())["state"] == ACTIVE
    assert "required" in (await cli(HALTED))["error"]
    assert (await cli(HALTED, "--reason", "halt"))["by"] == ops.CLI_BY
    with pytest.raises(SystemExit):
        p.parse_args(["kill-switch", ACTIVE])


async def test_mcp_tools_can_trip_and_flatten_but_nothing_clears():
    from shortbot.mcp_server import mcp
    tools = {x.name: x for x in await mcp.list_tools()}
    assert {"flatten_all", "halt_trading", "trading_state"} <= set(tools)
    assert not any((t.meta or {}).get("anthropic/requiresUserInteraction") for t in tools.values())
    assert not [n for n in tools if "clear" in n or "resume" in n or "unhalt" in n]
