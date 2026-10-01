import math

import pytest

from shortbot.probability import baseline as b

SIG_H = 1.0 / math.sqrt(365 * 24)   # 100%/yr expressed per hour


def test_three_way_matches_verified_reference():
    r = b.three_way(1.0, 1.03, 0.98, SIG_H, 4.0)
    assert r["up"] == pytest.approx(0.1655, abs=2e-3)
    assert r["down"] == pytest.approx(0.3442, abs=2e-3)
    assert r["time"] == pytest.approx(0.4902, abs=2e-3)


def test_zero_drift_race_is_pure_geometry():
    # optional stopping: P(up first) = ln(P0/L)/ln(U/L), independent of sigma
    p = b.race_up_first(100, 110, 95, 0.05)
    assert p == pytest.approx(math.log(100 / 95) / math.log(110 / 95))


def test_touch_reflection_rule_and_monotonicity():
    p = b.touch_up(1.0, 1.03, SIG_H, 4.0)
    assert p == pytest.approx(2 * b.phi(-math.log(1.03) / (SIG_H * 2)), rel=1e-9)
    assert b.touch_up(1.0, 1.05, SIG_H, 4.0) < p < b.touch_up(1.0, 1.03, SIG_H, 24.0)
    assert b.touch_down(1.0, 0.97, SIG_H, 4.0) == pytest.approx(b.touch_up(1.0, 1 / 0.97, SIG_H, 4.0))


def test_long_horizon_three_way_converges_to_race():
    r = b.three_way(1.0, 1.05, 0.90, SIG_H * 5, 24 * 365)
    assert r["time"] < 1e-6
    assert r["up"] == pytest.approx(b.race_up_first(1.0, 1.05, 0.90, SIG_H * 5), abs=1e-6)


def test_short_geometry_has_no_edge_without_drift():
    g = b.short_geometry(entry=1.0, stop=1.05, target=0.90, sigma=SIG_H * 5, t=24 * 365)
    assert g["ev_R_no_edge"] == pytest.approx(0.0, abs=0.01)   # R:R alone creates no edge
    assert g["p_target"] == pytest.approx(0.05 / 0.15, abs=2e-3)  # price martingale: stop/(stop+target)


def test_bgk_moves_barrier_away():
    assert b.bgk_shift(1.03, 1.0, SIG_H, 1.0) > 1.03 and b.bgk_shift(0.97, 1.0, SIG_H, 1.0) < 0.97


def test_montecarlo_matches_closed_form_on_gaussian_history():
    import numpy as np

    from shortbot.probability.montecarlo import simulate_short
    rng = np.random.default_rng(1)
    sig_step = 0.004
    closes = list(np.exp(np.cumsum(rng.normal(0, sig_step, 3000))))
    sig_h = sig_step * math.sqrt(12)
    mc = simulate_short(closes, 1.0, 1.03, [0.97], 6.0, sig_h, n_paths=8000)
    ref = b.short_geometry(1.0, 1.03, 0.97, sig_h, 6.0)
    t = mc["targets"][0]
    assert t["p_target_first"] == pytest.approx(ref["p_target"], abs=0.03)
    assert t["p_stop_first"] == pytest.approx(ref["p_stop"], abs=0.03)


def test_fat_tails_change_hit_odds_but_not_ev():
    import numpy as np

    from shortbot.probability.montecarlo import simulate_short
    rng = np.random.default_rng(3)
    closes = list(np.exp(np.cumsum(rng.standard_t(2.5, 4000) * 0.004)))   # very fat tails
    r = simulate_short(closes, 1.0, 1.05, [0.90], 48.0, 0.02, n_paths=8000)["targets"][0]
    assert abs(r["ev_R"]) < 4 * r["ev_R_se"] + 0.03        # no edge from tails
    assert r["avg_stop_loss_R"] >= 1.0                        # overshoot = slippage at the stop


def test_vol_scaled_bracket_positive_at_extreme_vol():
    from shortbot.probability import vol_scaled_bracket
    stop, target = vol_scaled_bracket(100.0, 0.25, 24)      # 25%/h: linear bracket would go negative
    assert target > 0 and stop > 100 and b.short_geometry(100, stop, target, 0.25, 24)["p_target"] > 0
