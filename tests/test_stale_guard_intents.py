"""D5 (#36 step 5): with the order gateway on, the stale guard for a write is the protocol check. A stale MCP server's
order write goes to the gateway and runs on the code on disk; the gateway refuses an intent built for other code (its
signature, gateway.signature_of); and nothing ever runs on stale code in the client: not a journal-only write, not
break-glass. A current client facing a gateway on other code treats it as unavailable (D3: break-glass for the
protective set, new risk refused)."""

from types import SimpleNamespace

import pytest

from shortbot import audit, cmd_desk, cmd_kill, cmd_research, cmd_trade, gateway_client, procs
from shortbot import gateway_wire as wire
from shortbot.config import Settings
from shortbot.journal import Journal
from shortbot.mcp_server import TOOLS
from tests.gateway_rig import as_session, served, short_dir
from tests.test_stale_guard import NEW, _call_rows, _disk_is, _never


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """An MCP server (process kind mcp) with SHORTBOT_GATEWAY on; the trader's calls recorded."""
    monkeypatch.setattr(audit, "_seen", {})
    as_session(monkeypatch)
    j = Journal(tmp_path / "j.db")
    sent = []

    async def move_stop(*a):
        sent.append(("move_stop", audit.actor()))
        return {"ok": True, "stop": a[1]}
    settings = Settings("demo", "", "", tmp_path / "j.db", "", "", gateway=True)
    app = SimpleNamespace(journal=j, settings=settings, ex=SimpleNamespace(experiment=None),
                          trader=SimpleNamespace(move_stop=move_stop, open_short=_never))
    for mod in (cmd_trade, cmd_research, cmd_desk, cmd_kill):
        monkeypatch.setattr(mod, "get_app", lambda *a, **k: app)
    monkeypatch.setattr(gateway_client, "CONNECT_S", 0.05)
    monkeypatch.setattr(gateway_client, "CONNECT_RETRY_S", 0.1)
    return SimpleNamespace(j=j, sent=sent)


def other_code(monkeypatch):
    """This client builds intents for other code than the gateway runs (a changed signature)."""
    real = wire.intent_dict
    monkeypatch.setattr(wire, "intent_dict", lambda it: {**real(it), "sig": "0" * 16})


def down(monkeypatch):
    monkeypatch.setattr(wire, "socket_path", lambda s: short_dir() / "gateway-demo.sock")


async def test_a_stale_servers_order_write_runs_on_the_gateway(desk, monkeypatch):
    _disk_is(monkeypatch, NEW)
    async with served(desk.j):
        res = await TOOLS["move_stop"]("QNT", 1.1, "trail")
    assert res == {"ok": True, "stop": 1.1}   # no stale_code warning: the gateway ran it on its code
    assert desk.sent == [("move_stop", "mcp:move_stop")]
    assert [(r["name"], r["outcome"], r["actor"]) for r in _call_rows(desk.j)] == [("move_stop", "done", "mcp:move_stop")]


async def test_an_intent_built_for_other_code_is_refused_there_and_journalled(desk, monkeypatch):
    _disk_is(monkeypatch, NEW)
    other_code(monkeypatch)
    async with served(desk.j):
        res = await TOOLS["move_stop"]("QNT", 1.1, "trail")
    assert res["ok"] is False and res["stale_code"] is True and "shortbot move-stop" in res["error"]
    assert "built for other code" in res["gateway"] and desk.sent == []
    (row,) = _call_rows(desk.j)
    assert (row["name"], row["outcome"]) == ("move_stop", "refused")


async def test_a_current_client_and_a_gateway_on_other_code_is_break_glass(desk, monkeypatch):
    """The client runs the code on disk, so the gateway is the stale side: unavailable to it (D3)."""
    other_code(monkeypatch)
    async with served(desk.j):
        res = await TOOLS["move_stop"]("QNT", 1.1, "trail")
        assert res == {"ok": True, "stop": 1.1} and desk.sent == [("move_stop", "breakglass:mcp:move_stop")]
        res = await TOOLS["open_short"]("QNT", 1.2, "pump_fade", "turned")
    assert res["ok"] is False and res["gateway"] == "unavailable" and "built for other code" in res["error"]


async def test_another_protocol_and_stale_code_is_refused(desk, monkeypatch):
    _disk_is(monkeypatch, NEW)
    async with served(desk.j) as srv:
        monkeypatch.setattr(srv, "_mismatch", lambda hello: "protocol v1 is not this gateway's v2")
        res = await TOOLS["move_stop"]("QNT", 1.1, "trail")
    assert res["stale_code"] is True and "protocol v1" in res["gateway"] and desk.sent == []


async def test_down_and_stale_is_refused_never_break_glass(desk, monkeypatch):
    _disk_is(monkeypatch, NEW)
    down(monkeypatch)
    res = await TOOLS["move_stop"]("QNT", 1.1, "trail")
    assert res["ok"] is False and res["stale_code"] is True and desk.sent == []
    assert not [a for a in desk.j.alerts() if a["kind"] == "gateway_down_breakglass"]


async def test_down_and_current_is_break_glass(desk, monkeypatch):
    down(monkeypatch)
    res = await TOOLS["move_stop"]("QNT", 1.1, "trail")
    assert res == {"ok": True, "stop": 1.1} and desk.sent == [("move_stop", "breakglass:mcp:move_stop")]


async def test_a_halt_is_stale_exempt_and_still_breaks_glass_when_down(desk, monkeypatch):
    halted = []

    async def halt(tr, state, by, why):
        halted.append((state, audit.actor()))
        return {"ok": True, "state": state}
    monkeypatch.setattr(cmd_kill.killswitch_ops, "halt", halt)
    _disk_is(monkeypatch, NEW)
    down(monkeypatch)
    res = await TOOLS["halt_trading"]("squeeze")
    assert res["ok"] is True and halted == [("HALTED", "breakglass:mcp:halt_trading")]


async def test_journal_only_writes_still_refuse_stale_code(desk, monkeypatch):
    """They run in this process whatever the flag says: the stale guard still refuses them (#68)."""
    _disk_is(monkeypatch, NEW)
    async with served(desk.j):
        res = await TOOLS["reset_risk_params"]("back to baseline")
    assert res["ok"] is False and res["stale_code"] is True and "shortbot reset-params" in res["error"]
    assert procs.code_version()[:12] in res["error"]


def test_the_signature_covers_names_and_defaults(monkeypatch):
    from shortbot import gateway

    async def v1(ctx, symbol: str, pct: float = 10.0): ...

    async def v2(ctx, symbol: str, pct: float = 12.0): ...

    async def v3(ctx, sym: str, pct: float = 10.0): ...
    sigs = []
    for fn in (v1, v1, v2, v3):
        monkeypatch.setitem(gateway.HANDLERS, "t_sig", fn)
        sigs.append(gateway.signature_of("t_sig"))
    assert sigs[0] == sigs[1] and len(set(sigs)) == 3
    assert gateway.signature_of("restore_stop") and gateway.signature_of("move_stop") != gateway.signature_of("close_position")
