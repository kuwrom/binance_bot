"""Every write goes through one submit path (#36 gateway step 2): each MCP write tool and each writing subcommand
creates exactly one intent; real order flows link their client ids and decision rows to it; a process that dies
between journalling and sending is resolved as before; the guardian's protective actions are intents of its jobs."""

import asyncio
import json
import typing
from types import SimpleNamespace

import pytest

from shortbot import (audit, cli, cli_cmds, cmd_book, cmd_desk, cmd_kill, cmd_research, cmd_trade, commands,
                      entry_intents, gateway, intents)
from shortbot.commands import build_parser
from shortbot.entries import EntryDesk
from shortbot.gateway import LocalGateway
from shortbot.intents import Intent
from shortbot.journal import Journal
from shortbot.mcp_server import TOOLS
from shortbot.trading import Trader
from tests.fakes import FakeCcxt, FakeEx, neutral_budget, ok_prechecks
from tests.test_entries import favorable, prop
from tests.test_entry_intents import age
from tests.test_guardian import guardian_with, open_trade

WRITES = [c for c in commands.load() if c.write]
MODULES = (cmd_trade, cmd_research, cmd_book, cmd_desk, cmd_kill, cli_cmds)


@pytest.fixture(autouse=True)
def fresh_process(monkeypatch):
    monkeypatch.setattr(audit, "_proc", {"kind": None, "claude_session": None})
    monkeypatch.setattr(audit, "_seen", {})


def _intents(j):
    return j._rows("SELECT * FROM intents ORDER BY rowid")


# ------------------------------------------------------------ every write, both surfaces
def _dummy(p):
    t = commands._core(p.annotation)
    return {str: "QNT", float: 1.0, int: 1, bool: False, dict: {}, list: []}.get(typing.get_origin(t) or t, "x")


def _argv(cmd) -> list[str]:
    """The shortest command line the parser accepts: required arguments only, each a value of its type."""
    out = [cmd.cli]
    for a in cmd.args:
        kw = cmd._spec(a)
        if kw.get("action") in ("store_true", "store_false") or kw.get("nargs") in ("?", "*") or (
                a.flag and not kw.get("required")):
            continue
        p = None if a.cli_only else cmd.sig.parameters[a.name]
        core = commands._core(p.annotation) if p is not None else None
        if a.choices:
            v = str(a.choices[0])
        elif a.type is not None and a.type not in (int, float):
            v = "max_leverage=2"   # set-params: KEY=VALUE pairs
        elif a.convert is json.loads:
            v = "{}" if (typing.get_origin(core) or core) is dict else "[]"
        elif kw.get("type") in (int, float):
            v = "1"
        else:
            v = "QNT"
        out += [a.flag, v] if a.flag else [v]
    return out


@pytest.fixture
def stubbed(tmp_path, monkeypatch):
    """Every handler replaced by a stub (the surfaces' wiring is under test, not the bodies); one journal for all."""
    j = Journal(tmp_path / "j.db")
    app = SimpleNamespace(journal=j)
    for mod in (*MODULES, cli):
        monkeypatch.setattr(mod, "get_app", lambda *a, **k: app)

    async def no_close():
        return None
    monkeypatch.setattr(cli, "close_app", no_close)
    ran = []

    def handler_for(kind):
        async def stub(ctx, **args):
            ran.append((kind, args))
            return {"ok": True}
        return stub
    monkeypatch.setattr(gateway, "handler_for", handler_for)
    return j, ran


@pytest.mark.parametrize("cmd", [c for c in WRITES if c.name], ids=lambda c: c.name)
async def test_every_mcp_write_creates_exactly_one_intent(stubbed, cmd):
    j, ran = stubbed
    args = {n: _dummy(p) for n, p in cmd.mcp_signature().parameters.items() if p.default is p.empty}
    assert await TOOLS[cmd.name](**args) == {"ok": True}
    (row,) = _intents(j)
    assert (row["kind"], row["actor"], row["status"]) == (cmd.kind, f"mcp:{cmd.name}", "done")
    assert ran == [(cmd.kind, json.loads(row["args"]))]


@pytest.mark.parametrize("cmd", [c for c in WRITES if c.cli], ids=lambda c: c.cli)
async def test_every_cli_write_creates_exactly_one_intent(stubbed, cmd):
    j, ran = stubbed
    assert await cli._audited(build_parser().parse_args(_argv(cmd))) == {"ok": True}
    (row,) = _intents(j)
    assert (row["kind"], row["actor"], row["status"]) == (cmd.kind, f"cli:{cmd.cli}", "done")
    assert ran == [(cmd.kind, json.loads(row["args"]))] and set(ran[0][1]) == set(cmd.sig.parameters)


def test_each_write_has_its_own_kind():
    assert len({c.kind for c in WRITES}) == len(WRITES) and all(commands.by_kind(c.kind) is c for c in WRITES)
    assert commands.by_kind("positions") is None   # a read is never an intent


# ------------------------------------------------------------ real flows: orders and rows link to their intent
async def test_an_open_and_a_close_link_their_orders_and_decisions(tmp_path, monkeypatch):
    fake = FakeCcxt()
    t = Trader(FakeEx(fake), Journal(tmp_path / "j.db"), prechecks=ok_prechecks, budget=neutral_budget)
    app = SimpleNamespace(journal=t.j, trader=t, settings=t.ex.settings, ex=t.ex)
    monkeypatch.setattr(cmd_trade, "get_app", lambda: app)
    res = await TOOLS["open_short"]("PUMP", 1.05, "pump_fade", "lower high", notional_usdt=100)
    assert res["ok"], res
    opened, = _intents(t.j)
    links = {o["kind"]: o["client_id"] for o in intents.orders(t.j, opened["id"])}
    trade = t.j.get_trade(res["trade_id"])
    assert set(links) == {"open", "stop"} and links["open"] == trade["entry_features"]["client_id"]
    assert opened["trade_id"] == res["trade_id"] and opened["symbol"] == "PUMP/USDT:USDT"
    assert await TOOLS["close_position"]("PUMP", "target hit", "faded as planned")
    _, closed = _intents(t.j)
    assert [o["kind"] for o in intents.orders(t.j, closed["id"])] == ["close"]
    rows = t.j.decisions()
    assert {d["intent_id"] for d in rows} == {opened["id"], closed["id"]}


# ------------------------------------------------------------ a crash between journalling and sending (§5)
async def test_a_process_that_dies_mid_send_is_swept_and_its_entry_resolved_as_before(tmp_path, monkeypatch):
    fake = FakeCcxt(price=1.035)
    t = Trader(FakeEx(fake), Journal(tmp_path / "j.db"), prechecks=ok_prechecks, budget=neutral_budget)
    d = EntryDesk(t, regime_fn=favorable)
    sending, never = asyncio.Event(), asyncio.Event()
    send = fake.create_order

    async def hang(s, type_, side, amount, price=None, params=None):
        if type_ == "limit":
            sending.set()
            await never.wait()   # the process dies here: the limit never reaches the book
        return await send(s, type_, side, amount, price, params)
    fake.create_order = hang

    async def propose(ctx):
        return await d.propose_group([prop(1.04)])
    monkeypatch.setitem(gateway.HANDLERS, "t_propose", propose)
    it = Intent("t_propose", {})
    task = asyncio.create_task(LocalGateway(lambda: t.j).submit(it))
    await sending.wait()
    other = Journal(tmp_path / "j.db")   # the guardian's view of the shared journal
    (entry,) = other._rows("SELECT * FROM entries")
    assert intents.get(other, it.id)["status"] == "running" and entry["status"] == "placing"
    assert [o["client_id"] for o in intents.orders(other, it.id)] == [entry["client_id"]]
    monkeypatch.setattr(intents, "finish", lambda *a, **k: False)   # a dead process records nothing more
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (swept,) = intents.sweep(other, alive=lambda pid: False)
    assert (swept["id"], swept["status"], swept["orders"]) == (it.id, "interrupted", [entry["client_id"]])
    guardian = EntryDesk(Trader(FakeEx(fake), other, prechecks=ok_prechecks, budget=neutral_budget))
    age(guardian, entry["id"])
    await entry_intents.reconcile(guardian)   # as before #36: a send that never reached the book is rejected
    assert guardian._rows("id=?", (entry["id"],))[0]["status"] == "rejected" and fake.limits == {}


# ------------------------------------------------------------ the guardian's protective actions
async def test_guardian_restore_force_close_and_plan_are_intents_of_its_job():
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    fake.stops.clear()
    with audit.acting("guardian:positions"):
        await g.check_positions()                 # restores the stop
        fake.price = 0.93
        await g.check_positions()                 # the plan's breakeven
        fake.stops.clear()
        fake.fail_stop = 99
        for _ in range(3):
            await g.check_positions()             # restores fail, then the forced close
    rows = _intents(g.j)
    assert {r["actor"] for r in rows} == {"guardian:positions"}
    kinds = [(r["kind"], r["status"]) for r in rows]
    assert kinds[0] == ("restore_stop", "done") and ("plan_step", "done") in kinds
    assert ("restore_stop", "failed") in kinds and kinds[-1] == ("force_close", "done")
    assert json.loads(rows[-1]["result"])["closed"] is True and fake.position == 0
    restore = rows[0]
    assert [o["kind"] for o in intents.orders(g.j, restore["id"])] == ["stop"]
    plan = next(r for r in rows if r["kind"] == "plan_step")
    assert json.loads(plan["args"])["seen"]["actions"] == ["move_stop"]


async def test_guardian_cap_stop_is_an_intent_of_the_margin_pass(monkeypatch):
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    capped = {"symbol": "X", "from": 1.2, "to": 1.1, "liquidation": 1.15, "mark": 1.0, "action": "tightened"}

    async def cap_stop(symbol):
        return {**capped, "symbol": symbol}
    monkeypatch.setattr(g.app.trader, "cap_stop", cap_stop)
    with audit.acting("guardian:positions"):
        await g._cap_stop("PUMP/USDT:USDT", {})
    (row,) = _intents(g.j)
    assert (row["kind"], row["actor"], row["status"]) == ("cap_stop", "guardian:positions", "done")
    assert "plan_stop_unreachable" in [a["kind"] for a in g.j.alerts()]


async def test_the_reconcile_job_sweeps_dead_intents_and_alerts_their_orders(monkeypatch):
    g = guardian_with(FakeCcxt())
    it = Intent("restore_stop", {"symbol": "PUMP/USDT:USDT", "fallback_pct": 10.0}, "PUMP/USDT:USDT")
    intents.receive(g.j, it)
    intents.start(g.j, it.id)
    with audit.within(it.id):
        intents.link(g.j, "sb-sPUMP-1", "PUMP/USDT:USDT")
    monkeypatch.setattr(intents, "_alive", lambda pid: False)
    await g.reconcile_entries()
    assert intents.get(g.j, it.id)["status"] == "interrupted"
    (a,) = [a for a in g.j.alerts() if a["kind"] == "intent_interrupted"]
    assert "sb-sPUMP-1" in a["message"] and a["symbol"] == "PUMP/USDT:USDT"
