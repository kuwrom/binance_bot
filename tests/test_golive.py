"""The go-live gate (#46 §4, golive.py): read-only criteria over the demo journal since max(EPOCH, uptime_since).
A desk that meets every one is READY; an empty desk names every blocker; each gated criterion fails on its own when
only its evidence is broken; drill trades never count; the report never writes."""

import json
from datetime import timedelta

import pytest

from shortbot import golive
from shortbot import setup_ladder as L
from shortbot.config import RiskLimits
from shortbot.journal import Journal
from tests import golive_rig as rig
from tests.golive_rig import NOW

GATED = ["registry", "sample", "expectancy", "stress", "drawdown", "equity", "uptime", "incidents", "slippage",
         "plumbing"]


def run(j, **kw):
    return golive.report(j, RiskLimits(), now=NOW, **kw)


def test_every_criterion_cites_its_threshold_and_source():
    rep = run(Journal(":memory:"))
    assert set(rep["criteria"]) == {*GATED, "parameters"}
    for name, c in rep["criteria"].items():
        assert {"ok", "value", "threshold", "source", "detail", "gated", "summary"} <= set(c), name
        assert c["source"] and c["threshold"], name
    assert rep["criteria"]["parameters"]["gated"] is False
    assert [k for k, c in rep["criteria"].items() if c["gated"]] == GATED


def test_an_empty_desk_is_not_ready_and_names_every_blocker():
    rep = run(Journal(":memory:"))
    assert rep["verdict"] == "NOT READY"
    assert rep["blockers"] == [k for k in GATED if k != "incidents"]    # no alert at all is no incident
    assert rep["window"]["from"] == golive.EPOCH and rep["window"]["uptime_since"] is None
    assert "start at minimum size" in " ".join(rep["owner_steps"]).lower()


def test_a_desk_that_meets_every_criterion_is_ready(monkeypatch):
    rep = run(rig.desk(monkeypatch))
    assert rep["blockers"] == [] and rep["verdict"] == "READY", {k: c["summary"] for k, c in rep["criteria"].items()}
    c = rep["criteria"]
    assert c["registry"]["value"] == ["pump_fade"]
    assert c["sample"]["value"]["n"] == 60 and c["sample"]["value"]["days"] == 30
    assert c["sample"]["value"]["n_eff"] >= golive.MIN_N_EFF
    assert c["expectancy"]["value"]["lower"] > 0 and c["stress"]["value"] > 0
    assert c["incidents"]["value"] == 0 and c["incidents"]["detail"]["near_misses"] == {"stop_restored": 1}
    assert c["uptime"]["value"] == 0 and c["slippage"]["ok"]
    assert rep["window"]["from"] == rig.SINCE


def test_the_report_never_writes(monkeypatch):
    j = rig.desk(monkeypatch)
    before = j.db.total_changes
    run(j)
    run(j, light=True)
    assert j.db.total_changes == before


def test_drill_trades_never_count(monkeypatch):
    j = rig.desk(monkeypatch)
    for i in range(5):   # a run of drill losses that would break the sample, the stress and the drawdown
        rig.trade(j, -3.0, rig.DAY0 + timedelta(days=i), rig.DAY0 + timedelta(days=i, hours=1), setup="exp_drill",
                  fees=-50.0)
    rep = run(j)
    assert rep["verdict"] == "READY" and rep["criteria"]["sample"]["value"]["n"] == 60


def test_trades_before_the_window_never_count(monkeypatch):
    j = rig.desk(monkeypatch)
    rig.trade(j, -9.0, rig.DAY0 - timedelta(days=5), rig.DAY0 - timedelta(days=5, hours=-1))   # before the epoch
    assert run(j)["criteria"]["sample"]["value"]["n"] == 60
    j.set("uptime_since", "2026-10-20T00:00:00+00:00")    # uptime recorded later: the window starts there
    rep = run(j)
    assert rep["window"]["from"] == "2026-10-20T00:00:00+00:00"
    assert rep["criteria"]["sample"]["value"]["n"] < 60


# ------------------------------------------------------------------ each gated criterion fails on its own
def blockers(j, **kw):
    return run(j, **kw)["blockers"]


def test_registry_fails_alone_when_no_setup_passed_its_forward_test(monkeypatch):
    j = rig.desk(monkeypatch, adj=(-0.4, 0.1, -0.2))
    assert blockers(j) == ["registry"]
    c = run(j)["criteria"]["registry"]
    assert c["detail"]["pump_fade"]["eligible"] is False and c["detail"]["pump_fade"]["decision"] == "fail"


def test_sample_fails_alone_below_20_entry_days(monkeypatch):
    j = rig.desk(monkeypatch, entry_div=4)          # 60 trades on 15 entry days
    assert blockers(j) == ["sample"]


def test_expectancy_fails_alone_when_mainnet_prices_would_have_eaten_the_edge(monkeypatch):
    j = rig.desk(monkeypatch, entry=("limit", -150.0))   # demo filled 150 bp above the mainnet price
    rep = run(j)
    assert rep["blockers"] == ["expectancy"]
    v = rep["criteria"]["expectancy"]["value"]
    assert v["basis"] == "mainnet" and v["demo"]["lower"] > 0 and v["mainnet"]["lower"] < 0


def test_stress_fails_alone_when_1_5x_fees_and_2x_slippage_turn_the_mean(monkeypatch):
    j = rig.desk(monkeypatch, fees=-12.0)
    assert blockers(j) == ["stress"]


def test_drawdown_fails_alone_past_the_setup_ladder_halve_level(monkeypatch):
    j = rig.desk(monkeypatch)
    monkeypatch.setitem(L.PRIORS, "pump_fade", (0.5, 1.0))
    assert blockers(j) == ["drawdown"]
    assert blockers(j, light=True) == ["drawdown"]


def test_equity_fails_alone_at_the_governors_halve_tier(monkeypatch):
    j = rig.desk(monkeypatch)
    j.set("eq_mark:day:2026-10-20", json.dumps({"equity": 900.0, "ts": 0}))
    c = run(j)["criteria"]["equity"]
    assert blockers(j) == ["equity"] and c["value"] >= 8.0 and c["threshold"].startswith("< 8.0")


def test_uptime_fails_alone_on_an_untagged_gap_but_not_on_a_drill_one(monkeypatch):
    j = rig.desk(monkeypatch)
    rig.gap(j, rig.DAY0 + timedelta(days=2), 3600.0, tag="drill")     # the restart drill's planned outage
    rig.gap(j, rig.DAY0 + timedelta(days=4), 590.0)                   # under the watchdog's limit
    assert blockers(j) == []
    rig.gap(j, rig.DAY0 + timedelta(days=6), 900.0)
    assert blockers(j) == ["uptime"]
    assert run(j)["criteria"]["uptime"]["value"] == 900.0


def test_uptime_fails_while_the_guardian_is_down_now(monkeypatch):
    j = rig.desk(monkeypatch)
    j.set("guardian_heartbeat", str(NOW.timestamp() - 1200))
    assert blockers(j) == ["uptime"]


def test_incidents_fail_alone_on_one_unprotected_alert(monkeypatch):
    j = rig.desk(monkeypatch)
    rig.alert(j, "NAKED_POSITION", rig.DAY0 + timedelta(days=9), "C3/USDT:USDT")
    rig.alert(j, "NAKED_POSITION", rig.DAY0 - timedelta(days=9), "C3/USDT:USDT")   # before the window
    c = run(j)["criteria"]["incidents"]
    assert blockers(j) == ["incidents"] and c["value"] == 1 and c["detail"]["by_kind"] == {"NAKED_POSITION": 1}


def test_slippage_fails_alone_when_the_fills_cannot_calibrate_the_model(monkeypatch):
    j = rig.desk(monkeypatch, entry=("market", -1.0), exit_bps=-1.0)   # k <= 0: no calibration
    assert blockers(j) == ["slippage"]


def test_plumbing_fails_alone_when_a_drill_is_older_than_30_days(monkeypatch):
    j = rig.desk(monkeypatch)
    j.db.execute("DELETE FROM kv WHERE k='backup_drill_last_ok'")
    j.db.commit()
    rig.plumbing(j, age_days=31)
    assert blockers(j) == ["plumbing"]


# ------------------------------------------------------------------ the estimators
def test_mainnet_repricing_moves_r_by_the_excess_over_the_model():
    j = Journal(":memory:")
    rig.trade(j, 1.0, rig.DAY0, rig.DAY0 + timedelta(hours=2), entry=("limit", -50.0), exit_bps=0.0)
    rig.trade(j, 1.0, rig.DAY0, rig.DAY0 + timedelta(hours=2), refs=False)
def test_slippage_calibrates_on_the_windows_fills_only(monkeypatch):
    j = rig.desk(monkeypatch)
    before = run(j)["criteria"]["slippage"]
    for i in range(40):   # an older stack's demo fills, before the window: with them k would be <= 0
        t0 = rig.DAY0 - timedelta(days=30 - i * 0.5)
        rig.trade(j, 0.5, t0, t0 + timedelta(hours=2), entry=("market", -40.0), exit_bps=-40.0)
    c = run(j)["criteria"]["slippage"]
    assert c["ok"] and c["value"] == before["value"] and c["detail"]["n_trades"] == 60
    assert c["detail"]["since"] == rig.SINCE


    a, b = golive.sample(j, "")
    model_exit = 20 / (rig.VOL / 1e6) ** 0.5
    assert a["excess_bps"] == pytest.approx(-50.0 - model_exit, abs=0.02)
    assert a["r_main"] == pytest.approx(1.0 + a["excess_bps"] * 100 / 1e4 / 5, abs=1e-3) and a["priced"]
    assert b["r_main"] == b["r"] == pytest.approx(1.0) and not b["priced"]
    assert a["r_stress"] < a["r_main"] and b["r_stress"] < b["r"]   # 1.5x fees, 2x slippage


def test_parameter_match_is_reported_never_gated(monkeypatch):
    j = rig.desk(monkeypatch)
    base = RiskLimits().__dict__
    rig.trade(j, 0.5, rig.DAY0 + timedelta(days=3), rig.DAY0 + timedelta(days=3, hours=3),
              limits={**base, "max_leverage": 5})
    rep = run(j)
    p = rep["criteria"]["parameters"]
    assert p["gated"] is False and p["detail"]["differs"] == {"max_leverage": [5]}
    assert p["value"] == "0/1 trades sized under the .env baseline (60 without recorded limits)"
