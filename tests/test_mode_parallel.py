"""Demo and live desks run at once on one journal (docs/DESKS.md): each account has its own kill switch, guardian
heartbeat, alert cooldowns, acknowledged alerts, warm-up and symbol locks; neither desk's state moves the other's."""

import json
import time
from datetime import UTC, datetime

from shortbot import alerts_view
from shortbot import killswitch as ks
from shortbot.close_intent import recent_change
from shortbot.journal import Journal
from shortbot.notify import Notifier
from shortbot.symlock import lock_dir_for
from shortbot.warmup import last_activity
from tests.test_killswitch import SETTINGS


def pair(tmp_path):
    db = tmp_path / "data" / "shortbot.db"
    db.parent.mkdir()
    return Journal(db, "demo"), Journal(db, "live")


def test_demo_keeps_its_bare_keys_and_live_gets_its_own(tmp_path):
    demo, live = pair(tmp_path)
    assert (ks.state_key(demo), ks.heartbeat_key(demo), ks.fresh_key(demo)) == (ks.KV_KEY, ks.HEARTBEAT_KEY,
                                                                                ks.FRESH_KEY)
    assert ks.heartbeat_key(live) == "guardian_heartbeat@live" and ks.state_key(live) == "trading_state@live"


def test_one_guardian_beating_never_covers_the_other_account(tmp_path):
    demo, live = pair(tmp_path)
    demo.set(ks.heartbeat_key(demo), str(time.time()))
    assert ks.heartbeat_age(demo) < 5 and ks.heartbeat_age(live) is None
    assert ks.status(live)["guardian_alive"] is False


async def test_a_trip_halts_only_its_own_desk_and_a_clear_frees_only_its_own(tmp_path):
    demo, live = pair(tmp_path)
    await ks.trip(demo, SETTINGS, ks.HALTED, "drill", "plumbing drill")
    assert ks.read(demo)["state"] == ks.HALTED and ks.read(live)["state"] == ks.ACTIVE
    await ks.trip(live, SETTINGS, ks.REDUCING, "agent", "a stop I can't verify")
    r = await ks.clear(live, SETTINGS, ks.ACTIVE, "human:owner (tty)", "verified", live.get(ks.state_key(live)))
    assert r["ok"] and ks.read(live)["state"] == ks.ACTIVE and ks.read(demo)["state"] == ks.HALTED


async def test_a_stale_halt_on_one_desk_counts_only_its_own_fresh_passes(tmp_path):
    demo, live = pair(tmp_path)
    await ks.trip(live, SETTINGS, ks.HALTED, "session watchdog", "heartbeat 11 min old", cause=ks.STALE)
    for _ in range(3):   # demo's guardian keeps beating: live's halt must not lift on it
        demo.set(ks.heartbeat_key(demo), str(time.time() + _))
        assert await ks.auto_clear_stale(live, SETTINGS) is None
    assert ks.read(live)["state"] == ks.HALTED
    for i in range(ks.AUTO_CLEAR_CHECKS):   # live's own guardian is back
        live.set(ks.heartbeat_key(live), str(time.time() + i))
        out = await ks.auto_clear_stale(live, SETTINGS)
    assert out and out["state"] == ks.ACTIVE and ks.read(demo)["state"] == ks.ACTIVE


async def test_alert_cooldowns_are_per_desk(tmp_path):
    demo, live = pair(tmp_path)
    assert await Notifier(SETTINGS, demo).send("stop_near", "demo short", "BTC/USDT:USDT")
    assert await Notifier(SETTINGS, live).send("stop_near", "live short", "BTC/USDT:USDT")
    assert not await Notifier(SETTINGS, live).send("stop_near", "again", "BTC/USDT:USDT")


async def test_each_session_reads_and_acknowledges_only_its_own_alerts(tmp_path):
    demo, live = pair(tmp_path)
    demo.db.execute("INSERT INTO alerts (ts, kind, message) VALUES ('2026-09-01T00:00:00+00:00', 'old', 'pre-#32')")
    demo.db.commit()
    await Notifier(SETTINGS, demo).send("stop_missing", "demo", "A/USDT:USDT", cooldown_s=0)
    await Notifier(SETTINGS, live).send("stop_missing", "live", "B/USDT:USDT", cooldown_s=0)
    await demo.flush()
    await live.flush()
    assert [a["message"] for a in live.alerts()] == ["live"] and len(demo.alerts()) == 2   # NULL mode = demo
    acked = alerts_view.acknowledge(demo)
    assert acked["count"] == 2 and [a["message"] for a in acked["urgent"]] == ["demo"]
    assert "1 unacknowledged" in alerts_view.brief_lines(live)[0]   # demo's ack left live's urgent alert
    assert alerts_view.acknowledge(live)["count"] == 1
    assert "none unacknowledged" in alerts_view.brief_lines(demo)[0]


def test_a_warm_up_warms_only_its_own_desk(tmp_path):
    demo, live = pair(tmp_path)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    demo.db.execute("INSERT INTO warmups (started_at, t0_ms, symbols, snap0, status, checked_at) VALUES "
                    "(?, 0, '[]', '{}', 'done', ?)", (now, now))   # an old row: no mode written, demo's
    demo.db.commit()
    assert last_activity(demo) is not None and last_activity(live) is None


async def test_a_size_change_on_one_desk_is_not_the_others(tmp_path):
    demo, live = pair(tmp_path)
    demo.log_decision("partial_close", "T1", "A/USDT:USDT", snapshot={"amount": 1})
    await demo.flush()
    assert recent_change(demo, "A/USDT:USDT", None) and recent_change(live, "A/USDT:USDT", None) is None


def test_the_desks_never_wait_on_each_others_symbol_locks(tmp_path):
    demo, live = pair(tmp_path)
    assert lock_dir_for(demo.db, demo.mode) == tmp_path / "data" / "locks"
    assert lock_dir_for(live.db, live.mode) == tmp_path / "data" / "locks" / "live"


def test_per_desk_kv_never_leaks(tmp_path):
    demo, live = pair(tmp_path)
    for k in ("alert_ts:x:None", "news_seen:u", "ann_seen:u", "ann_initialised", "reconcile:empty_since",
              "guardian_last_run:setup_ladder", alerts_view.ACK_KEY):
        demo.set(demo.mode_key(k), json.dumps(1))
        assert live.get(live.mode_key(k)) is None, k


def test_both_desks_see_market_alerts_and_only_their_own_account_alerts(tmp_path):
    demo, live = pair(tmp_path)
    for kind, actor, sym in (("scan_alert", "guardian:scan", "A/USDT:USDT"), ("binance_new_listing", "guardian", None),
                             ("stop_near", "guardian:positions", "A/USDT:USDT")):
        demo.db.execute("INSERT INTO alerts (ts, kind, message, actor, symbol, mode) VALUES "
                        "('2026-10-04T00:00:00+00:00', ?, 'x', ?, ?, 'demo')", (kind, actor, sym))
    demo.db.commit()
    assert {a["kind"] for a in live.alerts()} == {"scan_alert", "binance_new_listing"}
    assert len(demo.alerts()) == 3


async def test_decisions_are_read_per_desk(tmp_path):
    demo, live = pair(tmp_path)
    demo.log_decision("skip", "demo only", "A/USDT:USDT")
    await demo.flush()
    assert [d["reasoning"] for d in demo.decisions()] == ["demo only"] and live.decisions() == []


def test_the_live_guardian_alerts_a_stale_market_feed(tmp_path):
    from shortbot import market_feed
    from shortbot.market.market_map import KV_LAST
    demo, live = pair(tmp_path)
    assert "never seen" in market_feed.problem(live)
    demo.set(ks.heartbeat_key(demo), str(time.time() - market_feed.STALE_S - 60))
    assert "min old" in market_feed.problem(live)
    demo.set(ks.heartbeat_key(demo), str(time.time()))
    assert "no market map" in market_feed.problem(live)
    demo.set(KV_LAST, json.dumps({"ts": time.time(), "pause": []}))
    assert market_feed.problem(live) is None


def test_coin_events_are_new_per_desk(tmp_path):
    from types import SimpleNamespace

    from shortbot.guardian_coins import CoinJobs
    demo, live = pair(tmp_path)
    ev = [{"name": "oi_surge", "detail": "+12%"}, {"name": "btc_impulse", "detail": "quiet"}]
    d, li = SimpleNamespace(j=demo), SimpleNamespace(j=live)
    assert [e["name"] for e in CoinJobs._new_events(d, "A/USDT:USDT", ev)] == ["oi_surge"]
    assert CoinJobs._new_events(d, "A/USDT:USDT", ev) == []   # demo's next pass: seen
    assert [e["name"] for e in CoinJobs._new_events(li, "A/USDT:USDT", ev)] == ["oi_surge"]   # live: still new
