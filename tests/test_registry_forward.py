"""#26: forward-only, pre-registered scoring. A frozen plan per pattern version, forward symbol-episodes vs the same
scan's unflagged coins after costs, one decision on a fixed sample at alpha / M, day-clustered decay (finding F)."""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from shortbot import registry as R
from shortbot import registry_forward as F
from shortbot import research_stats as rs
from shortbot.journal import Journal
from shortbot.registry_seed import SCAN_METRIC, feed_candidates

H = 3_600_000
DAY = 24 * H
REG = "2026-01-01T00:00:00+00:00"
T_REG = int(datetime.fromisoformat(REG).timestamp() * 1000)
T0 = T_REG + 2 * H


def _at(monkeypatch, iso: str) -> None:
    monkeypatch.setattr(R, "_now", lambda: iso)


def _node(db, pid="setup.pump_fade", metric=SCAN_METRIC, **kw):
    return R.register(db, {"id": pid, "level": pid.split(".")[0] if pid.split(".")[0] in R.LEVELS else "setup",
                           "definition": kw.pop("definition", "d"), "detector": "x", "metric": metric,
                           "status": "shadow", **kw})


def _scan(j, ts, rows):
    """rows: (symbol, ret, flagged, features?)"""
    j.log_candidates("t", ts, [{"symbol": s, "price": 1.0, "score": 10, "vetoes": [],
                                "setups": ["pump_fade"] if flag else [],
                                "features": {"tape_state": "no_edge", **(f[0] if f else {})}}
                               for s, _, flag, *f in rows], "caution")
    lab = {s: r for s, r, *_ in rows}
    for c in j.unlabelled_candidates(older_than_ms=10**15, limit=10**6):
        if c["ts_ms"] == ts:
            j.label_candidate(c["id"], {"short_ret_24h": lab[c["symbol"]], "max_adverse_24h": 2.0})


def _eps(db, pid="setup.pump_fade"):
    return [(s, t, o, json.loads(f)) for s, t, o, f in db.execute(
        "SELECT symbol, ts_ms, outcome, features FROM pattern_episodes WHERE pattern_id=? ORDER BY ts_ms, symbol",
        (pid,))]


# ------------------------------------------------------------------ the frozen plan
def test_plan_is_frozen_at_registration_and_any_edit_is_a_new_hypothesis(monkeypatch):
    d = sqlite3.connect(":memory:")
    _at(monkeypatch, REG)
    assert _node(d)["version"] == 1
    p = F.plan_of(d, "setup.pump_fade", 1)
    assert p["registered_at"] == REG and p["registered_ms"] == T_REG and len(p["spec_hash"]) == 16
    assert p["plan"]["target_n"] == 100 and p["plan"]["min_days"] == 20 and p["plan"]["alpha"] == 0.025
    assert p["plan"]["decidable"] and p["plan"]["feed"] == "scan" and "alpha / M" in p["plan"]["rule"]
    with pytest.raises(sqlite3.DatabaseError):
        d.execute("UPDATE pattern_plans SET plan='{}'")
    with pytest.raises(sqlite3.DatabaseError):
        d.execute("DELETE FROM pattern_plans")
    assert _node(d)["new"] is False                                    # same spec: no new hypothesis
    _at(monkeypatch, "2026-02-01T00:00:00+00:00")
    r = _node(d, plan={"target_n": 200})                               # a new target is a new hypothesis, new date
    assert r == {"id": "setup.pump_fade", "version": 2, "new": True}
    p2 = F.plan_of(d, "setup.pump_fade", 2)
    assert p2["plan"]["target_n"] == 200 and p2["registered_at"] == "2026-02-01T00:00:00+00:00"
    assert p2["spec_hash"] != p["spec_hash"] and F.plan_of(d, "setup.pump_fade", 1) == p
    assert _node(d, plan={"target_n": 200})["new"] is False
    with pytest.raises(ValueError):
        _node(d, pid="setup.tiny", plan={"target_n": 10})               # below the floors: refused
    assert not F.plan_of(d, *[_node(d, pid="veto.x", metric="blocked_vs_allowed_r")[k]
                              for k in ("id", "version")])["plan"]["decidable"]
    assert F.plan_of(d, *[_node(d, pid="strategy.pump_fade", metric="trade_r")[k]
                          for k in ("id", "version")])["plan"]["target_n"] == 30


# ------------------------------------------------------------------ the feed (G/H for the registry)
def test_feed_scores_symbol_episodes_against_the_same_scans_unflagged_coins_after_costs(monkeypatch):
    j = Journal(":memory:")
    _at(monkeypatch, REG)
    _node(j.db)
    fund = ({"funding_rate": 0.0001, "funding_interval_h": 8},)      # +0.03% carry to a short
    _scan(j, T_REG - H, [("OLD", 9.0, True), *((f"U{i}", 1.0, False) for i in range(5))])   # before registration
    _scan(j, T0, [("A", 5.0, True, *fund), ("B", 3.0, True), ("C", 1.0, False), ("D", 2.0, False),
                  ("E", 3.0, False), ("G", 4.0, False), ("K", 0.0, False)])
    _scan(j, T0 + 2 * H, [("A", 9.0, True), *((f"U{i}", 1.0, False) for i in range(5))])     # rescan: same bet
    _scan(j, T0 + 3 * H, [("Z", 4.0, True), ("C", 1.0, False), ("D", 2.0, False)])           # too few controls
    assert feed_candidates(j) == 3 and feed_candidates(j) == 0         # idempotent (net new episodes)
    eps = _eps(j.db)
    assert [(s, t) for s, t, *_ in eps] == [("A", T0), ("B", T0), ("Z", T0 + 3 * H)]
    a = eps[0][3]
    assert eps[0][2] == 5.0 and a["control_n"] == 5 and a["vs_control"] == pytest.approx(5.0 - 2.0)
    assert a["vs_market"] == pytest.approx(5.0 - 3.0)                 # median of all 7 coins in the scan
    assert a["net"] == pytest.approx(5.0 - 0.1 + 0.03) and a["adj"] == pytest.approx(4.93 - 2.0)
    assert eps[1][3]["adj"] == pytest.approx(3.0 - 0.1 - 2.0)
    assert eps[2][3]["adj"] is None and eps[2][3]["control_n"] == 2


def test_relabels_and_late_labels_propagate_into_the_registry(monkeypatch):
    j = Journal(":memory:")
    _at(monkeypatch, REG)
    _node(j.db)
    ctl = [(f"U{i}", 1.0, False) for i in range(5)]
    _scan(j, T0 + 2 * H, [("A", 4.0, True), *ctl])
    j.log_candidates("t", T0, [{"symbol": "A", "price": 1.0, "score": 10, "vetoes": [], "setups": ["pump_fade"],
                                "features": {"tape_state": "no_edge"}}], "caution")      # not labelled yet
    feed_candidates(j)
    assert [(s, t, o) for s, t, o, _ in _eps(j.db)] == [("A", T0 + 2 * H, 4.0)]
    for c in j._rows("SELECT id, symbol, ts_ms FROM candidates"):     # the late label + a relabel (v2) of every row
        j.label_candidate(c["id"], {"short_ret_24h": -2.0 if c["symbol"] == "A" else 1.0, "max_adverse_24h": 1.0}, 2)
    feed_candidates(j)
    assert [(s, t, o) for s, t, o, _ in _eps(j.db)] == [("A", T0, -2.0)]   # the T0 row now opens the episode


# ------------------------------------------------------------------ the decision
def _fwd_db(monkeypatch, target_n=40, extra_nodes=0):
    d = sqlite3.connect(":memory:")
    _at(monkeypatch, REG)
    _node(d, plan={"target_n": target_n})
    for i in range(extra_nodes):
        _node(d, pid=f"tape.n{i}")
    return d


def _add(d, values_by_day, start_day=0, pid="setup.pump_fade", raw=None):
    rows = [{"symbol": f"S{day}_{k}", "ts_ms": T0 + (start_day + day) * DAY + k * H, "source": "scan", "oos": True,
             "outcome": raw if raw is not None else v, "features": {"adj": v}}
            for day, vals in enumerate(values_by_day) for k, v in enumerate(vals)]
    R.add_episodes(d, pid, rows)


def _days(a, n=20, per=2):
    """n days; day d's values are 1 +/- a (alternating), `per` episodes a day: mean 1, t = 1 / se."""
    return [[1 + (a if d % 2 else -a)] * per for d in range(n)]


def test_collecting_until_the_fixed_sample_then_one_decision_later_data_cannot_change(monkeypatch):
    d = _fwd_db(monkeypatch)
    _add(d, _days(0.5, n=19))
    f = R.card(d, "setup.pump_fade", T0 + 40 * DAY)
    assert f["forward"]["status"] == "collecting" and f["forward"]["interim"]["n"] == 38
    assert "not a test" in f["forward"]["interim"]["note"] and "ci95" not in f["forward"]["interim"]
    assert f["verdict"] == "keep shadow: forward 38/40 obs, 19/20 days"
    assert not R.set_status(d, "setup.pump_fade", "live", "hope")["ok"]
    _add(d, _days(0.5, n=1), start_day=19)
    c = R.card(d, "setup.pump_fade", T0 + 40 * DAY)
    t = c["forward"]["test"]
    assert c["forward"]["status"] == "decided" and c["forward"]["decision"] == "pass" and t["n"] == 40
    assert t["days"] == 20 and t["df"] == 19 and t["M"] == 1 and t["p"] <= t["alpha_per_test"] == 0.025
    assert c["verdict"] == "eligible for live"
    _add(d, [[-50.0] * 5] * 30, start_day=20)                          # a disaster after the fixed sample
    assert R.card(d, "setup.pump_fade", T0 + 40 * DAY)["forward"]["test"] == t   # the decision stands (no re-roll)
    assert R.set_status(d, "setup.pump_fade", "live", "forward test passed")["ok"]


def test_bonferroni_over_the_forward_family(monkeypatch):
    vals = _days(19 ** 0.5 / 3)       # CV3 se = a / sqrt(19) for 20 equal days: t = 3, p ~ 0.004 on 19 df
    one = _fwd_db(monkeypatch)
    _add(one, vals)
    many = _fwd_db(monkeypatch, extra_nodes=39)
    _add(many, vals)
    t1 = R.card(one, "setup.pump_fade")["forward"]["test"]
    t40 = R.card(many, "setup.pump_fade")["forward"]["test"]
    assert t1["t"] == pytest.approx(3.0, abs=0.05) and t1["p"] == t40["p"]
    assert t1["M"] == 1 and t1["decision"] == "pass"
    assert t40["M"] == 40 and t40["alpha_per_test"] == pytest.approx(0.025 / 40) and t40["decision"] == "fail"
    assert t40["t_crit"] == pytest.approx(rs.t_ppf(1 - 0.025 / 40, 19))
    assert R.card(many, "setup.pump_fade")["verdict"].startswith("keep shadow: forward test failed")
    R.register(many, {"id": "tape.draft", "level": "tape", "definition": "d", "metric": SCAN_METRIC})
    R.register(many, {"id": "veto.v", "level": "veto", "definition": "d", "metric": "blocked_vs_allowed_r",
                      "status": "shadow"})
    assert F.family(many)["M"] == 40                                   # drafts and feedless nodes are not tested


def test_falling_market_raw_shorts_win_but_the_adjusted_test_fails(monkeypatch):
    j = Journal(":memory:")
    _at(monkeypatch, REG)
    _node(j.db, plan={"target_n": 40})
    for day in range(20):                                              # everything falls; flagged coins fall less
        x = day % 3
        _scan(j, T0 + day * DAY, [("A", 3.0 + x, True), ("B", 2.5 + x, True),
                                  *((f"U{i}", 5.0 + x, False) for i in range(5))])
    feed_candidates(j)
    c = R.card(j.db, "setup.pump_fade", T0 + 25 * DAY)
    assert c["all"]["mean"] > 2 and c["all"]["ci95"][0] > 0           # the raw card: "every short works"
    assert c["forward"]["decision"] == "fail" and c["forward"]["test"]["mean"] < -2
    assert c["verdict"].startswith("keep shadow: forward test failed")


def test_backtest_rows_rebuilt_from_hist_db_never_enter_the_forward_test(monkeypatch):
    d = sqlite3.connect(":memory:")
    _at(monkeypatch, REG)
    _node(d, pid="strategy.pump_fade", metric="trade_r")
    R.add_episodes(d, "strategy.pump_fade", [{"symbol": f"C{i}", "ts_ms": T0 + (i % 25) * DAY, "source": "bt60",
                                              "oos": True, "outcome": 2.0 + i % 3} for i in range(60)])
    c = R.card(d, "strategy.pump_fade", T0 + 30 * DAY)
    assert c["oos"]["n"] == 60 and c["oos"]["ci95"][0] > 0            # stellar backtest
    assert c["forward"]["status"] == "collecting" and c["forward"]["interim"]["n"] == 0
    assert c["verdict"] == "keep shadow: forward 0/30 obs, 0/20 days"


# ------------------------------------------------------------------ trades
def _trade(j, symbol, setup, opened_ms, hold_h, pnl, entry=100.0, stop=105.0, amount=1.0):
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()  # noqa: E731
    j.db.execute("INSERT INTO trades (symbol, status, opened_at, entry_price, amount, initial_stop, stop_price, setup,"
                 " closed_at, exit_price, pnl, mae_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (symbol, "closed", iso(opened_ms), entry, amount, stop, stop, setup, iso(opened_ms + hold_h * H),
                  entry - pnl / amount, pnl, 1.0))
    j.db.commit()


def test_trades_score_net_r_minus_the_market_drift_over_the_hold(monkeypatch):
    j = Journal(":memory:")
    _at(monkeypatch, REG)
    _node(j.db, pid="strategy.pump_fade", metric="trade_r")
    _scan(j, T0, [("X", 1.0, True), *((f"U{i}", r, False) for i, r in enumerate((2.0, 4.0, 4.0, 6.0, 8.0)))])
    _trade(j, "X", "pump_fade", T0 + H // 2, 6, 10.0)                  # risk 5 USDT: R = 2
    _trade(j, "Y", "pump_fade", T_REG - H, 2, 10.0)                    # opened before registration
    _trade(j, "W", "exp_level_fade", T0 + H, 2, 10.0)                  # an experiment: not this node
    _trade(j, "V", "pump_fade", T0 + 10 * DAY, 3, -5.0)                # no scan in the 6h before: no control
    feed_candidates(j)
    eps = _eps(j.db, "strategy.pump_fade")
    assert [(s, o) for s, _, o, _ in eps] == [("X", 2.0), ("V", -1.0)]
    f = eps[0][3]
    # control: median 24h short return 4% x 6h / 24h = 1% drift; 1R = 5% of the entry: market = 0.2R
    assert f["market_r"] == pytest.approx(0.2) and f["adj"] == pytest.approx(1.8) and f["control_n"] == 5
    assert eps[1][3]["adj"] is None
    assert {r[0] for r in j.db.execute("SELECT source FROM pattern_episodes")} == {"live"}


# ------------------------------------------------------------------ decay (finding F)
def test_decay_alarm_is_day_clustered_not_coin_clustered():
    d = sqlite3.connect(":memory:")
    now = T0 + 20 * DAY
    _node(d, pid="setup.x")
    d.execute("UPDATE patterns SET status='live'")
    # 40 coins over 10 days; the day shock dominates (+7 / -3): coin-clustered CI > 0, day-clustered t < 2
    R.add_episodes(d, "setup.x", [{"symbol": f"C{i}", "ts_ms": now - (i % 10 + 1) * DAY, "source": "scan",
                                   "oos": True, "outcome": (7.0 if (i % 10) % 2 else -3.0) + 0.3 * (i // 10)}
                                  for i in range(40)])
    c = R.card(d, "setup.x", now)
    assert c["last_90d"]["ci95"][0] > 0 and c["last_90d"]["t_day"] < 2
    assert c["forward_90d"]["days"] == 10 and c["forward_90d"]["ci95"][0] <= 0
    assert c["verdict"].startswith("probation") and R.decay_alarms(d, now)[0]["id"] == "setup.x"


def test_probation_retires_on_a_negative_adjusted_mean_over_20_days():
    d = sqlite3.connect(":memory:")
    now = T0 + 30 * DAY
    _node(d, pid="setup.x")
    d.execute("UPDATE patterns SET status='probation'")
    _add(d, [[-1.0 + (0.5 if k % 2 else -0.5), 0.2] for k in range(20)], start_day=5, pid="setup.x", raw=4.0)
    c = R.card(d, "setup.x", now)
    assert c["forward_90d"]["basis"] == "adj" and c["forward_90d"]["mean"] < 0      # raw says +4%: not used
    assert c["verdict"] == "retire: edge gone"


def test_forward_report_and_the_read_coin_card(monkeypatch):
    d = _fwd_db(monkeypatch)
    _node(d, pid="veto.x", metric="blocked_vs_allowed_r")
    R.register(d, {"id": "tape.draft", "level": "tape", "definition": "d", "metric": SCAN_METRIC})
    _add(d, _days(0.5))
    rep = F.report(d)
    assert rep["family"]["M"] == 1 and rep["no_forward_feed"] == ["veto.x"]
    assert [n["id"] for n in rep["nodes"]] == ["setup.pump_fade"]                      # drafts are left out
    assert rep["nodes"][0]["forward"]["decision"] == "pass" and rep["nodes"][0]["plan"]["target_n"] == 40
    assert F.report(d, "veto.x")["nodes"][0]["forward"]["status"] == "no_feed"
    (card,) = R.node_cards(d, ["setup.pump_fade"])
    assert card["forward"] == {"status": "decided", "decision": "pass", "p": card["forward"]["p"], "mean": 1.0}


async def test_research_report_carries_each_nodes_forward_status(monkeypatch):
    from types import SimpleNamespace

    from shortbot import mcp_research
    fresh = Journal(":memory:")                                    # no registry tables yet
    monkeypatch.setattr(mcp_research, "get_app", lambda: SimpleNamespace(journal=fresh))
    out = (await mcp_research.research_report())["forward"]
    assert out["nodes"] == [] and out["family"]["M"] == 1 and "how_to_read" in out
    j = Journal(":memory:")
    _at(monkeypatch, REG)
    _node(j.db, plan={"target_n": 40})
    _add(j.db, _days(0.5))
    monkeypatch.setattr(mcp_research, "get_app", lambda: SimpleNamespace(journal=j))
    (node,) = (await mcp_research.research_report())["forward"]["nodes"]
    assert node["id"] == "setup.pump_fade" and node["forward"]["decision"] == "pass"
    assert "plan" not in node                                      # the card has it (registry card)
