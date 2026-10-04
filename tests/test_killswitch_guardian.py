"""Kill switch in the guardian: HALTED sweep, fills while not ACTIVE, auto-trips (daily loss on equity marks, drawdown,
long, untracked), the stale-heartbeat auto-clear, the loosening tripwire (across restarts too), and hooks that never
fail the protective pass."""

import json
import time
from datetime import UTC, datetime

import pytest

from shortbot import equity_marks as em
from shortbot import guardian_positions as gp
from shortbot import killswitch as ks
from shortbot.entries import EntryDesk
from shortbot.guardian_loop import guarded
from shortbot.guardian_positions import UNTRACKED_GRACE_S
from shortbot.journal import now_iso
from shortbot.killswitch import ACTIVE, HALTED, REDUCING, STALE
from tests.fakes import SYM, FakeCcxt
from tests.test_guardian import SETTINGS, guardian_with, kinds
from tests.test_killswitch import favorable, prop


def guardian():
    fake = FakeCcxt()
    g = guardian_with(fake)
    g.j.set(ks.HEARTBEAT_KEY, str(time.time()))
    return g, fake


def protective_pass(g, i):
    """What the protective lane writes after a good pass (Guardian._heartbeat), one distinct value per pass."""
    g.j.set(ks.HEARTBEAT_KEY, str(time.time() - 10 + i))


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


async def test_every_fill_while_reducing_is_alerted_including_a_late_oco_sibling():
    g, fake = guardian()
    fake.price = 1.035
    await two_entries(g, fake)
    await ks.trip(g.j, SETTINGS, REDUCING, "guardian", "daily loss")
    fake.price = 1.07                                       # a spike through both levels between two polls
    await g.check_entries()
    assert kinds(g).count("fill_while_reducing") == 2 and list(fake.stops.values()) == [1.10]


def day_mark(g, equity, late=False):
    """Today's equity mark, as the first guardian pass after 00:00 UTC (or a late one) recorded it."""
    key, start = em.period_starts(datetime.now(UTC))["day"]
    g.j.set(f"eq_mark:day:{key}", json.dumps({"equity": equity, "ts": start + (3_600_000 if late else 60_000)}))


def realized_today(fake, usdt):   # an earlier trade's result today: in the old day P&L (income since 00:00)
    fake.income.append({"symbol": "PUMPUSDT", "incomeType": "REALIZED_PNL", "income": str(usdt), "asset": "USDT",
                        "time": int(time.time() * 1000)})


async def test_daily_loss_breach_trips_reducing_and_records_the_limit_used():
    g, fake = guardian()
    day_mark(g, fake.equity + 400)                          # equity is 400 below today's mark
    realized_today(fake, -400)
    await g.check_positions()
    st = ks.read(g.j)
    assert st["state"] == REDUCING and st["cause"] == "daily_loss" and ".env baseline" in st["reason"]
    ev = json.loads(g.j.decisions()[0]["snapshot"])["evidence"]
    assert ev["day_pnl"] == -400 and ev["old_day_pnl"] == -400 and not ev["late"]["day"] and not ev["degraded"]


async def test_a_loss_carried_over_midnight_no_longer_trips_the_daily_loss():
    g, fake = guardian()
    day_mark(g, fake.equity)                                # no change since 00:00
    realized_today(fake, -400)                              # the old definition: -4% "today"
    await g.check_positions()
    assert ks.read(g.j)["state"] == ACTIVE


async def test_a_late_or_unreadable_mark_falls_back_to_the_old_day_pnl(monkeypatch):
    g, fake = guardian()
    day_mark(g, fake.equity, late=True)                     # the guardian slept across midnight
    realized_today(fake, -400)
    await g.check_positions()
    assert ks.read(g.j)["cause"] == "daily_loss"
    g2, fake2 = guardian()
    day_mark(g2, fake2.equity)
    realized_today(fake2, -400)

    def locked(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(em, "_mark", locked)                # kv unreadable: equity_marks.period_pnl degrades
    await g2.check_positions()
    assert ks.read(g2.j)["cause"] == "daily_loss"
    assert json.loads(g2.j.decisions()[0]["snapshot"])["evidence"]["degraded"]


@pytest.mark.parametrize("explained_by", ["pending", "placing", "unknown", "external", "external_filled"])
async def test_untracked_position_trips_after_the_grace_unless_one_of_our_limits_rests(explained_by):
    g, fake = guardian()
    fake.position, fake.entry, fake.stops["s1"] = 50, 1.0, 1.1   # opened in the Binance app, stop set there
    fake.sym[SYM]["lev"] = 5   # liquidation ~1.19: at the fake's 20x (~1.04) the guardian would tighten that stop (#64)
    await g.check_positions()
    assert ks.read(g.j)["state"] == ACTIVE and SYM in g.untracked_since
    g.untracked_since[SYM] -= UNTRACKED_GRACE_S
    eid = g.j._insert("INSERT INTO entries (group_id, symbol, status, created_at, expires_ms) VALUES ('g', ?, ?, ?, 0)",
                      (SYM, explained_by, now_iso()))   # a fill the poll / reconciler is about to journal
    await g.check_positions()
    assert ks.read(g.j)["state"] == ACTIVE and SYM not in g.untracked_since
    g.j.db.execute("UPDATE entries SET status='cancelled' WHERE id=?", (eid,))
    g.j.db.commit()
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

    async def gov(tr, reg, limits=None):
        return {"halt": True, "until": None, "reasons": ["drawdown 12.5% >= 12.0%"], "drawdown_pct": 12.5}
    monkeypatch.setattr("shortbot.guardian_jobs.market_regime", regime)
    monkeypatch.setattr("shortbot.guardian_jobs.governor_status", gov)
    await g.check_governor()
    assert ks.read(g.j)["cause"] == "drawdown" and "governor_halt" in kinds(g)


async def test_guardian_lifts_a_stale_halt_and_flags_a_clear_that_bypassed_admin():
    g, fake = guardian()
    await ks.trip(g.j, SETTINGS, HALTED, "session watchdog", "heartbeat 12 min old", cause=STALE)
    protective_pass(g, 1)
    await g.check_entries()
    await g.check_entries()                                   # no new pass in between: no count
    assert ks.read(g.j)["state"] == HALTED and "kill_switch_state" in kinds(g)
    protective_pass(g, 2)
    await g.check_entries()
    assert ks.read(g.j)["state"] == ACTIVE and g.j.alerts(1)[0]["kind"] == "kill_switch_loosened"
    await ks.trip(g.j, SETTINGS, HALTED, "agent (MCP)", "manual halt")
    await g.check_entries()
    g.j.set(ks.KV_KEY, json.dumps({"state": ACTIVE, "by": "agent", "reason": "sneaky", "at": "now"}))
    await g.check_entries()
    a = g.j.alerts(1)[0]
    assert a["kind"] == "kill_switch_loosened" and "NOT via shortbot-admin" in a["message"]


async def test_a_loosening_while_the_guardian_was_down_is_announced_when_it_starts():
    g, fake = guardian()
    await ks.trip(g.j, SETTINGS, REDUCING, "guardian", "daily loss")
    await g.check_entries()                                   # seen (and persisted) as REDUCING
    g.kill_seen = None                                        # the guardian restarts...
    g.j.set(ks.KV_KEY, json.dumps({"state": ACTIVE, "by": "agent", "reason": "sneaky", "at": "now"}))   # ...late
    await g.check_entries()
    a = g.j.alerts(1)[0]
    assert a["kind"] == "kill_switch_loosened" and "NOT via shortbot-admin" in a["message"]
    assert "guardian was down" in a["message"]
    await ks.trip(g.j, SETTINGS, REDUCING, "guardian", "drawdown")
    await g.check_entries()
    g.kill_seen = None
    await ks.clear(g.j, SETTINGS, ACTIVE, "human:kibrom (tty)", "reviewed", g.j.get(ks.KV_KEY))
    await g.check_entries()
    a = g.j.alerts(1)[0]
    assert a["kind"] == "kill_switch_loosened" and "NOT via shortbot-admin" not in a["message"]


async def test_an_unreadable_last_seen_state_counts_as_never_seen():
    g, fake = guardian()
    g.j.set(g.j.mode_key(gp.SEEN_KEY), "garbage")
    await ks.trip(g.j, SETTINGS, REDUCING, "guardian", "daily loss")
    await g.check_entries()
    assert g.j.alerts(1)[0]["kind"] == "kill_switch_state"


async def test_a_stale_halt_lifting_back_to_a_sticky_trip_is_not_a_tamper_alarm():
    g, fake = guardian()
    await ks.trip_on_daily_loss(g.j, SETTINGS, 9_600, -400)
    await ks.trip(g.j, SETTINGS, HALTED, "session watchdog", "laptop slept", cause=STALE)
    for i in (1, 2):
        protective_pass(g, i)
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


async def test_guarded_returns_the_hook_result_or_none_when_it_failed():
    g, fake = guardian()

    async def hit():
        return {"changed": True}

    async def broken():
        raise RuntimeError("database is locked")
    assert await guarded("kill_switch", hit(), g.n) == {"changed": True}
    assert await guarded("kill_switch", broken(), g.n) is None and "guardian_error:kill_switch" in kinds(g)
