"""When the order gateway can't take an intent (#36 step 4, docs/GATEWAY.md §3.4, D3): risk-adding intents fail closed
and protective ones (killswitch.ALLOWED, halts, the watchdog's withdrawal) run in this process under the flock as
break-glass, journalled as such and alerted. The journal decides every doubt: the same intent id is claimed once,
so a gateway that took the intent before it died is never run again here."""

import asyncio
import contextlib
import os
from types import SimpleNamespace

import pytest

from shortbot import audit, commands, gateway, gateway_client, intents
from shortbot import gateway_wire as wire
from shortbot.config import Settings
from shortbot.journal import Journal
from shortbot.notify import Notifier
from tests.gateway_rig import short_dir
from tests.test_gateway_server import close, opening

commands.load()


@pytest.fixture
def down(tmp_path, monkeypatch):
    """A session whose gateway socket is `path` (nothing listens there unless a test serves it)."""
    j = Journal(tmp_path / "j.db")
    ran = []

    def handler_for(kind):
        async def stub(ctx, **args):
            ran.append((kind, audit.actor()))
            return {"ok": True}
        return stub
    monkeypatch.setattr(gateway, "handler_for", handler_for)
    sent, real_send = [], Notifier.send

    async def send(self, kind, message, symbol=None, cooldown_s=3600, urgent=False):
        sent.append((kind, urgent))
        return await real_send(self, kind, message, symbol, cooldown_s, urgent)
    monkeypatch.setattr(Notifier, "send", send)
    monkeypatch.setattr(gateway_client, "CONNECT_S", 0.05)
    monkeypatch.setattr(gateway_client, "CONNECT_RETRY_S", 0.15)
    monkeypatch.setattr(gateway_client, "ACCEPT_S", 0.2)
    settings = Settings("demo", "", "", tmp_path / "j.db", "", "", gateway=True)
    path = short_dir() / "gateway-demo.sock"
    monkeypatch.setattr(wire, "socket_path", lambda s: path)
    return SimpleNamespace(j=j, ran=ran, path=path, sent=sent,
                           remote=gateway_client.RemoteGateway(lambda: j, lambda: settings))


def alerts(j):
    return [a for a in j.alerts() if a["kind"] == "gateway_down_breakglass"]


async def fake_gateway(path, on_line, ended=None):
    """A listener that speaks as much of the protocol as a test needs: on_line(msg, writer) per line it reads;
    `ended` (an Event) is set once a connection was handled to its end."""
    async def conn(r, w):
        try:
            while line := await r.readline():
                if await on_line(wire.json_line(line), w) is False:
                    break
        finally:
            w.close()
            with contextlib.suppress(ConnectionError):
                await w.wait_closed()
            if ended is not None:
                ended.set()
    return await asyncio.start_unix_server(conn, path=str(path))


def test_break_glass_is_exactly_the_protective_set():
    assert gateway_client.BREAKGLASS == {"close_position", "move_stop", "cancel_entry_group", "flatten_all",
                                         "halt_trading", "kill_switch", "withdraw_entries"}
    assert gateway_client.BREAKGLASS - {"withdraw_entries"} <= {c.kind for c in commands.load() if c.orders}


async def test_with_no_gateway_new_risk_fails_closed_and_the_refusal_is_journalled(down):
    it = opening()
    res = await down.remote.submit(it)
    assert not res["ok"] and res["gateway"] == "unavailable" and "nothing sent" in res["error"]
    assert "open_short" in res["error"] and down.ran == [] and not alerts(down.j)
    row = intents.get(down.j, it.id)
    assert (row["status"], row["actor"], row["sent"]) == ("refused", "mcp:open_short", 0)


async def test_with_no_gateway_a_close_runs_here_as_break_glass_and_is_alerted(down):
    it = close()
    assert await down.remote.submit(it) == {"ok": True}
    assert down.ran == [("close_position", "breakglass:mcp:close_position")]
    row = intents.get(down.j, it.id)
    assert (row["status"], row["actor"], row["pid"]) == ("done", "breakglass:mcp:close_position", os.getpid())
    (a,) = alerts(down.j)
    assert "close_position" in a["message"] and it.id in a["message"] and "unavailable" in a["message"]
    assert down.sent == [("gateway_down_breakglass", True)]


async def test_a_gateway_that_never_says_hello_is_unavailable(down):
    async def silent(msg, w):
        return None
    srv = await fake_gateway(down.path, silent)
    async with srv:
        assert (await down.remote.submit(opening()))["gateway"] == "unavailable"
        assert await down.remote.submit(close()) == {"ok": True} and len(alerts(down.j)) == 1


@pytest.mark.parametrize("hello, why", [({"v": wire.PROTOCOL + 1, "mode": "demo"}, "protocol"),
                                        ({"v": wire.PROTOCOL, "mode": "live"}, "mode")])
async def test_a_gateway_of_another_protocol_or_mode_is_unavailable(down, hello, why):
    async def answer(msg, w):
        w.write(wire.encode({"op": "hello", "ok": True, **hello}))
    srv = await fake_gateway(down.path, answer)
    async with srv:
        res = await down.remote.submit(opening())
        assert res["gateway"] == "unavailable" and why in res["error"]
        assert await down.remote.submit(close()) == {"ok": True}


async def test_a_listener_of_another_uid_is_never_sent_an_intent(down, monkeypatch):
    seen = []

    async def answer(msg, w):
        seen.append(msg)
        w.write(wire.encode({"op": "hello", "ok": True, "v": wire.PROTOCOL, "mode": "demo"}))
    ended = asyncio.Event()
    srv = await fake_gateway(down.path, answer, ended)
    monkeypatch.setattr(wire, "peer_uid", lambda sock: os.geteuid() + 1)
    async with srv:
        res = await down.remote.submit(opening())
        assert res["gateway"] == "unavailable" and f"uid {os.geteuid() + 1}" in res["error"]
        await asyncio.wait_for(ended.wait(), 2)   # it hung up at once
    assert seen == []


@pytest.mark.parametrize("make", [close, opening], ids=["break-glass", "fail-closed"])
async def test_a_gateway_that_took_the_intent_and_died_is_never_run_twice(down, make):
    """It journalled (claimed) the intent, then the connection dropped: break-glass and the fail-closed refusal both
    find the row under the same id and answer it (still running: never resend), running nothing here."""
    it = make()

    async def took_it(msg, w):
        if msg["op"] == "hello":
            w.write(wire.encode({"op": "hello", "ok": True, "v": wire.PROTOCOL, "mode": "demo"}))
            return None
        intents.receive(down.j, it)
        intents.start(down.j, it.id)
        return False                                              # dies: the connection drops, no reply
    srv = await fake_gateway(down.path, took_it)
    async with srv:
        res = await down.remote.submit(it)
    assert res["in_progress"] and res["unknown"] and res["intent_id"] == it.id
    assert down.ran == [] and intents.get(down.j, it.id)["status"] == "running" and not alerts(down.j)


async def test_status_reads_the_journal_when_the_gateway_is_down(down):
    it = close()
    await down.remote.submit(it)
    assert await down.remote.status(it.id) == {"ok": True}
    assert await down.remote.status("01NOPE") is None
