"""One start-up reconciler (#36 step 6, GATEWAY.md §5). Every deploy restarts the guardian and the gateway together.
The gateway's start-up reconciliation and the guardian's reconciling jobs (the positions pass, entry_reconcile) hold
one lease, a flock next to the journal (symlock.reconciling), so they never run at once: the gateway waits for a
guardian pass, the guardian skips a pass while the gateway's start-up runs, and whichever comes second finds nothing
left to do. Both started at once on one book: one sweep per dead intent, one trade_closed, one stop restore."""

import asyncio

from shortbot import audit, cmd_trade, intents, symlock
from shortbot.entries import EntryDesk
from shortbot.gateway_acts import GuardianActs
from shortbot.gateway_server import GatewayServer
from shortbot.notify import Notifier
from tests.fakes import SYM
from tests.gateway_rig import short_dir
from tests.test_crash_restart import STOP, Desk, dead_pids, dies, opened  # noqa: F401 - dead_pids: autouse here too
from tests.test_fault_matrix import cids, close_stops, resolutions


def leases(monkeypatch):
    """(who, entered, left) of every reconcile lease taken, in order."""
    out, real = [], symlock.SymbolLocks.reconciling
    loop = asyncio.get_running_loop()

    def reconciling(self, action="", wait_s=0.0):
        cm = real(self, action, wait_s)

        class Rec:
            async def __aenter__(self_):
                await cm.__aenter__()
                out.append([action, loop.time(), None])
                self_.row = out[-1]

            async def __aexit__(self_, *exc):
                self_.row[2] = loop.time()
                return await cm.__aexit__(*exc)
        return Rec()
    monkeypatch.setattr(symlock.SymbolLocks, "reconciling", reconciling)
    return out


def gateway_of(d: Desk, monkeypatch) -> GatewayServer:
    """A gateway process: its own View and journal connection."""
    gw = d.trader("gateway")
    app = type("App", (), {"settings": gw.ex.settings, "journal": gw.j, "ex": gw.ex, "trader": gw})()
    monkeypatch.setattr(cmd_trade, "get_app", lambda *a, **k: app)
    return GatewayServer(gw.j, "demo", short_dir() / "gateway-demo.sock", EntryDesk(gw),
                         GuardianActs(app, Notifier(gw.ex.settings, gw.j)))


async def both_start(d: Desk, monkeypatch):
    """The gateway and a guardian start at the same moment."""
    srv = gateway_of(d, monkeypatch)
    g = d.guardian()
    jobs = {job.name: job.fn for job in g._lanes()["protective"].jobs}

    async def guardian_round():
        for name in ("entries", "positions", "entry_reconcile"):   # the protective lane's first tick, in its order
            with audit.acting(f"guardian:{name}"):
                await jobs[name]()
    await asyncio.gather(srv.start(), guardian_round())
    await g.j.flush()
    await srv.j.flush()
    return srv, g


def alerts(j, kind):
    return [a for a in j.alerts(300) if a["kind"] == kind]


def apart(rows):
    """No two leases overlapped."""
    spans = sorted((a, b) for _, a, b in rows)
    return all(b is not None for _, b in spans) and all(spans[i][1] <= spans[i + 1][0] for i in range(len(spans) - 1))


async def test_a_naked_open_is_swept_and_protected_once(tmp_path, monkeypatch):
    d = Desk(tmp_path)
    s = d.trader("session")
    s.ex.fake.crash_after("create_order", "open")
    iid = await dies(s, monkeypatch, lambda: s.open_short(SYM, stop_price=STOP, setup="pump_fade", thesis="t",
                                                          notional=100))
    held = leases(monkeypatch)
    srv, g = await both_start(d, monkeypatch)
    try:
        assert apart(held) and {a for a, *_ in held} >= {"gateway start-up"}
        assert len(alerts(g.j, "intent_interrupted")) == 1 and resolutions(g.j) == ["interrupted -> partial"]
        assert intents.get(g.j, iid)["status"] == "partial"
        assert close_stops(d.fake) == [STOP] and len(cids(d.fake, "stop")) == 1   # one restore, by one of them
        assert len(alerts(g.j, "stop_missing")) == 1
    finally:
        await srv.close(1.0)


async def test_a_close_the_dead_session_made_is_journalled_once(tmp_path, monkeypatch):
    d = Desk(tmp_path)
    s = await opened(d)
    tid = s.j.open_trades()[0]["id"]
    s.ex.fake.crash_after("create_order", "close")
    iid = await dies(s, monkeypatch, lambda: s.close(SYM, "exit", "lesson"))
    held = leases(monkeypatch)
    srv, g = await both_start(d, monkeypatch)
    try:
        assert apart(held)
        assert g.j.get_trade(tid)["status"] == "closed"
        assert len([x for x in g.j.decisions(SYM, 100) if x["action"] == "trade_closed"]) == 1
        assert len(alerts(g.j, "trade_closed")) == 1 and len(alerts(g.j, "intent_interrupted")) == 1
        assert intents.get(g.j, iid)["status"] == "partial" and resolutions(g.j) == ["interrupted -> partial"]
    finally:
        await srv.close(1.0)


async def test_the_guardian_skips_a_pass_while_the_gateway_reconciles(tmp_path, monkeypatch):
    d = Desk(tmp_path)
    g = d.guardian()
    gw = d.trader("gateway")
    ran = []

    async def positions():
        ran.append("positions")
    monkeypatch.setattr(g, "check_positions", positions)
    jobs = {job.name: job.fn for job in g._lanes()["protective"].jobs}
    monkeypatch.setattr(symlock, "GUARDIAN_WAIT_S", 0.05)
    async with gw.symlocks.reconciling("gateway start-up"):
        await jobs["positions"]()          # skipped: no failure, no heartbeat
    assert ran == []
    await jobs["positions"]()
    assert ran == ["positions"]


async def test_the_gateway_waits_for_a_guardian_pass_then_finds_nothing_to_do(tmp_path, monkeypatch):
    d = Desk(tmp_path)
    s = d.trader("session")
    s.ex.fake.crash_after("create_order", "open")
    await dies(s, monkeypatch, lambda: s.open_short(SYM, stop_price=STOP, setup="pump_fade", thesis="t",
                                                   notional=100))
    g = d.guardian()
    gate, done, real = asyncio.Event(), asyncio.Event(), g.check_positions

    async def slow():   # the guardian's pass restores the stop, then lingers holding the lease
        await real()
        done.set()
        await gate.wait()
    monkeypatch.setattr(g, "check_positions", slow)
    jobs = {job.name: job.fn for job in g._lanes()["protective"].jobs}

    async def positions():
        with audit.acting("guardian:positions"):
            await jobs["positions"]()
    pass_ = asyncio.create_task(positions())
    await asyncio.wait_for(done.wait(), 5)
    assert close_stops(d.fake) == [STOP]
    srv = gateway_of(d, monkeypatch)
    start = asyncio.create_task(srv.start())
    await asyncio.sleep(0.3)
    assert not start.done() and not srv.path.exists()   # it waits for the lease before it listens
    gate.set()
    await pass_
    await start
    try:
        await g.j.flush()
        assert srv.path.exists() and len(cids(d.fake, "stop")) == 1 and len(alerts(g.j, "stop_missing")) == 1
    finally:
        await srv.close(1.0)
