"""The restart drill (#46, drill_restart.py) against the fake: `before` leaves a drill short with 2 resting targets and a
far entry; the outage is played as the runbook's hand steps (the guardian's heartbeat goes stale, the session watchdog
trips HALTED, a new guardian starts, verifies twice and lifts the halt, its stream connects); `after` checks it all,
closes the drill trade with its lesson and journals the drill_result. Missing evidence: it answers `pending` and
journals nothing, so it can run again."""

import asyncio
import json
import time

import pytest

from shortbot import drill, drill_log, drill_restart, killswitch_ops, uptime
from shortbot import killswitch as ks
from shortbot.guardian import Guardian
from shortbot.journal import now_iso
from shortbot.journal_tables import DRILL_SETUP
from shortbot.targets import is_target
from shortbot.user_stream import STATS_KEY
from tests.test_drill import ADA, XRP, desk, favorable, intent_rows, results


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    async def instant(_s):
        return None
    monkeypatch.setattr(drill, "_sleep", instant)
    monkeypatch.setattr("shortbot.market.regime.market_regime", favorable)


async def outage(app, down_s=700.0):
    """The runbook's hand steps: the guardian stopped `down_s` ago, the watchdog run, the guardian started again."""
    j, tr = app.journal, app.trader
    j.set(ks.HEARTBEAT_KEY, str(time.time() - down_s))
    why = f"guardian heartbeat is {down_s / 60:.0f} min old"
    await killswitch_ops.halt(tr, ks.HALTED, "session watchdog", why, {"heartbeat_age_s": down_s}, cause=ks.STALE)
    g = Guardian(app)
    await g._startup()                              # guardian_started
    for _ in range(ks.AUTO_CLEAR_CHECKS):           # verified passes; the 5 s tick lifts the stale halt
        await g.check_positions()
        await g._kill_watch()
        await asyncio.sleep(0.002)                  # distinct heartbeat stamps
    j.set(j.mode_key(STATS_KEY), json.dumps({"since": now_iso(), "connects": 1, "up": True,
                                             "last_connect_at": now_iso()}))   # its user stream connected
    return g


def sb_t(fake, s=XRP):
    return [o for o in fake.limits.values() if o["symbol"] == s and o["status"] == "open" and is_target(o.get("cid"))]


async def test_before_outage_after_passes_and_closes_the_drill_trade(monkeypatch):
    app, fake = desk(monkeypatch)
    j = app.journal
    b = await drill.run(app, "restart", phase="before")
    assert b["ok"] and b["phase"] == "before" and len(b["next"]) == 4, b
    assert results(j) == [] and json.loads(j.get(drill_restart.STATE_KEY))["trade_id"] == b["trade_id"]
    assert len(sb_t(fake)) == 2 and len(await app.trader.stop_orders(XRP, True)) == 1
    assert uptime.windows(j)["restart"]["until"] > time.time() + 3600   # open through the outage
    await outage(app)
    assert ks.read(j)["state"] == ks.ACTIVE                              # lifted by the guardian
    res = await drill.run(app, "restart", phase="after", wait_min=0)
    assert res["verdict"] == "pass", {k: c for k, c in res["checks"].items() if not c["ok"]}
    assert {"stale_trip", "auto_clear", "guardian_started", "user_stream", "gap_logged", "gap_tagged",
            "one_close_stop", "targets_resting", "no_bad_alerts", "entry_withdrawn", "heartbeat_verified", "closed",
            "nothing_left", "before:opened", "before:entry_rests"} <= set(res["checks"])
    (gap,) = uptime.gaps(j)
    assert gap["tag"] == "drill" and gap["secs"] >= 600
    t = j.get_trade(b["trade_id"])
    assert t["setup"] == DRILL_SETUP and t["status"] == "closed" and t["lesson"] == drill_restart.LESSON
    assert fake.sym[XRP]["qty"] == 0 and fake.sym[ADA]["qty"] == 0
    assert j.get(drill_restart.STATE_KEY) is None and uptime.windows(j)["restart"]["until"] <= time.time()
    latest = drill_log.latest_results(j)["restart"]
    assert latest["run"] == res["run"] and latest["trade_id"] == b["trade_id"]
    kinds = [r["kind"] for r in intent_rows(j) if r["actor"] == "cli:drill:restart"]
    assert kinds == ["open_short", "propose_entries", "close_position"]


async def test_after_before_the_guardian_is_back_is_pending_and_journals_nothing(monkeypatch):
    app, _ = desk(monkeypatch)
    j = app.journal
    b = await drill.run(app, "restart", phase="before")
    res = await drill.run(app, "restart", phase="after", wait_min=0)
    assert res["ok"] is False and set(res["pending"]) == set(drill_restart.EVIDENCE) and results(j) == []
    assert j.get_trade(b["trade_id"])["status"] == "open"
    await outage(app)
    assert (await drill.run(app, "restart", phase="after", wait_min=0))["verdict"] == "pass"


async def test_an_alert_about_a_naked_or_untracked_position_fails_it(monkeypatch):
    app, _ = desk(monkeypatch)
    await drill.run(app, "restart", phase="before")
    await outage(app)
    app.journal.log_alert("untracked_position", "a position without a journal trade", "PUMP/USDT:USDT")
    res = await drill.run(app, "restart", phase="after", wait_min=0)
    assert res["verdict"] == "fail" and not res["checks"]["no_bad_alerts"]["ok"] and res["checks"]["closed"]["ok"]


async def test_a_short_outage_never_trips_the_stale_halt_and_fails(monkeypatch):
    app, _ = desk(monkeypatch)
    st = await drill.run(app, "restart", phase="before")
    g = Guardian(app)
    await g._startup()
    await g.check_positions()
    app.journal.set(drill_restart.STATE_KEY, json.dumps({**json.loads(app.journal.get(drill_restart.STATE_KEY)),
                                                         "at": time.time() - drill_restart.WINDOW_S - 1}))
    res = await drill.run(app, "restart", phase="after", wait_min=0)   # the window is over: judged as it is
    assert res["verdict"] == "fail" and not res["checks"]["stale_trip"]["ok"]
    assert app.journal.get_trade(st["trade_id"])["status"] == "closed"


async def test_refusals(monkeypatch):
    app, fake = desk(monkeypatch)
    r = await drill.run(app, "restart", phase="after")
    assert r["refused"] and "before" in r["error"]
    r = await drill.run(app, "restart")
    assert r["ok"] is False and "--phase" in r["error"]
    assert (await drill.run(app, "restart", phase="before"))["ok"]
    r = await drill.run(app, "restart", phase="before")
    assert r["refused"] and "under way" in r["error"]
    app, fake = desk(monkeypatch, mode="live")
    r = await drill.run(app, "restart", phase="after")
    assert r["refused"] and "demo" in r["error"] and fake.orders == []
