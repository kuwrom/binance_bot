import sqlite3
import statistics

import pytest

from shortbot import registry as R

DAY = 86_400_000
NOW = 1_790_000_000_000


def db():
    d = sqlite3.connect(":memory:")
    d.execute("CREATE TABLE learnings (id INTEGER PRIMARY KEY, created_at TEXT, updated_at TEXT, setup TEXT,"
              " statement TEXT, evidence TEXT, status TEXT, confidence TEXT)")
    R.ensure(d)
    return d


def pat(**kw):
    p = {"id": "script.discount_squeeze", "level": "script", "parent": "tape/pump_in_progress",
         "name": "discount squeeze", "mechanism": "crowded shorts pay funding", "definition": "perp >= 1% under index",
         "detector": "forensics", "metric": "short_ret_72h_pct", "status": "shadow"}
    p.update(kw)
    return p


def test_versions_only_when_the_definition_changes_and_live_cannot_be_registered():
    d = db()
    assert R.register(d, pat())["version"] == 1
    assert R.register(d, pat(name="renamed"))["new"] is False
    assert R.register(d, pat(definition="perp >= 1.5% under index"))["version"] == 2
    with pytest.raises(ValueError):
        R.register(d, pat(id="x", status="live"))


def test_card_is_coin_clustered_and_backtest_oos_evidence_never_promotes():
    d = db()
    R.register(d, pat())
    eps = [{"symbol": f"C{i}", "ts_ms": NOW - i * DAY, "source": "t", "oos": True, "outcome": 5.0 + (i % 3),
            "win": True, "mae_pct": 10.0} for i in range(35)]
    assert R.add_episodes(d, "script.discount_squeeze", eps) == 35
    assert R.add_episodes(d, "script.discount_squeeze", eps) == 0            # idempotent
    c = R.card(d, "script.discount_squeeze", NOW)
    assert c["oos"]["n_coins"] == 35 and c["oos"]["ci95"][0] > 0 and c["oos"]["t_day"] >= 3   # the old gate: live
    assert c["forward"]["status"] == "no_feed" and c["verdict"] == "keep shadow: no forward feed (not promotable)"
    res = R.set_status(d, "script.discount_squeeze", "live", "evidence")
    assert not res["ok"] and "forward" in res["error"]
    assert R.latest(d, "script.discount_squeeze")["status"] == "shadow"


def _eps(ts, n=35, out=lambda i: 5.0 + (i % 3)):
    return [{"symbol": f"C{i}", "ts_ms": ts(i), "source": "t", "oos": True, "outcome": out(i)} for i in range(n)]


def _card(eps, **kw):
    d = db()
    R.register(d, pat(**kw))
    R.add_episodes(d, "script.discount_squeeze", eps)
    return d, R.card(d, "script.discount_squeeze", NOW)


def test_card_stats_cluster_by_coin_and_by_day():
    _, c = _card(_eps(lambda i: NOW - i * 60_000))                      # 35 coins, one day
    assert c["oos"]["n_coins"] == 35 and c["oos"]["ci95"][0] > 0 and c["oos"]["t"] >= 3
    assert c["oos"]["t_day"] is None and c["oos"]["n_days"] == 1
    _, c = _card(_eps(lambda i: NOW - (i % 3) * DAY))                   # 35 coins over 3 days
    assert c["oos"]["n_days"] == 3 and c["oos"]["t_day"] > 9
    # 12 days x coins scoring 5/6/7: every day's mean is 6, so no day-to-day variance: the day-clustered variance is
    # floored at the iid one (design effect >= 1; #31), so t_day is the iid t, not missing
    _, c = _card(_eps(lambda i: NOW - (i % 12) * DAY, n=36, out=lambda i: 5.0 + i // 12))
    sd = statistics.stdev([5.0 + i // 12 for i in range(36)])
    assert c["oos"]["n_days"] == 12 and c["oos"]["t_day"] == round(6.0 / (sd / 6), 2) and c["oos"]["ci95"][0] > 0
    # the t belongs to the pooled mean: one coin with 30 losing rows and 10 coins winning once
    _, c = _card([{"symbol": "BIG", "ts_ms": NOW - k * DAY, "source": "t", "oos": True, "outcome": -1.0}
                  for k in range(30)] + _eps(lambda i: NOW - i * DAY, n=10, out=lambda i: 1.0 + i % 2))
    assert c["oos"]["mean"] < 0 and c["oos"]["t"] < 0
    assert c["verdict"] == "keep shadow: no forward feed (not promotable)"


def test_decay_puts_a_live_pattern_on_probation():
    d = db()
    R.register(d, pat())
    old = [{"symbol": f"O{i}", "ts_ms": NOW - (200 + i) * DAY, "source": "t", "oos": True, "outcome": 6.0 + i % 2}
           for i in range(40)]
    new = [{"symbol": f"N{i}", "ts_ms": NOW - i * DAY, "source": "live", "oos": True, "outcome": (-1) ** i * 3.0}
           for i in range(12)]
    R.add_episodes(d, "script.discount_squeeze", old + new)
    d.execute("UPDATE patterns SET status='live'")                      # promoted earlier
    assert R.card(d, "script.discount_squeeze", NOW)["verdict"].startswith("probation")


def test_duplicates_and_lesson_search():
    d = db()
    R.register(d, pat())
    R.register(d, pat(id="script.copy", definition="same thing"))
    eps = [{"symbol": f"C{i}", "ts_ms": NOW + i, "source": "t", "outcome": 1.0} for i in range(10)]
    R.add_episodes(d, "script.discount_squeeze", eps)
    R.add_episodes(d, "script.copy", eps)
    assert R.duplicates(d, "script.copy")[0]["id"] == "script.discount_squeeze"
    d.execute("INSERT INTO learnings VALUES (1,'','','pump_fade','Invalidations need acceptance, two closes','ZRO',"
              "'adopted','medium')")
    assert R.reindex_lessons(d) == 2                   # patterns only: learnings have their own live index
    d.execute("INSERT INTO lessons_fts VALUES ('learning', '#1', 'acceptance closes')")   # a row an old seed left
    assert R.search(d, "acceptance closes") == []
    hits = R.search(d, "crowded shorts")
    assert hits and {h["kind"] for h in hits} == {"pattern"}
    assert R.search(d, '"(((') == [] and R.search(d, "trade:12 a-b AND") == []
    assert "script" in R.tree(d)


def test_feed_candidates_adds_live_episodes_to_hierarchy_nodes():
    from shortbot.journal import Journal
    from shortbot.registry_seed import feed_candidates, seed_static
    j = Journal(":memory:")
    seed_static(j.db)
    import time
    now = int(time.time() * 1000) + 5000
    feats = {"lifecycle_stage": "post_pump", "controller_labels": ["thin_float"], "tape_state": "failed_sweep",
             "funding_rate": -0.002}
    j.log_candidates("2026-09-29T00:00:00+00:00", now, [
        {"symbol": "ABC/USDT:USDT", "score": 10, "setups": ["pump_fade"], "vetoes": [], "signals": {}, "price": 1.0,
         "features": feats}], "favorable")
    j.log_candidates("2026-09-20T00:00:00+00:00", now - 30 * DAY, [   # scanned before the nodes existed: skipped
        {"symbol": "OLD/USDT:USDT", "score": 10, "setups": ["pump_fade"], "vetoes": [], "signals": {}, "price": 1.0,
         "features": feats}], "favorable")
    j.db.execute("UPDATE candidates SET short_ret_24h=3.0, max_adverse_24h=2.0, labelled_at='x'")
    j.db.commit()
    assert feed_candidates(j) >= 4 and feed_candidates(j) == 0
    c = R.card(j.db, "lifecycle.post_pump", now)
    assert c["all"]["n"] == 1 and c["sources"] == {"scan": 1}          # the old scan was not backfilled
    assert R.card(j.db, "tape.failed_sweep", now)["all"]["n"] == 1      # scan-time tape state used
    assert R.card(j.db, "detector.pump_fade_discount_scan", now)["all"]["n"] == 1
    R.register(j.db, {"id": "strategy.x", "level": "strategy", "definition": "r", "metric": "trade_r"})
    assert R.latest(j.db, "strategy.x")["metric"] != "short_ret_24h_pct"   # R-unit nodes never get scan %


def test_refresh_is_due_weekly():
    from shortbot import refresh
    from shortbot.journal import Journal
    j = Journal(":memory:")
    assert refresh.due(j, 1_000_000)
    j.set(refresh.KV_KEY, str(1_000_000))
    assert not refresh.due(j, 1_000_000 + 86_400) and refresh.due(j, 1_000_000 + 7 * 86_400)


def test_refresh_steps_never_raise():
    import sys

    from shortbot import refresh
    r = refresh._run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
    assert r["rc"] == -1 and "timeout" in r["error"]
    s = refresh._step("boom", lambda: 1 / 0)
    assert "ZeroDivisionError" in s["error"]
