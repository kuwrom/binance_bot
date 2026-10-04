"""With SHORTBOT_GATEWAY on (#36 step 4), a session's order-sending writes go to the gateway process: every MCP tool
and `shortbot` subcommand declared orders=True, the web console's, and the session watchdog's withdrawal. Journal-only
writes stay in-process; with the flag off, or in the guardian, nothing changes even while a gateway runs."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from shortbot import audit, cli, cmd_trade, commands, gateway, gateway_client, gateway_server, intents, killswitch_ops
from shortbot.commands import build_parser
from shortbot.config import Settings
from shortbot.entries import EntryDesk
from shortbot.gateway import LocalGateway
from shortbot.journal import Journal
from shortbot.mcp_server import TOOLS
from shortbot.trading import Trader
from shortbot.web import runner
from tests.fakes import FakeCcxt, FakeEx, neutral_budget, ok_prechecks
from tests.gateway_rig import as_session, flag_on, served
from tests.test_intent_paths import MODULES, _argv, _dummy

ORDERS = [c for c in commands.load() if c.orders]


def rows(j):
    return j._rows("SELECT * FROM intents ORDER BY rowid")


@pytest.fixture
def served_ids(monkeypatch):
    """The intent ids the gateway server ran (it runs in this process: the pid can't tell)."""
    out, real = [], gateway_server.GatewayServer.execute

    async def execute(self, intent, *a, **k):
        out.append(intent.id)
        return await real(self, intent, *a, **k)
    monkeypatch.setattr(gateway_server.GatewayServer, "execute", execute)
    return out


@pytest.fixture
def stubbed(tmp_path, monkeypatch, served_ids):
    """Every handler a stub (the routing is under test, not the bodies); one journal; the flag on."""
    j = Journal(tmp_path / "j.db")
    app = SimpleNamespace(journal=j, settings=Settings("demo", "", "", tmp_path / "j.db", "", "", gateway=True))
    for mod in (*MODULES, cli, runner):
        monkeypatch.setattr(mod, "get_app", lambda *a, **k: app)

    async def no_close():
        return None
    monkeypatch.setattr(cli, "close_app", no_close)
    ran = []

    def handler_for(kind):
        async def stub(ctx, **args):
            ran.append((kind, ctx))
            return {"ok": True}
        return stub
    monkeypatch.setattr(gateway, "handler_for", handler_for)
    return SimpleNamespace(j=j, app=app, ran=ran, served=served_ids)


def test_the_order_commands_are_declared():
    assert {c.kind for c in ORDERS} == {
        "open_short", "close_position", "move_stop", "update_plan", "widen_stop", "add_to_winner", "propose_entries",
        "cancel_entry_group", "amend_entry", "kill_switch", "halt_trading", "flatten_all", "ladder"}
    assert all(c.write for c in ORDERS) and not commands.by_kind("probe").orders   # probes stay raw (GATEWAY §1.2)


@pytest.mark.parametrize("cmd", [c for c in ORDERS if c.name], ids=lambda c: c.name)
async def test_every_order_tool_goes_through_the_gateway(stubbed, monkeypatch, cmd):
    async with served(stubbed.j):
        as_session(monkeypatch, "mcp", "sess-1")
        args = {n: _dummy(p) for n, p in cmd.mcp_signature().parameters.items() if p.default is p.empty}
        assert await TOOLS[cmd.name](**args) == {"ok": True}
    (row,) = rows(stubbed.j)
    assert stubbed.served == [row["id"]] and stubbed.ran == [(cmd.kind, None)]
    assert (row["kind"], row["actor"], row["status"], row["claude_session"]) == (
        cmd.kind, f"mcp:{cmd.name}", "done", "sess-1")


@pytest.mark.parametrize("cmd", [c for c in ORDERS if c.cli], ids=lambda c: c.cli)
async def test_every_order_subcommand_goes_through_the_gateway(stubbed, monkeypatch, cmd):
    async with served(stubbed.j):
        as_session(monkeypatch, "cli")
        assert await cli._audited(build_parser().parse_args(_argv(cmd))) == {"ok": True}
    (row,) = rows(stubbed.j)
    assert stubbed.served == [row["id"]] and (row["actor"], row["status"]) == (f"cli:{cmd.cli}", "done")


async def test_a_web_console_write_goes_through_the_gateway(stubbed, monkeypatch):
    async with served(stubbed.j):
        as_session(monkeypatch, "web", None)
        out = await runner.run("move_stop", commands.by_name("move_stop"),
                               {"symbol": "QNT", "new_stop": 1.1, "reason": "tighten"})
    assert out["result"] == {"ok": True}
    (row,) = rows(stubbed.j)
    assert stubbed.served == [row["id"]] and (row["actor"], row["status"]) == ("web:move_stop", "done")


async def test_journal_only_writes_the_flag_off_and_the_guardian_never_reach_the_gateway(stubbed, monkeypatch):
    learn = commands.by_name("add_learning")
    async with served(stubbed.j):
        as_session(monkeypatch, "mcp")
        args = {n: _dummy(p) for n, p in learn.mcp_signature().parameters.items() if p.default is p.empty}
        assert await TOOLS["add_learning"](**args) == {"ok": True}
        stubbed.app.settings = replace(stubbed.app.settings, gateway=False)
        assert await TOOLS["close_position"]("QNT", "flag off") == {"ok": True}
        settings = flag_on(stubbed.app.settings)
        as_session(monkeypatch, "guardian")
        assert isinstance(gateway_client.for_session(lambda: stubbed.j, lambda: settings), LocalGateway)
        as_session(monkeypatch, "mcp")
        with audit.within("01RUNNING"):   # inside a running intent its steps run where it runs
            assert isinstance(gateway_client.for_session(lambda: stubbed.j, lambda: settings), LocalGateway)
        assert isinstance(gateway_client.for_session(lambda: stubbed.j, lambda: settings),
                          gateway_client.RemoteGateway)
    assert stubbed.served == [] and [r["status"] for r in rows(stubbed.j)] == ["done", "done"]


async def test_the_session_watchdogs_withdrawal_goes_through_the_gateway_with_its_desk(stubbed, monkeypatch):
    t = Trader(FakeEx(FakeCcxt()), stubbed.j, prechecks=ok_prechecks, budget=neutral_budget)
    t.ex.settings = flag_on(t.ex.settings)
    desk = EntryDesk(t)                                    # the gateway's own: its entry-row intents' ctx
    async with served(stubbed.j, ctx=desk):
        as_session(monkeypatch, "mcp")
        with audit.acting("mcp:trading_state"):
            assert await killswitch_ops.withdraw_resting(EntryDesk(t), "guardian heartbeat stale") == {"ok": True}
    (row,) = rows(stubbed.j)
    assert stubbed.served == [row["id"]] and stubbed.ran == [("withdraw_entries", desk)]
    assert (row["kind"], row["actor"]) == ("withdraw_entries", "mcp:trading_state")


async def test_a_real_open_and_close_through_the_gateway_link_orders_decisions_and_session(tmp_path, monkeypatch,
                                                                                          served_ids):
    t = Trader(FakeEx(FakeCcxt()), Journal(tmp_path / "j.db"), prechecks=ok_prechecks, budget=neutral_budget)
    t.ex.settings = flag_on(t.ex.settings)
    app = SimpleNamespace(journal=t.j, trader=t, settings=t.ex.settings, ex=t.ex)
    monkeypatch.setattr(cmd_trade, "get_app", lambda: app)
    async with served(t.j):
        as_session(monkeypatch, "mcp", "sess-9")
        res = await TOOLS["open_short"]("PUMP", 1.05, "pump_fade", "lower high", notional_usdt=100)
        assert res["ok"], res
        assert (await TOOLS["close_position"]("PUMP", "target hit", "faded as planned"))["ok"]
    opened, closed = rows(t.j)
    assert served_ids == [opened["id"], closed["id"]] and opened["trade_id"] == res["trade_id"]
    assert {o["kind"] for o in intents.orders(t.j, opened["id"])} == {"open", "stop"}
    assert [o["kind"] for o in intents.orders(t.j, closed["id"])] == ["close"]
    assert {d["intent_id"] for d in t.j.decisions()} == {opened["id"], closed["id"]}
    assert {d["claude_session"] for d in t.j.decisions()} == {"sess-9"} == {opened["claude_session"]}


async def test_a_write_command_that_sends_without_orders_true_fails_the_lock_audit(order_lock_audit, monkeypatch):
    """conftest's guard: an exchange write inside the intent of a write command not declared orders=True would
    bypass the gateway once the flag is on."""
    fake = FakeCcxt()

    async def sends(ctx, **args):
        try:
            await fake.cancel_order("1", "QNT/USDT:USDT")
        except Exception:  # noqa: BLE001 - the fake's answer doesn't matter here
            pass
        return {"ok": True}
    monkeypatch.setattr(gateway, "handler_for", lambda kind: sends)
    learn = commands.by_kind("add_learning")
    j = Journal(":memory:")
    await LocalGateway(lambda: j).submit(learn.intent({n: _dummy(p) for n, p in learn.signature.parameters.items()
                                                       if p.default is p.empty}))
    assert any("add_learning" in b and "orders=True" in b for b in order_lock_audit), order_lock_audit
    order_lock_audit.clear()
