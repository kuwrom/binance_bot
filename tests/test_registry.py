import sqlite3

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


def test_card_is_coin_clustered_and_promotion_needs_oos_evidence():
    d = db()
    R.register(d, pat())
    eps = [{"symbol": f"C{i}", "ts_ms": NOW - i * DAY, "source": "t", "oos": True, "outcome": 5.0 + (i % 3),
            "win": True, "mae_pct": 10.0} for i in range(35)]
    assert R.add_episodes(d, "script.discount_squeeze", eps) == 35
    assert R.add_episodes(d, "script.discount_squeeze", eps) == 0            # idempotent
    c = R.card(d, "script.discount_squeeze", NOW)
    assert c["oos"]["n_coins"] == 35 and c["oos"]["ci95"][0] > 0 and c["verdict"] == "eligible for live"
    assert R.set_status(d, "script.discount_squeeze", "live", "evidence")["ok"]
    R.register(d, pat(id="weak", definition="w"))
    R.add_episodes(d, "weak", [{"symbol": "A", "ts_ms": NOW, "source": "t", "oos": True, "outcome": 1.0}])
    assert not R.set_status(d, "weak", "live", "hope")["ok"]


def _eps(ts, n=35, out=lambda i: 5.0 + (i % 3)):
    return [{"symbol": f"C{i}", "ts_ms": ts(i), "source": "t", "oos": True, "outcome": out(i)} for i in range(n)]


def _card(eps, **kw):
    d = db()
    R.register(d, pat(**kw))
    R.add_episodes(d, "script.discount_squeeze", eps)
    return d, R.card(d, "script.discount_squeeze", NOW)


def test_single_day_oos_has_no_day_clustered_t_and_is_not_eligible():
    d, c = _card(_eps(lambda i: NOW - i * 60_000))                      # 35 coins, one day
    assert c["oos"]["n_coins"] == 35 and c["oos"]["ci95"][0] > 0 and c["oos"]["t"] >= R.PROMOTE_MIN_T
    assert c["oos"]["t_day"] is None and c["oos"]["n_days"] == 1
    assert c["verdict"] == "keep shadow: only 1 OOS day (need 10)"
    res = R.set_status(d, "script.discount_squeeze", "live", "hope")
    assert not res["ok"] and "only 1 OOS day" in res["error"]
    assert R.latest(d, "script.discount_squeeze")["status"] == "shadow"


def test_three_days_are_not_eligible_even_with_a_high_day_clustered_t():
    d, c = _card(_eps(lambda i: NOW - (i % 3) * DAY))                   # 35 coins over 3 days
    assert c["oos"]["n_days"] == 3 and c["oos"]["t_day"] > 3 * R.PROMOTE_MIN_T   # every other gate clears
    assert c["verdict"] == "keep shadow: only 3 OOS days (need 10)"
    assert not R.set_status(d, "script.discount_squeeze", "live", "hope")["ok"]


def test_enough_days_but_no_day_clustered_t_is_not_eligible():
    # 12 days x coins scoring 5/6/7: every day's mean is 6, so no day-to-day variance and no t_day
    d, c = _card(_eps(lambda i: NOW - (i % 12) * DAY, n=36, out=lambda i: 5.0 + i // 12))
    assert c["oos"]["n_days"] == 12 and c["oos"]["t_day"] is None and c["oos"]["ci95"][0] > 0
    assert c["verdict"] == "keep shadow: no day-clustered t (no day-to-day variance)"


def test_multi_day_oos_with_a_good_day_clustered_t_is_still_eligible():
    d, c = _card(_eps(lambda i: NOW - (i % 12) * DAY), status="draft")   # 35 coins over 12 days
    assert c["oos"]["n_days"] == 12 and c["oos"]["t_day"] >= R.PROMOTE_MIN_T
    assert c["verdict"] == "eligible for live"
    assert R.set_status(d, "script.discount_squeeze", "live", "evidence")["ok"]


def test_decay_puts_a_live_pattern_on_probation():
    d = db()
    R.register(d, pat())
    old = [{"symbol": f"O{i}", "ts_ms": NOW - (200 + i) * DAY, "source": "t", "oos": True, "outcome": 6.0 + i % 2}
           for i in range(40)]
    new = [{"symbol": f"N{i}", "ts_ms": NOW - i * DAY, "source": "live", "oos": True, "outcome": (-1) ** i * 3.0}
           for i in range(12)]
    R.add_episodes(d, "script.discount_squeeze", old + new)
    assert R.set_status(d, "script.discount_squeeze", "live", "evidence")["ok"]
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
    assert R.reindex_lessons(d) == 3
    hits = R.search(d, "acceptance closes")
    assert hits and hits[0]["ref"] == "#1"
    assert R.search(d, '"(((') == []
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
    from shortbot.journal import Journal
    from shortbot import refresh
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
