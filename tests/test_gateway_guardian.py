"""The guardian as the gateway's client (#36 step 6, gateway_guardian.py): its intents go to the gateway; when the
gateway can't take one, every kind breaks glass at once (protection never waits on the gateway); a deploy's restart
is an alerts row, a real outage an urgent page; answers stay typed (busy, pending); its 30 s check stamps whether it
reaches the gateway (kv gateway_client:guardian, the deploy health check)."""

import asyncio
import json
import time
from dataclasses import replace

import pytest

from shortbot import audit, gateway, gateway_client, gateway_guardian, gateway_health, gateway_server
from shortbot import gateway_wire as wire
from shortbot.entries import EntryDesk
from shortbot.gateway import LocalGateway
from shortbot.gateway_acts import GuardianActs
from shortbot.gateway_guardian import GuardianClient, IntentPending, Link
from shortbot.intents import Intent
from shortbot.notify import Notifier
from shortbot.symlock import SymbolBusy
from tests.fakes import SYM, FakeCcxt
from tests.gateway_rig import served, short_dir
from tests.test_gateway_client import fake_gateway
from tests.test_guardian import guardian_with, open_trade


def client_guardian(fake=None):
    """A guardian with SHORTBOT_GATEWAY on: both its clients are the gateway's."""
    g = guardian_with(fake or FakeCcxt())
    on = replace(g.app.settings, gateway=True, gateway_guardian=True)
    g.app.settings = on
    g.gateway_link = Link()
    g.gw = GuardianClient(lambda: g.j, lambda: on, g, g.n, g.gateway_link)
    g.entries.gw = GuardianClient(lambda: g.j, lambda: on, g.entries, g.n, g.gateway_link)
    return g


def acts(g):
    return {"ctx": EntryDesk(g.app.trader), "acts": GuardianActs(g.app, Notifier(g.app.settings, g.j))}


@pytest.fixture
def nowhere(monkeypatch):
    """No gateway listens at the socket path."""
    path = short_dir() / "gateway-demo.sock"
    monkeypatch.setattr(wire, "socket_path", lambda s: path)
    return path


@pytest.fixture
def pages(monkeypatch):
    """Every alert Notifier.send delivered (past its cooldown): (kind, urgent)."""
    out, real = [], Notifier.send

    async def send(self, kind, message, symbol=None, cooldown_s=3600, urgent=False):
        if sent := await real(self, kind, message, symbol, cooldown_s, urgent):
            out.append((kind, urgent))
        return sent
    monkeypatch.setattr(Notifier, "send", send)
    return out


def breakglass_rows(g):
    return [a for a in g.j.alerts() if a["kind"] == "gateway_down_breakglass"]


def test_the_flag_picks_the_transport():
    g = guardian_with(FakeCcxt())
    assert isinstance(g.gw, LocalGateway) and isinstance(g.entries.gw, LocalGateway)   # `sessions` or off
    link = Link()
    on = replace(g.app.settings, gateway_guardian=True)
    gw = gateway_guardian.for_guardian(lambda: g.j, lambda: on, g, g.n, link)
    assert isinstance(gw, GuardianClient) and gw.link is link and gw.process() == "guardian"
    assert gw.accept_s(Intent("protect_fill", {})) == gateway_guardian.LATE_ACCEPT_S
    assert gw.accept_s(Intent("plan_step", {})) == gateway_client.ACCEPT_S


async def test_its_writes_go_through_the_gateway(monkeypatch):
    ran, real = [], gateway_server.GatewayServer.execute

    async def execute(self, it, *a, **k):
        ran.append((it.kind, it.actor))
        return await real(self, it, *a, **k)
    monkeypatch.setattr(gateway_server.GatewayServer, "execute", execute)
    fake = FakeCcxt()
    g = client_guardian(fake)
    await open_trade(g)
    fake.stops.clear()
    async with served(g.j, **acts(g)):
        with audit.acting("guardian:positions"):
            await g.check_positions()
    assert ran == [("restore_stop", "guardian:positions")] and list(fake.stops.values()) == [1.05]
    assert breakglass_rows(g) == []


@pytest.fixture
def stubs(monkeypatch):
    ran = []

    def handler_for(kind):
        async def stub(ctx, **args):
            ran.append((kind, type(ctx).__name__, audit.actor()))
            return {"ok": True}
        return stub
    monkeypatch.setattr(gateway, "handler_for", handler_for)
    return ran


async def test_down_every_kind_breaks_glass_at_once_with_its_ctx(nowhere, stubs):
    gateway.load_handlers()
    g = client_guardian()
    t0 = time.monotonic()
    for kind in sorted(gateway.HANDLERS):
        gw = g.gw if gateway.CTX[kind] == "guardian" else g.entries.gw
        with audit.acting("guardian:positions"):
            assert await gw.submit(Intent(kind, {}, SYM)) == {"ok": True}
    assert time.monotonic() - t0 < 2.0   # one refused connect, then the down memo: nobody waits
    want = [(k, "Guardian" if gateway.CTX[k] == "guardian" else "EntryDesk", "breakglass:guardian:positions")
            for k in sorted(gateway.HANDLERS)]
    assert stubs == want and len(want) == 13


async def test_a_deploy_restart_is_an_alerts_row_not_a_page(nowhere, stubs, pages):
    g = client_guardian()
    gateway_health.beat(g.j, {"at": time.time() - 3, "pid": 1})   # it beat 3 s ago: restarting
    with audit.acting("guardian:positions"):
        await g.gw.submit(Intent("restore_stop", {}, SYM))
        await g.gw.submit(Intent("restore_stop", {}, SYM))
    assert pages == [] and len(rows := breakglass_rows(g)) == 2 and "not urgent" in rows[0]["message"]
    assert [r["actor"] for r in g.j._rows("SELECT actor FROM intents")] == ["breakglass:guardian:positions"] * 2


@pytest.mark.parametrize("how", ["heartbeat", "unreachable"])
async def test_a_real_outage_pages_once_per_cooldown(nowhere, stubs, pages, how):
    g = client_guardian()
    if how == "heartbeat":   # this guardian just started; the gateway died long ago
        gateway_health.beat(g.j, {"at": time.time() - gateway_health.DOWN_URGENT_S - 5, "pid": 1})
    else:                    # it beats, but this guardian hasn't reached it for a minute
        gateway_health.beat(g.j, {"at": time.time() - 2, "pid": 1})
        g.gateway_link.down_since = time.time() - gateway_health.DOWN_URGENT_S - 1
    with audit.acting("guardian:positions"):
        for _ in range(3):
            await g.gw.submit(Intent("restore_stop", {}, SYM))
    assert pages == [("gateway_down_breakglass", True)] and "down" in breakglass_rows(g)[0]["message"]
    assert len(stubs) == 3   # protection ran every time


async def test_after_a_failure_it_does_not_try_again_for_a_while(nowhere, stubs, monkeypatch):
    tries, real = [], gateway_client.RemoteGateway._hello

    async def hello(path, mode):
        tries.append(path)
        return await real(path, mode)
    monkeypatch.setattr(gateway_client.RemoteGateway, "_hello", staticmethod(hello))
    g = client_guardian()
    for _ in range(3):
        await g.entries.gw.submit(Intent("cancel_entry", {}, SYM))
    assert len(tries) == 1 and len(stubs) == 3
    g.gateway_link.skip_until = 0.0   # DOWN_S passed
    await g.entries.gw.submit(Intent("cancel_entry", {}, SYM))
    assert len(tries) == 2


async def test_a_gateway_on_other_code_or_busy(nowhere, stubs):
    async def other(msg, w):
        w.write(wire.encode({"op": "hello", "ok": True, "v": 1, "mode": "demo"}))
    srv = await fake_gateway(nowhere, other)
    g = client_guardian()
    try:
        await g.gw.submit(Intent("restore_stop", {}, SYM))
    finally:
        srv.close()
    assert stubs[0][2].startswith("breakglass:")   # protocol v1: not this client's gateway

    async def busy(msg, w):
        if msg.get("op") == "hello":
            w.write(wire.encode({"op": "hello", "ok": True, "v": wire.PROTOCOL, "mode": "demo"}))
        else:
            w.write(wire.encode({"id": "x", "status": "busy", "error": "64 in flight"}))
    nowhere.unlink(missing_ok=True)
    srv = await fake_gateway(nowhere, busy)
    g = client_guardian()
    try:
        with pytest.raises(SymbolBusy, match="queues are full"):
            await g.gw.submit(Intent("restore_stop", {}, SYM))
    finally:
        srv.close()
    assert len(stubs) == 1   # busy: nothing ran anywhere


async def test_an_end_it_cannot_see_is_pending_never_a_guess(stubs, monkeypatch):
    gate = asyncio.Event()

    def handler_for(kind):
        async def slow(ctx, **args):
            await gate.wait()
            return {"ok": True}
        return slow
    monkeypatch.setattr(gateway, "handler_for", handler_for)
    monkeypatch.setattr(gateway_client, "FINAL_S", 0.2)
    g = client_guardian()
    async with served(g.j, **acts(g)) as srv:
        with pytest.raises(IntentPending, match="runs on in the gateway"):
            await g.gw.submit(Intent("plan_step", {}, SYM))
        gate.set()
        await asyncio.wait(set(srv.inflight), timeout=2)


async def test_its_check_stamps_whether_it_reaches_the_gateway(nowhere, pages):
    g = client_guardian()
    gateway_health.beat(g.j, {"at": time.time(), "pid": 1})

    def stamp():
        return json.loads(g.j.get(gateway_guardian.KEY))
    await g.check_gateway()
    assert stamp()["client"] is True and stamp()["ok"] is False and "gateway-demo.sock" in stamp()["why"]
    assert pages == []   # a fresh outage: not yet
    g.gateway_link.down_since = time.time() - gateway_health.DOWN_URGENT_S - 1
    await g.check_gateway()
    assert ("gateway_unreachable", True) in pages
    async with served(g.j, **acts(g)):
        await g.check_gateway()
    assert stamp()["ok"] is True and g.gateway_link.down_since is None
    local = guardian_with(FakeCcxt())
    await local.check_gateway()
    assert json.loads(local.j.get(gateway_guardian.KEY))["client"] is False


async def test_one_the_gateway_journalled_but_never_accepted_is_pending_not_run_twice(nowhere, stubs, monkeypatch):
    """A wedged gateway: it journalled and started the intent, then said nothing. Break-glass finds the row (the
    journal decides, gateway_client) and never runs it a second time: the guardian sees it pending."""
    from shortbot import intents
    monkeypatch.setattr(gateway_guardian, "LATE_ACCEPT_S", 0.2)
    g = client_guardian()

    async def wedged(msg, w):
        if msg.get("op") == "hello":
            w.write(wire.encode({"op": "hello", "ok": True, "v": wire.PROTOCOL, "mode": "demo"}))
        else:
            d = msg["intent"]
            it = Intent(d["kind"], d["args"], d["symbol"], id=d["id"], actor=d["actor"])
            intents.receive(g.j, it, None)
            intents.start(g.j, it.id)
    srv = await fake_gateway(nowhere, wedged)
    try:
        with pytest.raises(IntentPending):
            await g.entries.gw.submit(Intent("protect_fill", {}, SYM))
    finally:
        srv.close()
    assert stubs == []   # it ran nowhere here


async def test_an_old_gateway_that_does_not_serve_its_kinds_is_break_glass(nowhere, stubs):
    """Mixed code in a rolling restart: a gateway on step 4's code greets but refuses the guardian's kinds."""
    async def old(msg, w):
        if msg.get("op") == "hello":
            w.write(wire.encode({"op": "hello", "ok": True, "v": wire.PROTOCOL, "mode": "demo"}))
        else:
            w.write(wire.encode({"status": "refused", "ok": False, "error": "intent kind 'cap_stop' is not served"}))
    srv = await fake_gateway(nowhere, old)
    g = client_guardian()
    try:
        with audit.acting("guardian:positions"):
            assert await g.gw.submit(Intent("cap_stop", {}, SYM)) == {"ok": True}
    finally:
        srv.close()
    assert stubs == [("cap_stop", "Guardian", "breakglass:guardian:positions")]
