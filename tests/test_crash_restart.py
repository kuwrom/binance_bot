"""#46 plumbing gate, GATEWAY.md §8 step 6 in-process: a process killed outright (kill -9: no handler, no finally
that matters, its queued journal writes lost) at each dangerous point of the order path, then a NEW guardian on its
own client and its own journal connection (tests/fake_faults.View, one shared book and journal file) runs its
start-up jobs (the protective lane's: the fill poll, the positions pass, entry_reconcile) and must reach:
- the dead intent swept (interrupted) and, when it sent an order under a client id, resolved once, never re-sent
- exactly one closePosition stop on every position, every bridge and orphaned pre-arm swept
- a trade closed by the dead process journalled closed once, from trade history, with exchange-truth PnL
- the heartbeat stamped only by a pass that verified every position's stop

Crash points: (1) an open's market SELL filled, before its stop; (2) a stop swap with its bridge placed, the old stop
not yet cancelled; (3) a close filled, before the journal write; (4) a limit entry filled, before protect_fill (and
inside it, after the whole-position stop); (5) a HALTED withdrawal, after its first cancel ran."""

from types import SimpleNamespace

import pytest

from shortbot import audit, fill_quality, gateway, intents, reconcile
from shortbot import killswitch as ks
from shortbot.guardian import Guardian
from shortbot.intents import Intent
from shortbot.journal import Journal
from shortbot.trading import Trader
from tests.fake_faults import Crash, FaultyCcxt, View
from tests.fakes import SYM, FakeEx, neutral_budget, ok_prechecks
from tests.test_entries import favorable, prop
from tests.test_fault_matrix import bridges, cids, close_stops, resolutions

STOP = 1.05


class Desk:
    """One shared exchange and journal file; each process gets its own View and Journal connection."""

    def __init__(self, tmp_path, price: float = 1.0):
        self.fake, self.db = FaultyCcxt(price=price), tmp_path / "shortbot.db"

    def trader(self, name: str) -> Trader:
        j = Journal(self.db)
        return Trader(FakeEx(View(self.fake, name).kills(j)), j, prechecks=ok_prechecks, budget=neutral_budget)

    def guardian(self, name: str = "guardian") -> Guardian:
        tr = self.trader(name)
        g = Guardian(SimpleNamespace(settings=tr.ex.settings, journal=tr.j, ex=tr.ex, trader=tr))

        async def quiet(*a, **k):
            return None
        g._squeeze_and_funding = g._news = quiet
        g.entries._regime_fn = favorable
        g.stamps = []   # each heartbeat: closePosition stops per held symbol at that moment
        real = g._heartbeat

        def heartbeat(at):
            g.stamps.append({s: len(close_stops(self.fake, s)) for s, b in self.fake.sym.items() if b["qty"]})
            real(at)
        g._heartbeat = heartbeat
        return g


async def dies(tr: Trader, monkeypatch, flow, kind: str = "t_dies") -> str:
    """`flow` runs as an intent of `tr`'s process until that process is killed; returns the intent's id."""
    async def run(ctx):
        return await flow()
    monkeypatch.setitem(gateway.HANDLERS, kind, run)
    it = Intent(kind, {}, SYM)
    with pytest.raises(Crash):
        await gateway.LocalGateway(lambda: tr.j).submit(it)
    return it.id


async def startup(g: Guardian, rounds: int = 2) -> None:
    """The protective lane's jobs as a (re)started guardian runs them, in its order."""
    for _ in range(rounds):
        for job, fn in (("entries", g.check_entries), ("positions", g.check_positions),
                        ("entry_reconcile", g.reconcile_entries)):
            with audit.acting(f"guardian:{job}"):
                await fn()
    await g.j.flush()


def alerts(g: Guardian, kind: str) -> list[str]:
    return [a["message"] for a in g.j.alerts(300) if a["kind"] == kind]


def verified(g: Guardian, *held: str) -> None:
    """The heartbeat was stamped, and only while every held position had exactly one closePosition stop."""
    assert g.stamps and g.j.get(ks.HEARTBEAT_KEY)
    assert all(set(s) == set(held) and all(n == 1 for n in s.values()) for s in g.stamps), g.stamps


@pytest.fixture(autouse=True)
def dead_pids(monkeypatch):
    """The killed process shares this test's pid: the sweep must judge it gone."""
    monkeypatch.setattr(intents, "_alive", lambda pid: False)


async def opened(d: Desk) -> Trader:
    s = d.trader("session")
    r = await s.open_short(SYM, stop_price=STOP, setup="pump_fade", thesis="t", notional=100)
    assert r["ok"], r
    await fill_quality.drain(5)   # its background references are done before the process dies
    return s


# ------------------------------------------------------------ (1) an open filled, before its stop
async def test_killed_after_the_open_filled_before_its_stop(tmp_path, monkeypatch):
    d = Desk(tmp_path)
    s = d.trader("session")
    s.ex.fake.crash_after("create_order", "open")
    iid = await dies(s, monkeypatch, lambda: s.open_short(SYM, stop_price=STOP, setup="pump_fade", thesis="t",
                                                          notional=100))
    assert d.fake.position == 100 and d.fake.stops == {}                    # naked on the exchange
    g = d.guardian()
    assert intents.get(g.j, iid)["status"] == "running" and g.j.open_trades(include_opening=True)[0]["status"] == \
        "opening"
    await startup(g, rounds=1)                                                # protected before the opening grace
    assert close_stops(d.fake) == [STOP] and g.j.open_trades(include_opening=True)[0]["status"] == "opening"
    monkeypatch.setattr(reconcile, "OPENING_GRACE_S", -1)
    await startup(g)
    (tr,) = g.j.open_trades()
    assert tr["status"] == "open" and tr["amount"] == 100 and close_stops(d.fake) == [STOP] and bridges(d.fake) == []
    assert intents.get(g.j, iid)["status"] == "done" and resolutions(g.j) == ["interrupted -> done"]
    assert len(cids(d.fake, "open")) == 1 and len(alerts(g, "intent_interrupted")) == 1
    verified(g, SYM)


# ------------------------------------------------------------ (2) a stop swap, its bridge placed
async def test_killed_mid_stop_swap_with_the_bridge_placed_and_the_old_stop_still_resting(tmp_path, monkeypatch):
    d = Desk(tmp_path)
    s = await opened(d)
    d.fake.price = 0.99                                                       # short of the plan's breakeven
    s.ex.fake.crash_after("create_order", "bridge")
    iid = await dies(s, monkeypatch, lambda: s.move_stop(SYM, 1.03, "trail"))
    assert close_stops(d.fake) == [STOP] and len(bridges(d.fake)) == 1     # covered twice, never naked
    g = d.guardian()
    await startup(g)
    assert close_stops(d.fake) == [STOP] and bridges(d.fake) == [] and d.fake.prearms == {} and d.fake.position == 100
    assert g.j.open_trades()[0]["stop_price"] == STOP and alerts(g, "bridge_swept")   # the journal's stop stands
    assert intents.get(g.j, iid)["status"] == "done" and resolutions(g.j) == ["interrupted -> done"]
    assert len(cids(d.fake, "bridge")) == 1 and len(cids(d.fake, "stop")) == 1
    verified(g, SYM)


# ------------------------------------------------------------ (3) a close filled, before the journal write
async def test_killed_after_the_close_filled_before_the_journal_write(tmp_path, monkeypatch):
    d = Desk(tmp_path)
    s = await opened(d)
    tid = s.j.open_trades()[0]["id"]
    s.ex.fake.crash_after("create_order", "close")
    iid = await dies(s, monkeypatch, lambda: s.close(SYM, "exit", "lesson"))
    assert d.fake.position == 0 and d.fake.stops == {}                      # the stop went with the position
    g = d.guardian()
    assert g.j.get_trade(tid)["status"] == "open"
    await startup(g)
    tr = g.j.get_trade(tid)
    assert tr["status"] == "closed" and tr["exit_price"] == 1.0
    assert tr["pnl"] == pytest.approx(sum(float(i["income"]) for i in d.fake.income))
    assert len([x for x in g.j.decisions(SYM, 100) if x["action"] == "trade_closed"]) == 1
    assert intents.get(g.j, iid)["status"] == "done" and resolutions(g.j) == ["interrupted -> done"]
    assert len(cids(d.fake, "close")) == 1 and d.fake.position == 0 and close_stops(d.fake) == []
    verified(g)


# ------------------------------------------------------------ (4) a limit entry filled, before / inside protect_fill
@pytest.mark.parametrize("point", ["before_protect", "after_its_stop"])
async def test_killed_after_a_limit_fill(point, tmp_path, monkeypatch):
    d = Desk(tmp_path, price=1.035)
    old = d.guardian("old guardian")
    (p,) = (await old.entries.propose_group([prop(1.04)]))["placed"]
    e = old.entries._rows("id=?", (p["entry_id"],))[0]
    d.fake.fill_limit(e["order_id"])                                          # its pre-arm alone protects it now
    if point == "before_protect":
        old.ex.fake.crash_after("fetch_order", "entry")                        # the poll saw the fill, then died
    else:
        old.ex.fake.crash_after("create_order", "stop")                        # its whole-position stop rests
    with pytest.raises(Crash), audit.acting("guardian:entries"):
        await old.check_entries()
    assert d.fake.position == p["amount"] and not old.j.open_trades()
    g = d.guardian()
    await startup(g)
    (tr,) = g.j.open_trades()
    assert tr["amount"] == p["amount"] and close_stops(d.fake) == [1.10] and d.fake.prearms == {}
    assert g.entries._rows("id=?", (e["id"],))[0]["status"] == "filled" and len(alerts(g, "action_fill")) == 1
    assert len(cids(d.fake, "stop")) == 1 and len(cids(d.fake, "entry")) == 1
    dead = [r for r in g.j._rows("SELECT * FROM intents WHERE kind='protect_fill' ORDER BY rowid")]
    if point == "after_its_stop":   # the dead protect_fill sent the stop: resolved by its id, not sent again
        assert dead[0]["status"] == "done" and resolutions(g.j) == ["interrupted -> done"]
    else:
        assert resolutions(g.j) == []
    verified(g, SYM)


# ------------------------------------------------------------ (5) a HALTED withdrawal, after its first cancel
async def halted_crash(tmp_path) -> tuple[Desk, Guardian]:
    d = Desk(tmp_path, price=1.035)
    old = d.guardian("old guardian")
    r = await old.entries.propose_group([prop(1.04), prop(1.06, stop=1.12)])
    assert len(r["placed"]) == 2 and len(d.fake.prearms) == 2
    await ks.trip(old.j, old.app.settings, ks.HALTED, "agent (MCP)", "halt")
    old.ex.fake.crash_after("cancel_order", "entry")                           # the first limit's cancel ran
    with pytest.raises(Crash), audit.acting("guardian:entries"):
        await old.check_entries()
    assert len([o for o in d.fake.limits.values() if o["status"] == "open"]) == 1 and len(d.fake.prearms) == 2
    g = d.guardian()
    await startup(g)
    return d, g


async def test_killed_mid_halted_withdrawal(tmp_path):
    d, g = await halted_crash(tmp_path)
    assert [o["status"] for o in d.fake.limits.values()] == ["canceled", "canceled"] and d.fake.prearms == {}
    assert {x["status"] for x in g.entries._rows()} == {"cancelled"} and ks.read(g.j)["state"] == ks.HALTED
    assert d.fake.position == 0 and d.fake.stops == {} and resolutions(g.j) == []
    assert len(cids(d.fake, "entry")) == 2 and len(cids(d.fake, "prearm")) == 2
    verified(g)


async def test_the_interrupted_alert_names_the_cancel_a_withdrawal_sent_before_it_died(tmp_path):
    _, g = await halted_crash(tmp_path)
    (swept,) = [a for a in g.j.alerts(300) if a["kind"] == "intent_interrupted"]
    (first,) = [c["order_id"] for c in g.j._rows("SELECT * FROM intent_changes WHERE op='cancel'")
                if c["intent_id"] in swept["message"]]   # the one cancel the dead withdrawal sent
    assert "withdraw_entries" in swept["message"] and f"cancel {first}" in swept["message"]
    assert "sent nothing" not in swept["message"] and "not linked" not in swept["message"]
