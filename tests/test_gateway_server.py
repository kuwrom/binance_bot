"""The shortbot-gateway server (#36 step 4, docs/GATEWAY.md §3): over a real Unix socket, a peer of another uid or
another protocol version or mode is refused before anything is read; an intent is journalled on receipt, answered
`accepted` then final, and runs once per id whatever the connections do; a full queue answers busy (nothing
received); risk-adding intents queue on the #74 account lock inside the gateway and against any other writer."""

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from shortbot import audit, commands, gateway, gateway_client, gateway_server, intents
from shortbot import gateway_wire as wire
from shortbot.commands import CommandError
from shortbot.config import Settings
from shortbot.journal import Journal
from shortbot.symlock import AccountBusy, SymbolLocks, lock_dir_for
from tests.fakes import FakeCcxt, FakeEx, neutral_budget, ok_prechecks
from tests.gateway_rig import served

commands.load()


def close(sym="QNT", actor="mcp:close_position"):
    with audit.acting(actor):
        return commands.by_kind("close_position").intent({"symbol": sym, "reason": "target hit"})


def opening(sym="QNT", actor="mcp:open_short"):
    with audit.acting(actor):
        return commands.by_kind("open_short").intent({"symbol": sym, "stop_price": 1.1, "setup": "pump_fade",
                                                       "thesis": "lower high"})


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """Stub handlers (the wire is under test, not the bodies); `gate[symbol]` holds a run on it until set."""
    j = Journal(tmp_path / "j.db")
    ran, gate = [], {}

    def handler_for(kind):
        async def stub(ctx, **args):
            ran.append((kind, args))
            if args.get("symbol") in gate:
                await gate[args["symbol"]].wait()
            if args.get("reason") == "boom":
                raise CommandError("no QNT position")
            return {"ok": True, "kind": kind}
        return stub
    monkeypatch.setattr(gateway, "handler_for", handler_for)
    settings = Settings("demo", "", "", tmp_path / "j.db", "", "", gateway=True)
    monkeypatch.setattr(audit, "_proc", {"kind": "mcp", "claude_session": None})
    return SimpleNamespace(j=j, ran=ran, gate=gate, settings=settings,
                           remote=lambda: gateway_client.RemoteGateway(lambda: j, lambda: settings))


async def connect(path, mode="demo", v=None):
    r, w = await asyncio.open_unix_connection(str(path), limit=wire.LIMIT)
    w.write(wire.encode({"v": wire.PROTOCOL if v is None else v, "op": "hello", "mode": mode}))
    return r, w, await wire.read(r, 2.0)


def submit(w, it, session="sess-client"):
    w.write(wire.encode({"op": "submit", "intent": wire.intent_dict(it), "origin": {"claude_session": session}}))


async def until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.005)


# ------------------------------------------------------------ the handshake
async def test_hello_answers_the_version_and_mode_and_refuses_another(desk):
    async with served(desk.j) as srv:
        _, _, hello = await connect(srv.path)
        assert hello["ok"] and hello["v"] == wire.PROTOCOL and hello["mode"] == "demo" and hello["pid"] == os.getpid()
        for kw, why in (({"v": wire.PROTOCOL + 1}, "protocol"), ({"mode": "live"}, "mode")):
            r, _, no = await connect(srv.path, **kw)
            assert not no["ok"] and no["status"] == "refused" and why in no["error"]
            assert await r.read() == b""                          # and the connection is closed
    assert desk.j._rows("SELECT * FROM intents") == []


async def test_a_peer_of_another_uid_is_refused_before_anything_is_read(desk, monkeypatch):
    async with served(desk.j) as srv:
        monkeypatch.setattr(wire, "peer_uid", lambda sock: os.geteuid() + 1)
        r, w = await asyncio.open_unix_connection(str(srv.path))
        no = await wire.read(r, 2.0)
        assert no["status"] == "refused" and f"uid {os.geteuid() + 1}" in no["error"]
        assert await r.read() == b""
        w.close()
    assert desk.ran == [] and desk.j._rows("SELECT * FROM intents") == []


async def test_only_one_gateway_serves_a_mode(desk):
    async with served(desk.j) as srv:
        second = gateway_server.GatewayServer(desk.j, "demo", srv.path, None)
        with pytest.raises(RuntimeError, match="another gateway"):
            await second.start()
        assert (await connect(srv.path))[2]["ok"]                 # the first one still serves


# ------------------------------------------------------------ submit, status
async def test_a_submit_is_accepted_then_answered_and_journalled_with_the_callers_origin(desk):
    async with served(desk.j) as srv:
        r, w, _ = await connect(srv.path)
        it = close()
        submit(w, it)
        assert await wire.read(r, 2.0) == {"id": it.id, "status": "accepted"}
        assert await wire.read(r, 2.0) == {"id": it.id, "status": "final", "result": {"ok": True,
                                                                                     "kind": "close_position"}}
    row = intents.get(desk.j, it.id)
    assert (row["status"], row["actor"], row["claude_session"], row["kind"]) == (
        "done", "mcp:close_position", "sess-client", "close_position")
    assert desk.ran == [("close_position", it.args)]


async def test_an_error_reaches_the_caller_as_in_process_and_the_row_keeps_it(desk):
    async with served(desk.j):
        with audit.acting("mcp:close_position"):
            it = commands.by_kind("close_position").intent({"symbol": "QNT", "reason": "boom"})
        with pytest.raises(CommandError, match="no QNT position"):
            await desk.remote().submit(it)
    row = intents.get(desk.j, it.id)   # nothing was sent: refused, its error typed (results.py)
    assert row["status"] == "refused" and "no QNT position" in row["error"]["message"]


@pytest.mark.parametrize("raw, why", [
    ({"id": "X1", "kind": "add_learning", "args": {}, "actor": "mcp:add_learning"}, "not served"),
    ({"id": "X2", "kind": "close_position", "args": {"symbol": "QNT"}, "actor": "mcp:close_position"}, "reason"),
    ({"id": "", "kind": "close_position", "args": {"symbol": "QNT", "reason": "r"}, "actor": "x"}, "id"),
])
async def test_a_malformed_or_unserved_intent_is_refused_and_never_journalled(desk, raw, why):
    async with served(desk.j) as srv:
        r, w, _ = await connect(srv.path)
        w.write(wire.encode({"op": "submit", "intent": raw, "origin": {}}))
        no = await wire.read(r, 2.0)
        assert no["status"] == "refused" and why in no["error"]
    assert desk.ran == [] and desk.j._rows("SELECT * FROM intents") == []


async def test_a_resubmitted_id_runs_once_across_reconnects_and_a_disconnect_never_cancels_it(desk):
    desk.gate["QNT"] = asyncio.Event()
    async with served(desk.j) as srv:
        it = close()
        r, w, _ = await connect(srv.path)
        submit(w, it)
        assert (await wire.read(r, 2.0))["status"] == "accepted"
        w.close()                                                 # the caller goes away mid-run
        r2, w2, _ = await connect(srv.path)
        submit(w2, it)                                            # the same id again, on a new connection
        again = await wire.read(r2, 2.0)
        assert again["status"] == "final" and again["result"]["in_progress"] and again["result"]["unknown"]
        desk.gate["QNT"].set()
        await until(lambda: intents.get(desk.j, it.id)["status"] == "done")
        assert await desk.remote().status(it.id) == {"ok": True, "kind": "close_position"}
        assert await desk.remote().submit(it) == {"ok": True, "kind": "close_position"}
        assert await desk.remote().status("01NOPE") is None
    assert desk.ran == [("close_position", it.args)]


async def test_a_full_queue_answers_busy_and_nothing_is_received(desk, monkeypatch):
    monkeypatch.setattr(gateway_server, "BOUND_SYMBOL", 1)
    desk.gate["QNT"] = asyncio.Event()
    async with served(desk.j):
        first, second = close(), close()
        running = asyncio.create_task(desk.remote().submit(first))
        await until(lambda: desk.ran)
        busy = await desk.remote().submit(second)
        assert not busy["ok"] and busy["busy"] and "nothing sent" in busy["error"]
        assert (await desk.remote().submit(close("BTC")))["ok"]   # another symbol has room
        desk.gate["QNT"].set()
        assert (await running)["ok"]
    assert intents.get(desk.j, second.id)["status"] == "refused"   # the caller journalled why: never received
    assert [a["symbol"] for _, a in desk.ran] == ["QNT", "BTC"]


async def test_a_caller_whose_wait_ends_mid_run_gets_pending_then_the_result_by_status(desk, monkeypatch):
    monkeypatch.setattr(gateway_client, "FINAL_S", 0.05)
    desk.gate["QNT"] = asyncio.Event()
    async with served(desk.j):
        it = close()
        res = await desk.remote().submit(it)
        assert not res["ok"] and res["in_progress"] and res["intent_id"] == it.id
        desk.gate["QNT"].set()
        await until(lambda: intents.get(desk.j, it.id)["status"] == "done")
        assert await desk.remote().status(it.id) == {"ok": True, "kind": "close_position"}


async def test_a_heartbeat_names_the_gateway_and_its_load(desk, monkeypatch):
    from shortbot import gateway_health
    monkeypatch.setattr(gateway_server, "HEARTBEAT_S", 0.01)
    desk.gate["QNT"] = asyncio.Event()
    async with served(desk.j):
        running = asyncio.create_task(desk.remote().submit(close()))
        await until(lambda: (gateway_health.read(desk.j) or {}).get("running") == 1)
        hb = gateway_health.read(desk.j)
        assert hb["pid"] == os.getpid() and hb["v"] == wire.PROTOCOL and time.time() - hb["at"] < 5
        desk.gate["QNT"].set()
        await running


# ------------------------------------------------------------ global admission (#74 account lock, kept: GATEWAY §8)
async def test_risk_adding_intents_queue_on_the_account_lock_inside_and_against_other_writers(desk, monkeypatch):
    from shortbot.trading import Trader
    t = Trader(FakeEx(FakeCcxt()), desk.j, prechecks=ok_prechecks, budget=neutral_budget)
    inside, peak, release, holding = [0], [0], asyncio.Event(), asyncio.Event()

    def handler_for(kind):
        async def stub(ctx, **args):
            async with t.account_lock(f"open short {args['symbol']}"):
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
                holding.set()
                await release.wait()
                inside[0] -= 1
            return {"ok": True}
        return stub
    monkeypatch.setattr(gateway, "handler_for", handler_for)
    async with served(desk.j):
        a = asyncio.create_task(desk.remote().submit(opening("QNT")))
        b = asyncio.create_task(desk.remote().submit(opening("BTC")))
        await holding.wait()
        other = SymbolLocks(lock_dir_for(desk.j.db), wait_s=0.05)   # a flag-off CLI: another open file description
        with pytest.raises(AccountBusy):
            async with other.account("open short ETH"):
                pass
        release.set()
        assert (await a)["ok"] and (await b)["ok"] and peak[0] == 1
