"""A human clear that sticks: the loss trips hold at what the human accepted until the loss is a margin worse, its
period ends or the loss recovers (killswitch_rearm.py, task #63). Baseline limits: daily 3%, dd_stop 12%."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from shortbot import killswitch as ks
from shortbot import killswitch_rearm as rearm
from shortbot.journal import Journal
from shortbot.killswitch import ACTIVE, HALTED, REDUCING
from tests.test_killswitch import SETTINGS

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)   # a Thursday


def later(minutes=0, days=0):
    return NOW + timedelta(minutes=minutes, days=days)


async def cleared(j, margin=1.0, now=NOW):
    """What shortbot-admin clear does: preview the hold the human confirms, swap the row they saw."""
    r = await ks.clear(j, SETTINGS, ACTIVE, "human:kibrom (tty)", "reviewed", j.get(ks.KV_KEY),
                       hold=rearm.preview(j, margin, now))
    assert r["ok"] and ks.read(j)["state"] == ACTIVE
    return r


async def day(j, pct, now=NOW):
    """Day P&L of `pct` % on a 10k start of day."""
    return await ks.trip_on_daily_loss(j, SETTINGS, 10_000 + pct * 100, pct * 100, now=now)


async def dd(j, pct, now=NOW):
    return await ks.trip_on_drawdown(j, SETTINGS, pct, now=now)


async def test_a_cleared_daily_loss_holds_until_it_is_a_margin_worse():
    j = Journal(":memory:")
    assert (await day(j, -3.5))["changed"]
    r = await cleared(j)
    held = r["rearm"]["held"]["daily_loss"]
    assert held["baseline_pct"] == 3.5 and held["rearm_at_pct"] == 4.5 and held["period"] == "2026-10-01"
    snap = json.loads(j.decisions()[0]["snapshot"])
    assert snap["rearm"]["margin_pp"] == 1.0 and snap["rearm"]["held"]["daily_loss"]["rearm_at_pct"] == 4.5
    assert await day(j, -3.6, later(1)) is None
    assert await day(j, -4.4, later(2)) is None and ks.read(j)["state"] == ACTIVE
    r = await day(j, -4.5, later(3))
    assert r["changed"] and r["state"] == REDUCING and "re-arm" in r["reason"] and "3.50%" in r["reason"]
    ev = json.loads(j.decisions()[0]["snapshot"])["evidence"]
    assert ev["rearm"]["rearm_at_pct"] == 4.5 and ev["limit_pct"] == 3.0


async def test_a_daily_loss_hold_lapses_with_the_utc_day_or_a_recovery():
    j = Journal(":memory:")
    await day(j, -3.5)
    await cleared(j)
    assert (await day(j, -3.1, later(days=1)))["changed"]         # a new day: the plain limit again
    j2 = Journal(":memory:")
    await day(j2, -3.5)
    await cleared(j2)
    assert await day(j2, -1.9, later(5)) is None                  # back under limit - margin: the hold lapses
    assert j2.decisions()[0]["action"] == "kill_switch_rearmed"
    assert (await day(j2, -3.0, later(6)))["changed"]


async def test_a_cleared_drawdown_holds_until_a_margin_worse():
    j = Journal(":memory:")
    assert (await dd(j, 13.0))["changed"]
    r = await cleared(j)
    assert r["rearm"]["held"]["drawdown"]["rearm_at_pct"] == 14.0
    assert r["rearm"]["held"]["drawdown"]["period"] == "2026-W40"
    assert await dd(j, 13.9, later(5)) is None and ks.read(j)["state"] == ACTIVE
    assert (await dd(j, 14.0, later(10)))["changed"]


async def test_a_drawdown_hold_lapses_on_recovery_and_with_the_iso_week():
    j = Journal(":memory:")
    await dd(j, 13.0)
    await cleared(j)
    assert await dd(j, 11.5, later(5)) is None                    # inside the band: still held
    assert await dd(j, 10.9, later(10)) is None                   # < 12 - 1: lapsed
    assert (await dd(j, 12.0, later(15)))["changed"]              # a new drawdown trips at the plain stop
    j2 = Journal(":memory:")
    await dd(j2, 13.0)
    await cleared(j2)
    assert (await dd(j2, 13.0, later(days=4)))["changed"]         # Monday: review again (or reset the peak)


async def test_without_a_fresh_reading_a_clear_holds_nothing():
    j = Journal(":memory:")
    await dd(j, 13.0, later(-16))
    p = rearm.preview(j, 1.0, NOW)
    assert p["held"] == {} and "drawdown" in p["not_held"]
    await cleared(j)
    assert (await dd(j, 13.0, later(1)))["changed"]


async def test_only_conditions_over_their_limit_are_held_and_a_new_clear_replaces_the_hold():
    j = Journal(":memory:")
    await day(j, -1.0)
    await dd(j, 13.0)
    p = rearm.preview(j, 1.0, NOW)
    assert set(p["held"]) == {"drawdown"} and p["not_held"] == {}
    await cleared(j)
    await ks.trip(j, SETTINGS, REDUCING, "guardian", "untracked position", cause="reconciliation")
    await cleared(j, now=later(20))                               # the drawdown reading is stale by now
    assert (await dd(j, 13.0, later(21)))["changed"]


async def test_the_margin_is_bounded():
    j = Journal(":memory:")
    for bad in (-0.5, rearm.MAX_MARGIN_PP + 0.1):
        with pytest.raises(ValueError, match="margin"):
            rearm.preview(j, bad, NOW)


async def test_a_malformed_hold_is_ignored():
    for raw in ("garbage", json.dumps({"margin_pp": 1, "held": {"drawdown": {"rearm_at_pct": "x"}}}),
                json.dumps({"held": []}),
                json.dumps({"margin_pp": 50, "held": {"drawdown": {"baseline_pct": 13, "rearm_at_pct": 63,
                                                                   "period": "2026-W40"}}})):
        j = Journal(":memory:")
        j.set(j.mode_key(rearm.KV_HOLD), raw)
        assert (await dd(j, 13.5))["changed"]


async def test_a_clear_that_loses_the_race_stores_no_hold():
    j = Journal(":memory:")
    await dd(j, 13.0)
    shown, hold = j.get(ks.KV_KEY), rearm.preview(j, 1.0, NOW)
    await ks.trip(j, SETTINGS, HALTED, "agent (MCP)", "squeeze")   # lands while the human types
    r = await ks.clear(j, SETTINGS, ACTIVE, "human:kibrom (tty)", "reviewed", shown, hold=hold)
    assert not r["ok"] and j.get(j.mode_key(rearm.KV_HOLD)) is None and ks.read(j)["state"] == HALTED


async def test_status_shows_the_hold():
    j = Journal(":memory:")
    await dd(j, 13.0)
    assert ks.status(j)["rearm"] is None
    await cleared(j)
    assert ks.status(j)["rearm"]["drawdown"]["rearm_at_pct"] == 14.0


async def test_readings_and_holds_belong_to_one_account(tmp_path):
    live, demo = Journal(tmp_path / "desk.db", "live"), Journal(tmp_path / "desk.db", "demo")
    await dd(live, 13.0)
    await cleared(live)
    assert ks.status(live)["rearm"]["drawdown"] and ks.status(demo)["rearm"] is None
    assert rearm.preview(demo, 1.0, NOW)["held"] == {}                 # demo never saw live's reading
    assert (await dd(demo, 13.0, later(1)))["changed"]                # the state row itself is shared
    assert ks.read(live)["state"] == REDUCING
