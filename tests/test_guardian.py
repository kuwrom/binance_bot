from types import SimpleNamespace

from shortbot.config import RiskLimits, Settings
from shortbot.guardian import Guardian
from shortbot.journal import Journal
from shortbot.notify import Notifier
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks

SETTINGS = Settings("demo", "", "", ":memory:", "", "", RiskLimits())


async def test_notifier_cooldown():
    n = Notifier(SETTINGS, Journal(":memory:"))
    assert await n.send("x", "first", "A")
    assert not await n.send("x", "again", "A")
    assert await n.send("x", "other coin", "B")
    assert await n.send("y", "zero cooldown", "A", cooldown_s=0)
    assert await n.send("y", "zero cooldown", "A", cooldown_s=0)


def guardian_with(fake: FakeCcxt) -> Guardian:
    ex = FakeEx(fake)
    j = Journal(":memory:")
    app = SimpleNamespace(settings=ex.settings, journal=j, ex=ex, trader=Trader(ex, j, prechecks=ok_prechecks, budget=neutral_budget))
    g = Guardian(app)

    async def quiet(*a, **k):
        return None
    g._squeeze_and_funding = quiet
    g._news = quiet
    return g


async def open_trade(g, **plan):
    r = await g.app.trader.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100,
                                      plan=plan or None)
    assert r["ok"], r
    return r["trade_id"]


def kinds(g):
    return [a["kind"] for a in g.j.alerts(100)]


async def test_restores_missing_stop():
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    fake.stops.clear()
    await g.check_positions()
    assert list(fake.stops.values()) == [1.05] and "stop_missing" in kinds(g)


async def test_forced_close_after_repeated_restore_failures():
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    fake.stops.clear()
    fake.fail_stop = 99
    for _ in range(3):
        await g.check_positions()
    assert fake.position == 0 and "forced_close" in kinds(g)


async def test_stop_out_on_exchange_is_reconciled_with_real_pnl():
    fake = FakeCcxt()
    g = guardian_with(fake)
    tid = await open_trade(g)
    fake.price = 1.05
    fake.trigger_stop()
    await g.check_positions()
    t = g.j.get_trade(tid)
    assert t["status"] == "closed" and t["pnl"] < -5 and "trade_closed" in kinds(g)
    assert g.j.stats()["by_setup"]["pump_fade"]["wins"] == 0   # losses reach the stats now


async def test_plan_breakeven_executed_and_excursions_tracked():
    fake = FakeCcxt()
    g = guardian_with(fake)
    tid = await open_trade(g)
    fake.price = 0.93
    await g.check_positions()
    assert list(fake.stops.values())[0] <= 1.005   # entry 1.0 is a round: breakeven clears just past its zone
    t = g.j.get_trade(tid)
    assert t["mfe_pct"] >= 7 and t["low_since_entry"] == 0.93 and "plan_stop_moved" in kinds(g)


async def test_one_bad_position_does_not_stop_others():
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)

    async def boom(*a, **k):
        raise RuntimeError("boom")
    g._run_plan = boom
    await g.check_positions()
    assert "position_check_error" in kinds(g)
