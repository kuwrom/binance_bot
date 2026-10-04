"""The gateway serves the guardian (#36 step 6): every @handler kind from a guardian client, each with its own ctx (the
gateway's EntryDesk, or GuardianActs), run for the guardian (origin.py); the sessions' kinds stay the sessions'. An
intent's exception reaches its caller typed, as it would have in-process."""

from types import SimpleNamespace

import pytest

from shortbot import audit, errors, gateway, gateway_client, intents, origin
from shortbot import gateway_wire as wire
from shortbot.commands import CommandError
from shortbot.config import Settings
from shortbot.entries import EntryDesk
from shortbot.entries_fills import StopNotPlaced
from shortbot.gateway_acts import GuardianActs
from shortbot.gateway_server import Refused
from shortbot.intents import Intent
from shortbot.journal import Journal
from shortbot.notify import Notifier
from shortbot.symlock import SymbolBusy
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks
from tests.gateway_rig import as_session, served
from tests.test_gateway_server import connect


@pytest.fixture
def book(tmp_path):
    """One fake account and journal file; the gateway's own Trader, EntryDesk and GuardianActs on it."""
    fake, j = FakeCcxt(), Journal(tmp_path / "j.db")
    tr = Trader(FakeEx(fake), j, prechecks=ok_prechecks, budget=neutral_budget)
    settings = Settings("demo", "", "", tmp_path / "j.db", "", "", gateway=True, gateway_guardian=True)
    app = SimpleNamespace(settings=settings, journal=j, ex=tr.ex, trader=tr)
    return SimpleNamespace(fake=fake, j=j, tr=tr, settings=settings, desk=EntryDesk(tr),
                           acts=GuardianActs(app, Notifier(settings, j)),
                           remote=lambda: gateway_client.RemoteGateway(lambda: j, lambda: settings))


async def test_a_guardian_restore_stop_runs_in_the_gateway_as_the_guardian(book, monkeypatch):
    r = await book.tr.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100)
    assert r["ok"], r
    as_session(monkeypatch, "guardian", None)
    async with served(book.j, book.desk, acts=book.acts):   # its start-up found the stop resting
        book.fake.stops.clear()
        with audit.acting("guardian:positions"):
            it = Intent("restore_stop", {"symbol": SYM, "fallback_pct": 10.0}, SYM)
        res = await book.remote().submit(it)
    assert res["action"] and list(book.fake.stops.values()) == [1.05]
    row = intents.get(book.j, it.id)
    assert (row["status"], row["actor"]) == ("done", "guardian:positions")


@pytest.fixture
def stubs(monkeypatch):
    """Every handler kind a stub recording its ctx and whether it ran for the guardian."""
    ran = []

    def handler_for(kind):
        async def stub(ctx, **args):
            ran.append((kind, type(ctx).__name__, origin.is_guardian()))
            if name := args.get("raise"):
                raise RAISED[name]()
            return {"ok": True}
        return stub
    monkeypatch.setattr(gateway, "handler_for", handler_for)
    return ran


async def test_each_kind_gets_its_ctx_and_the_guardians_run_as_the_guardian(book, stubs, monkeypatch):
    gateway.load_handlers()
    async with served(book.j, book.desk, acts=book.acts):
        as_session(monkeypatch, "guardian", None)
        for kind in sorted(gateway.HANDLERS):
            await book.remote().submit(Intent(kind, {}, SYM))
        as_session(monkeypatch, "mcp")
        await book.remote().submit(Intent("withdraw_entries", {}))
    want = {k: "GuardianActs" if gateway.CTX[k] == "guardian" else "EntryDesk" for k in gateway.HANDLERS}
    assert {k: c for k, c, _ in stubs[:-1]} == want and len(want) == 13
    assert all(g for _, _, g in stubs[:-1]) and stubs[-1] == ("withdraw_entries", "EntryDesk", False)


@pytest.mark.parametrize("process, kind", [("mcp", "restore_stop"), ("guardian", "close_position")])
async def test_each_client_only_sends_its_own_kinds(book, stubs, monkeypatch, process, kind):
    as_session(monkeypatch, process)
    async with served(book.j, book.desk, acts=book.acts) as srv:
        r, w, _ = await connect(srv.path)
        args = {"symbol": "QNT", "reason": "r"} if kind == "close_position" else {}
        it = Intent(kind, args, "QNT/USDT:USDT")
        w.write(wire.encode({"op": "submit", "intent": wire.intent_dict(it), "origin": {"process": process}}))
        no = await wire.read(r, 2.0)
        w.close()
    assert no["status"] == "refused" and "not served" in no["error"] and stubs == []


async def test_without_guardian_acts_its_kinds_are_refused(book, stubs, monkeypatch):
    async with served(book.j, book.desk) as srv:
        assert srv.parse(wire.intent_dict(Intent("protect_fill", {})), "guardian").kind == "protect_fill"
        with pytest.raises(Refused, match="no guardian context"):
            srv.parse(wire.intent_dict(Intent("restore_stop", {})), "guardian")


RAISED = {
    "busy": lambda: SymbolBusy(SYM, 5, "pid 7 (session close)"),
    "naked": lambda: StopNotPlaced({"id": 4, "stop_price": 1.1}, RuntimeError("x")),
    "gone": lambda: errors.Gone(None, "-2011 gone"),
    "command": lambda: CommandError("no QNT position"),
    "other": lambda: ValueError("bad"),
}


@pytest.mark.parametrize("name, check", [
    ("busy", lambda e: isinstance(e, SymbolBusy) and e.symbol == SYM and e.holder == "pid 7 (session close)"),
    ("naked", lambda e: isinstance(e, StopNotPlaced)),
    ("gone", lambda e: isinstance(e, errors.Gone) and e.sent is False),
    ("command", lambda e: isinstance(e, CommandError)),
    ("other", lambda e: isinstance(e, wire.RemoteError) and e.kind == "ValueError"),
])
async def test_an_exception_reaches_the_caller_typed(book, stubs, monkeypatch, name, check):
    as_session(monkeypatch, "guardian", None)
    async with served(book.j, book.desk, acts=book.acts):
        with pytest.raises(Exception) as e:
            await book.remote().submit(Intent("restore_stop", {"raise": name}, SYM))
    want = RAISED[name]()
    assert check(e.value), e.value
    assert str(e.value) == (str(want) if name != "other" else "ValueError: bad")
