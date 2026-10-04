"""The gateway killed outright mid-intent (kill -9: its connection drops, its queued journal writes are lost, nothing of
it runs on) at each dangerous point, then a NEW gateway on its own client and journal connection (#36 step 6,
GATEWAY.md §5): its start-up reconciliation, before it listens, must reach what the guardian's start-up reached
in-process (test_crash_restart.py): the dead intent swept and resolved once, never re-sent; exactly one closePosition
stop and no bridge; a trade closed by the dead run journalled closed once; each alerted. The session that sent it never
resends: it gets "in progress", then the stored outcome. Crash points: a stop swap with its bridge placed, an open's
market SELL filled before its stop, a close filled before its journal write."""

import os
from types import SimpleNamespace

import pytest

from shortbot import audit, cmd_trade, commands, fill_quality, gateway_client, intents
from shortbot.config import Settings
from shortbot.entries import EntryDesk
from shortbot.gateway_acts import GuardianActs
from shortbot.gateway_server import GatewayServer
from shortbot.journal import Journal
from shortbot.notify import Notifier
from shortbot.trading import Trader
from tests.fake_faults import Crash, FaultyCcxt, View
from tests.fakes import SYM, FakeEx, neutral_budget, ok_prechecks
from tests.gateway_rig import as_session, short_dir
from tests.test_fault_matrix import bridges, cids, close_stops, resolutions

STOP = 1.05
SETTINGS = Settings("demo", "", "", ":memory:", "", "", gateway=True, gateway_guardian=True)


class Host:
    """One exchange and journal file; each process (session, gateway A, gateway B) its own View and connection."""

    def __init__(self, tmp_path, monkeypatch):
        self.fake, self.db, self.mp = FaultyCcxt(price=1.0), tmp_path / "shortbot.db", monkeypatch
        self.path = short_dir() / "gateway-demo.sock"
        monkeypatch.setattr("shortbot.gateway_wire.socket_path", lambda s: self.path)
        monkeypatch.setattr(intents, "_alive", lambda pid: False)   # the killed gateway shares this test's pid

    def app(self, name: str) -> SimpleNamespace:
        j = Journal(self.db)
        tr = Trader(FakeEx(View(self.fake, name).kills(j)), j, prechecks=ok_prechecks, budget=neutral_budget)
        return SimpleNamespace(settings=SETTINGS, journal=j, ex=tr.ex, trader=tr)

    async def gateway(self, name: str) -> GatewayServer:
        """A gateway process: the commands' bodies find its app (get_app), as in shortbot-gateway; it starts with its
        start-up reconciliation."""
        app = self.app(name)
        self.mp.setattr(cmd_trade, "get_app", lambda *a, **k: app)
        srv = GatewayServer(app.journal, "demo", self.path, EntryDesk(app.trader),
                            GuardianActs(app, Notifier(SETTINGS, app.journal)))
        srv.app = app
        await srv.start()
        return srv


def kill(srv: GatewayServer) -> None:
    """kill -9: no drain, no more answers; the kernel drops its socket and its flock."""
    if srv._lock is None:
        return   # dead already
    srv._server.close()
    for t in (*srv.inflight, srv._beats):
        t.cancel()
    srv.path.unlink(missing_ok=True)
    os.close(srv._lock)
    srv._lock = None


async def sent_by_a_session(h: Host, srv: GatewayServer, kind: str, **args):
    """A session's command through the gateway, which dies inside it; the session's answer and the intent id."""
    as_session(h.mp)
    with audit.acting(f"mcp:{kind}"):
        it = commands.by_kind(kind).intent(args)
    real = type(srv).execute.__get__(srv)

    async def execute(*a, **k):
        try:
            return await real(*a, **k)
        except Crash:
            kill(srv)
            raise
    srv.execute = execute
    h.mp.setattr(gateway_client, "CONNECT_RETRY_S", 0.1)
    s = h.app("session")
    res = await gateway_client.RemoteGateway(lambda: s.journal, lambda: SETTINGS).submit(it)
    return res, it.id


def alerts(j, kind):
    return [a["message"] for a in j.alerts(300) if a["kind"] == kind]


async def opened(h: Host) -> GatewayServer:
    """Gateway A, after a session opened a short through it."""
    srv = await h.gateway("gateway A")
    as_session(h.mp)
    res, _ = await sent_by_a_session(h, srv, "open_short", symbol=SYM, stop_price=STOP, setup="pump_fade",
                                     thesis="t", notional_usdt=100)
    assert res["ok"], res
    await fill_quality.drain(5)
    return srv


async def test_killed_mid_stop_swap_then_a_new_gateway_reconciles_at_start(tmp_path, monkeypatch):
    h = Host(tmp_path, monkeypatch)
    a = await opened(h)
    h.fake.price = 0.99
    a.app.ex.fake.crash_after("create_order", "bridge")
    res, iid = await sent_by_a_session(h, a, "move_stop", symbol=SYM, new_stop=1.03, reason="trail")
    assert res.get("in_progress") and res.get("unknown")                       # never resent: in progress
    assert close_stops(h.fake) == [STOP] and len(bridges(h.fake)) == 1        # covered twice, never naked
    b = await h.gateway("gateway B")                                          # reconciled before it listens
    j = b.app.journal
    assert close_stops(h.fake) == [STOP] and bridges(h.fake) == [] and h.fake.position == 100
    assert intents.get(j, iid)["status"] == "partial" and resolutions(j) == ["interrupted -> partial"]
    assert len(alerts(j, "intent_interrupted")) == 1   # the bridge: swept by the orphan sweep (§5: entries first)
    assert alerts(j, "orphan_stop_cancelled") or alerts(j, "bridge_swept")
    assert len(cids(h.fake, "bridge")) == 1 and len(cids(h.fake, "stop")) == 1   # nothing sent again
    answer = await gateway_client.RemoteGateway(lambda: j, lambda: SETTINGS).status(iid)
    assert answer["status"] == "partial" and answer["resolved"]
    await b.close(1.0)


async def test_killed_after_the_open_filled_before_its_stop(tmp_path, monkeypatch):
    h = Host(tmp_path, monkeypatch)
    a = await h.gateway("gateway A")
    a.app.ex.fake.crash_after("create_order", "open")
    res, iid = await sent_by_a_session(h, a, "open_short", symbol=SYM, stop_price=STOP, setup="pump_fade",
                                       thesis="t", notional_usdt=100)
    assert res.get("in_progress") and h.fake.position == 100 and h.fake.stops == {}   # naked
    b = await h.gateway("gateway B")
    j = b.app.journal
    assert close_stops(h.fake) == [STOP] and alerts(j, "stop_missing")      # protected at its start
    assert intents.get(j, iid)["status"] == "partial" and resolutions(j) == ["interrupted -> partial"]
    assert len(cids(h.fake, "open")) == 1 and len(alerts(j, "intent_interrupted")) == 1
    assert j.open_trades(include_opening=True)[0]["status"] == "opening"    # its row: the guardian's grace decides
    await b.close(1.0)


async def test_killed_after_the_close_filled_before_its_journal_write(tmp_path, monkeypatch):
    h = Host(tmp_path, monkeypatch)
    a = await opened(h)
    tid = a.app.journal.open_trades()[0]["id"]
    a.app.ex.fake.crash_after("create_order", "close")
    res, iid = await sent_by_a_session(h, a, "close_position", symbol=SYM, reason="exit", lesson="lesson")
    assert res.get("in_progress") and h.fake.position == 0
    b = await h.gateway("gateway B")
    j = b.app.journal
    tr = j.get_trade(tid)
    assert tr["status"] == "closed" and tr["exit_price"] == 1.0
    assert tr["pnl"] == pytest.approx(sum(float(i["income"]) for i in h.fake.income))
    assert len([x for x in j.decisions(SYM, 100) if x["action"] == "trade_closed"]) == 1
    assert len(alerts(j, "trade_closed")) == 1
    assert intents.get(j, iid)["status"] == "partial" and resolutions(j) == ["interrupted -> partial"]
    assert len(cids(h.fake, "close")) == 1 and close_stops(h.fake) == []
    await b.close(1.0)

