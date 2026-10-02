import json
import sqlite3
from dataclasses import replace

import pytest

from shortbot import setup_ladder as L
from shortbot.config import RiskLimits
from shortbot.journal import Journal
from shortbot.risk_budget import BudgetInputs, _ladder_fallback, compute_budget


class J:
    """Journal stand-in: closed_results(), kv get/set (demo keys), candidates(), alerts; `db` holds the kv table the
    stop alert's once-only marker is claimed in."""

    mode = "demo"

    def __init__(self, rows=(), cands=()):
        self.rows, self.kv, self.cands, self.alerts = list(rows), {}, list(cands), []
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)")

    def closed_results(self): return self.rows
    def get(self, k): return self.kv.get(k)

    def set(self, k, v):                     # mirrors the real kv table, which auto_reenable lists ladders from
        self.kv[k] = v
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (k, v))
    def mode_key(self, k): return k
    def candidates(self, labelled_only=False, limit=5000): return self.cands
    def log_alert(self, kind, message, symbol=None): self.alerts.append((kind, message))


GOOD = [{"day": f"2026-10-{1 + d:02d}", "ret": 0.2 + 0.1 * (d % 4) + 0.05 * k} for d in range(10) for k in range(3)]


def rows(rs, setup="pump_fade", start=1):
    return [{"setup": setup, "r_multiple": r, "opened_at": f"2026-10-{start + k // 24:02d}T{k % 24:02d}:00",
             "closed_at": f"2026-10-{start + k // 24:02d}T{k % 24:02d}:30"} for k, r in enumerate(rs)]


def tr(rs):
    return [{"closed_at": f"2026-10-01T{k:02d}:00", "r": r} for k, r in enumerate(rs)]


def test_tv1_tv2_halve_then_shadow_with_the_prior_thresholds():
    assert L.thresholds("pump_fade", [0.1] * 10) == {"halve": 8.6, "stop": 12.3, "source": "prior", "n": 10}
    assert L.walk(tr([2] + [-1] * 8), 8.6, 12.3, [])["state"] == "NORMAL"
    w = L.walk(tr([2] + [-1] * 9), 8.6, 12.3, [])
    assert w["state"] == "HALF" and w["dd_r"] == 9.0
    assert L.walk(tr([2] + [-1] * 12), 8.6, 12.3, [])["state"] == "HALF"         # DD 12.0
    w = L.walk(tr([2] + [-1] * 13), 8.6, 12.3, [])
    assert w["state"] == "SHADOW" and w["stops_idx"] == [13]


def test_tv3_half_recovers_only_at_half_the_halve_threshold():
    assert L.walk(tr([2] + [-1] * 9 + [5]), 8.6, 12.3, [])["state"] == "NORMAL"   # DD 4.0 <= 4.3
    assert L.walk(tr([2] + [-1] * 9 + [4]), 8.6, 12.3, [])["state"] == "HALF"     # DD 5.0


def test_reenable_returns_at_half_with_a_reset_peak_and_a_new_stop_starts_a_new_cycle():
    seq = [2] + [-1] * 13                                     # stopped at index 13 (closed 13:00)
    w = L.walk(tr(seq), 8.6, 12.3, ["2026-10-01T13:30"])
    assert w["state"] == "HALF" and w["hwm_r"] == w["cum_r"] == -11.0
    w = L.walk(tr(seq + [-1] * 13), 8.6, 12.3, ["2026-10-01T13:30"])
    assert w["state"] == "SHADOW" and w["stops_idx"] == [13, 26]


def test_status_freezes_criteria_once_and_tv4_epoch_ignores_older_trades():
    j = J(rows([2] + [-1] * 13))
    st = L.status(j, "pump_fade")
    assert st["state"] == "SHADOW" and st["mult"] == 0.0 and "SHADOW" in st["reasons"][0]
    ev = json.loads(j.kv["setup_ladder:pump_fade"])
    assert len(ev["stops"]) == 1 and ev["stops"][0]["criteria"] == L.CRITERIA
    L.status(j, "pump_fade")
    assert len(json.loads(j.kv["setup_ladder:pump_fade"])["stops"]) == 1     # not re-recorded
    fresh = L.status(J(rows([2] + [-1] * 13)), "pump_fade", epoch="2026-10-02")
    assert fresh["state"] == "NORMAL" and fresh["thresholds"]["n"] == 0


def test_tv5_reenable_needs_the_frozen_evidence_and_a_human_in_live():
    j = J(rows([2] + [-1] * 13))
    L.status(j, "pump_fade")
    good = [{"day": f"2026-10-{1 + d:02d}", "ret": 0.2 + 0.1 * (d % 4) + 0.05 * k} for d in range(10) for k in range(3)]
    assert L.reenable(j, "pump_fade", good[:29], live=False)["ok"] is False              # 29 episodes
    assert L.reenable(j, "pump_fade", [{**e, "ret": -0.1} for e in good], live=False)["ok"] is False
    live = L.reenable(j, "pump_fade", good, live=True)
    assert live["ok"] is False and "human" in live["reason"]
    assert L.reenable(j, "pump_fade", good, live=False)["ok"] is True
    assert L.status(j, "pump_fade")["state"] == "HALF"


def test_tv6_bootstrap_from_50_trades_is_clamped_to_the_prior_band():
    th = L.thresholds("pump_fade", [(-1.0 if k % 3 == 0 else 0.6) for k in range(50)])
    assert th["source"] == "bootstrap" and 6.45 <= th["halve"] <= 12.9 and 9.22 <= th["stop"] <= 18.45
    assert L.thresholds("pump_fade", [0.1] * 49)["source"] == "prior"
    assert L.prior("unlock") == (9.6, 13.6)


def daily(rs, setup):
    return [{"setup": setup, "r_multiple": r, "opened_at": f"2026-{9 + d // 30:02d}-{1 + d % 30:02d}T01:00",
             "closed_at": f"2026-{9 + d // 30:02d}-{1 + d % 30:02d}T02:00"} for d, r in enumerate(rs)]


def test_variants_roll_up_by_daily_correlation_or_registry_overlap():
    a = daily([1, -1, 2] * 10, "pump_fade") + daily([1, -1, 2] * 10, "pump_fade_v2") + \
        daily([0.5, 0.7, -0.2, 0.1, 0.3] * 6, "other")
    assert L.family("pump_fade", a) == ["pump_fade", "pump_fade_v2"]
    assert L.family("pump_fade", a, overlaps={"other": 0.8}) == ["pump_fade", "other", "pump_fade_v2"]


def test_risk_budget_applies_the_ladder_multiplier_with_its_reason():
    lim = replace(RiskLimits(), risk_per_trade_pct=0.2)            # small enough that no slot/heat cap binds
    base = BudgetInputs(equity=1000, peak_equity=1000, regime="favorable")
    full = compute_budget(lim, base, "XYZ/USDT:USDT")
    half = compute_budget(lim, BudgetInputs(equity=1000, peak_equity=1000, regime="favorable",
                                                     ladder={"mult": 0.5, "reasons": ["setup ladder: half"]}),
                          "XYZ/USDT:USDT")
    shadow = compute_budget(lim, BudgetInputs(equity=1000, peak_equity=1000, regime="favorable",
                                                       ladder={"mult": 0.0, "reasons": ["setup ladder: SHADOW"]}),
                            "XYZ/USDT:USDT")
    assert full["multipliers"]["m_ladder"] == 1.0 and half["multipliers"]["m_ladder"] == 0.5
    assert half["risk_usd"] == pytest.approx(full["risk_usd"] / 2, abs=0.01) and "setup ladder: half" in half["reasons"]
    assert shadow["risk_usd"] == 0 and shadow["blocked"]


def test_tv4_an_unreleased_stop_survives_an_epoch_reset_and_a_new_registry_version():
    j = J(rows([2] + [-1] * 13))
    assert L.status(j, "pump_fade")["state"] == "SHADOW"
    newer = L.status(j, "pump_fade", epoch="2026-10-05")     # a detector change registers a new version
    assert newer["state"] == "SHADOW" and newer["mult"] == 0.0 and newer["thresholds"]["n"] == 0
    assert "stop predates the replay start: re-enable needs `shortbot-admin ladder-clear`" in newer["reasons"][0]
    L.reset_epoch(j, "pump_fade", "human", "reviewed")
    assert L.status(j, "pump_fade")["state"] == "SHADOW"
    assert L.reenable(j, "pump_fade", GOOD, live=False)["ok"] is True
    after = L.status(j, "pump_fade")                         # released: the new epoch's own replay sizes it
    assert after["state"] != "SHADOW" and after["mult"] > 0


def test_the_stop_record_is_per_account_a_demo_stop_is_invisible_in_live(tmp_path):
    demo, live = Journal(tmp_path / "j.db", "demo"), Journal(tmp_path / "j.db", "live")
    demo.closed_results = lambda all_modes=False: rows([2] + [-1] * 13)
    assert L.status(demo, "pump_fade")["state"] == "SHADOW"
    assert demo.get("setup_ladder:pump_fade") and live.get("setup_ladder:pump_fade@live") is None
    assert L.events(live, "pump_fade")["stops"] == [] and L.status(live, "pump_fade")["state"] == "NORMAL"
    assert _ladder_fallback(live, "pump_fade", RuntimeError("x"))["mult"] == 0.5        # not demo's stop -> 0
    assert L.reenable(live, "pump_fade", GOOD, live=True)["reason"] == "not stopped"
    assert L.events(demo, "pump_fade")["reenables"] == []


def test_a_stop_alerts_once_however_many_readers_see_it():
    j = J(rows([2] + [-1] * 13))
    for _ in range(3):                                       # brief, guardian, sizing
        L.status(j, "pump_fade")
    assert [k for k, _ in j.alerts] == ["setup_ladder_stop"]
    assert "pump_fade" in j.alerts[0][1] and "13.0R" in j.alerts[0][1] and "ladder-status" in j.alerts[0][1]
    racer = J(rows([2] + [-1] * 13))                         # another process saw the same stop before the record
    racer.db = j.db
    assert L.status(racer, "pump_fade")["state"] == "SHADOW" and racer.alerts == []


def test_ladder_failure_fails_closed_from_the_frozen_stop_record():
    j = J(rows([2] + [-1] * 13))
    assert _ladder_fallback(j, "pump_fade", RuntimeError("x"))["mult"] == 0.5          # nothing recorded: half
    L.status(j, "pump_fade")                                                           # records the stop
    assert _ladder_fallback(j, "pump_fade", RuntimeError("x"))["mult"] == 0.0          # unreleased stop: 0
    ev = json.loads(j.kv["setup_ladder:pump_fade"])
    ev["reenables"] = [ev["stops"][-1]["at"] + "Z"]
    j.kv["setup_ladder:pump_fade"] = json.dumps(ev)
    assert _ladder_fallback(j, "pump_fade", RuntimeError("x"))["mult"] == 0.5          # re-enabled since: half


def _released(j, setup="pump_fade"):
    """Stop the ladder, then re-enable it right after the stopping trade."""
    L.status(j, setup)
    ev = json.loads(j.kv[L.key(j, setup)])
    ev["reenables"] = [ev["stops"][-1]["at"] + "Z"]
    j.kv[L.key(j, setup)] = json.dumps(ev)


def test_probation_holds_half_until_plus_half_the_halve_distance_since_reenable():
    seq = [2] + [-1] * 13                                     # stop at the 14th trade (prior halve 8.6, stop 12.3)
    j = J(rows(seq))
    _released(j)
    assert L.status(j, "pump_fade")["state"] == "HALF"        # re-enabled, nothing earned yet
    j.rows = rows(seq + [1.0])
    st = L.status(j, "pump_fade")
    assert st["state"] == "HALF" and st["probation"]["net_r"] == 1.0 and st["probation"]["need_r"] == 4.3
    j.rows = rows(seq + [1.0] * 5)                            # +5.0 >= 4.3 since the re-enable
    assert L.status(j, "pump_fade")["state"] == "NORMAL"


def test_probation_survives_an_epoch_reset_and_a_new_stop_after_reenable_is_shadow():
    seq = [2] + [-1] * 13
    j = J(rows(seq + [1.0]))
    _released(j)
    assert L.status(j, "pump_fade", epoch="2026-10-09")["state"] == "HALF"     # epoch after everything: still on probation
    j2 = J(rows(seq + [-1.0] * 13))
    _released(j2)
    assert L.status(j2, "pump_fade")["state"] == "SHADOW"                       # -13R after the re-enable: new stop


def test_auto_reenable_reports_per_setup_and_never_raises():
    j = J(rows([2] + [-1] * 13))
    L.status(j, "pump_fade")
    out = L.auto_reenable(j, live=False)
    assert out and out[0]["setup"] == "pump_fade" and out[0]["ok"] is False and "criteria" in out[0]["reason"]
