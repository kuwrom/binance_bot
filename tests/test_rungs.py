import pytest

from shortbot.backtest import rungs as RG
from shortbot.backtest.sim import FREE, Plan, simulate

H = 3_600_000


def bars(path):
    return [{"ts": k * H, "o": p, "h": p * 1.001, "l": p * 0.999, "c": p, "qv": 2e7} for k, p in enumerate(path)]


def test_rungs_are_carved_from_targets_or_added_to_a_runner():
    plan = Plan(stop=110.0, targets=((95.0, 0.5), (90.0, 0.5)), time_stop=20)
    v = RG.variants(plan, 100.0)
    carved = v["targets+d6-12-20_s50"].targets
    assert carved[:2] == ((95.0, 0.25), (90.0, 0.25))
    assert [round(p, 2) for p, _ in carved[2:]] == [94.0, 88.0, 80.0] and sum(f for _, f in carved) == pytest.approx(1)
    assert v["runner"].targets == () and sum(f for _, f in v["runner+d4-8-12_s30"].targets) == pytest.approx(0.3)


def test_a_deep_wick_fills_rungs_that_the_runner_baseline_misses():
    path = [100.0] * 6 + [100.0, 99.0, 98.0] + [100.0] * 12
    rows = bars(path)
    rows[8] = {**rows[8], "l": 85.0}                          # a last-price wick to -15% inside one hour
    plan = Plan(stop=110.0, targets=(), time_stop=10, be_r=None)
    base = simulate(rows, 5, plan, costs=FREE)
    rung = simulate(rows, 5, RG.with_rungs(plan, 100.0, (0.06, 0.12, 0.20), 0.5, carve=False), costs=FREE)
    assert base["r"] == pytest.approx(0.0, abs=1e-9)
    assert rung["r"] == pytest.approx((0.15 * 6 + 0.15 * 12) / 10, rel=1e-6)   # R1, R2 filled; R3 (-20%) not
    out = RG._sim_all(rows, 5, plan, 100.0, None, costs=FREE)
    assert out["fill"]["runner+d6-12-20_s50"] and not out["fill"]["runner"]
    assert out["fill"]["runner+d10-20-30_s30"]                # -10% rung touched by the -15% wick


def test_fill_is_detected_when_a_rung_sits_on_a_target_price():
    path = [100.0] * 6 + [99.0, 97.0] + [100.0] * 12
    rows = bars(path)
    rows[7] = {**rows[7], "l": 88.0}                       # touches the -10% rung, which equals the 90 target
    plan = Plan(stop=125.0, targets=((90.0, 0.5), (80.0, 0.5)), time_stop=10, be_r=None)
    out = RG._sim_all(rows, 5, plan, 100.0, None, costs=FREE)
    assert out["fill"]["runner+d10-20-30_s50"] and out["fill"]["targets+d10-20-30_s50"]


def test_evaluate_pairs_each_signal_with_its_own_baseline():
    rows = [{"day": f"2026-09-{k:02d}", "group": "g", "r": {"targets": 0.1, "runner": 0.0, "runner+x": 0.2,
             "targets+x": 0.1}, "fill": {"targets": False, "runner": False, "runner+x": True, "targets+x": False}}
            for k in range(1, 11)]
    e = RG.evaluate(rows)["g"]
    assert e["runner+x"]["uplift"] == pytest.approx(0.2) and e["runner+x"]["fill_pct"] == 100.0
    assert e["targets+x"]["uplift"] == pytest.approx(0.0) and e["targets+x"]["uplift_when_filled"] is None
