"""Kill switch in the guardian: HALTED sweep, fills while not ACTIVE, auto-trips (daily loss, drawdown, long,
untracked), the stale-heartbeat auto-clear, the loosening tripwire, and hooks that never fail the protective pass."""

import json
import time

from shortbot import killswitch as ks
from shortbot.entries import EntryDesk
from shortbot.guardian_positions import UNTRACKED_GRACE_S
from shortbot.killswitch import ACTIVE, HALTED, REDUCING, STALE
from tests.fakes import SYM, FakeCcxt
from tests.test_guardian import SETTINGS, guardian_with, kinds
from tests.test_killswitch import favorable, prop


def guardian():
    fake = FakeCcxt()
    g = guardian_with(fake)
    g.j.set(ks.HEARTBEAT_KEY, str(time.time()))
    return g, fake


async def two_entries(g, fake):
    await EntryDesk(g.app.trader, regime_fn=favorable).propose_group([prop(1.04), prop(1.06, stop=1.12)])
    return {o["price"]: i for i, o in fake.limits.items()}


async def test_halted_guardian_withdraws_resting_entries_and_still_protects_a_fill():
    g, fake = guardian()
    oids = await two_entries(g, fake)
    fake.fill_limit(oids[1.06], qty=10)
    await ks.trip(g.j, SETTINGS, HALTED, "agent (MCP)", "halt")   # tripped by a process that couldn't cancel
    await g.check_entries()
    assert fake.limits[oids[1.04]]["status"] == "canceled" and g.entries.pending() == []
    assert list(fake.stops.values()) == [1.12] and fake.position == 10 and fake.prearms == {}
    assert {"kill_switch_state", "kill_switch_sweep", "fill_while_halted"} <= set(kinds(g))


async def test_reducing_leaves_entries_resting_and_a_fill_is_protected_and_alerted():
    g, fake = guardian()
    oids = await two_entries(g, fake)
    await ks.trip(g.j, SETTINGS, REDUCING, "guardian", "daily loss")
    await g.check_entries()
    assert len(g.entries.pending()) == 2 and "kill_switch_sweep" not in kinds(g)
    fake.fill_limit(oids[1.04])
    await g.check_entries()
    assert list(fake.stops.values()) == [1.10] and "fill_while_reducing" in kinds(g)
    assert ks.read(g.j)["state"] == REDUCING   # never auto-closed, never cleared


async def test_daily_loss_breach_trips_reducing_and_records_the_limit_used():
    g, fake = guardian()
    fake.income.append({"symbol": "PUMPUSDT", "incomeType": "REALIZED_PNL", "income": "-400", "asset": "USDT",
                        "time": int(time.time() * 1000)})   # an earlier trade's loss today
    await g.check_positions()
    st = ks.read(g.j)
    assert st["state"] == REDUCING and st["cause"] == "daily_loss" and ".env baseline" in st["reason"]


async def test_untracked_position_trips_after_the_grace_unless_one_of_our_limits_rests(monkeypatch):
    g, fake = guardian()
    fake.position, fake.entry, fake.stops["s1"] = 50, 1.0, 1.1   # opened in the Binance app, stop set there
    await g.check_positions()
    assert ks.read(g.j)["state"] == ACTIVE and SYM in g.untracked_since
    g.untracked_since[SYM] -= UNTRACKED_GRACE_S
    monkeypatch.setattr(g.entries, "pending", lambda group_id=None: [{"symbol": SYM}])   # a fill in flight
    await g.check_positions()
    assert ks.read(g.j)["state"] == ACTIVE and SYM not in g.untracked_since
    monkeypatch.undo()
    await g.check_positions()
    g.untracked_since[SYM] -= UNTRACKED_GRACE_S
    await g.check_positions()
    st = ks.read(g.j)
    assert st["state"] == REDUCING and st["cause"] == "reconciliation" and list(fake.stops.values()) == [1.1]


async def test_a_long_position_trips_reducing():
    g, fake = guardian()

    async def longs():
        return [{"symbol": SYM, "contracts": 5, "side": "long", "entryPrice": 1.0, "markPrice": 1.0, "notional": 5,
                 "unrealizedPnl": 0, "liquidationPrice": 0.5}]
    fake.fetch_positions = longs
    await g.check_positions()
    assert ks.read(g.j)["cause"] == "long_position" and "unexpected_long" in kinds(g)


async def test_governor_drawdown_stop_trips_reducing(monkeypatch):
    g, fake = guardian()

    async def regime(ex):
        return {"verdict_for_shorts": "caution"}

    async def gov(tr, reg):
        return {"halt": True, "until": None, "reasons": ["drawdown 12.5% >= 12.0%"], "drawdown_pct": 12.5}
    monkeypatch.setattr("shortbot.guardian_jobs.market_regime", regime)
    monkeypatch.setattr("shortbot.guardian_jobs.governor_status", gov)
    await g.check_governor()
    assert ks.read(g.j)["cause"] == "drawdown" and "governor_halt" in kinds(g)


async def test_guardian_lifts_a_stale_halt_and_flags_a_clear_that_bypassed_admin():
    g, fake = guardian()
    await ks.trip(g.j, SETTINGS, HALTED, "session watchdog", "heartbeat 12 min old", cause=STALE)
    await g.check_entries()
    assert ks.read(g.j)["state"] == HALTED and "kill_switch_state" in kinds(g)   # 1st fresh check
    await g.check_entries()
    assert ks.read(g.j)["state"] == ACTIVE and g.j.alerts(1)[0]["kind"] == "kill_switch_loosened"
    await ks.trip(g.j, SETTINGS, HALTED, "agent (MCP)", "manual halt")
    await g.check_entries()
    g.j.set(ks.KV_KEY, json.dumps({"state": ACTIVE, "by": "agent", "reason": "sneaky", "at": "now"}))
    await g.check_entries()
    a = g.j.alerts(1)[0]
    assert a["kind"] == "kill_switch_loosened" and "NOT via shortbot-admin" in a["message"]


async def test_a_stale_halt_lifting_back_to_a_sticky_trip_is_not_a_tamper_alarm():
    g, fake = guardian()
    await ks.trip_on_daily_loss(g.j, SETTINGS, 9_600, -400)
    await ks.trip(g.j, SETTINGS, HALTED, "session watchdog", "laptop slept", cause=STALE)
    for _ in range(2):
        await g.check_entries()
    a = g.j.alerts(1)[0]
    assert ks.read(g.j)["cause"] == "daily_loss" and a["kind"] == "kill_switch_loosened"
    assert "NOT via shortbot-admin" not in a["message"]


async def test_kill_switch_hooks_never_fail_the_protective_pass(monkeypatch):
    g, fake = guardian()
    oids = await two_entries(g, fake)

    async def broken(*a, **k):
        raise RuntimeError("database is locked")
    for name in ("auto_clear_stale", "trip_on_daily_loss", "trip"):
        monkeypatch.setattr(ks, name, broken)
    fake.fill_limit(oids[1.04])
    await g.check_entries()                                   # the fill is still protected
    assert list(fake.stops.values()) == [1.10]
    fake.stops.clear()                                        # and a missing stop is still restored
    await g.check_positions()
    assert list(fake.stops.values()) == [1.10] and "stop_missing" in kinds(g)
    assert "guardian_error:kill_switch" in kinds(g)
