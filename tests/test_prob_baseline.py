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


def test_mc_grid_caps_memory_paths_first_then_horizon():
    from shortbot.probability.montecarlo import MC_MAX_CELLS, MC_MIN_PATHS, mc_grid
    assert mc_grid(8000, 576) == (8000, 576)                  # 48 h: under the cap, untouched
    p, s = mc_grid(10_000, 600)                               # just over: fewer paths, full horizon
    assert s == 600 and p * s <= MC_MAX_CELLS and p >= MC_MIN_PATHS
    p, s = mc_grid(10_000, 750 * 12)                          # KITE's 750 h unlock time stop (2026-10-04 OOM)
    assert p == MC_MIN_PATHS and p * s <= MC_MAX_CELLS and s < 750 * 12


def test_long_time_stop_simulates_capped_horizon_without_blowing_memory():
    import tracemalloc

    import numpy as np

    from shortbot.probability.montecarlo import simulate_short
    rng = np.random.default_rng(5)
    closes = list(np.exp(np.cumsum(rng.normal(0, 0.004, 2000))))
    tracemalloc.start()
    r = simulate_short(closes, 1.0, 1.07, [0.90, 0.80], 750.0, 0.01)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert r["ok"] and r["horizon_capped"] and r["hours_simulated"] < 750
    assert peak < 1_000_000_000                               # uncapped: ~5 GB


# --- long side: the mirror of the short bracket (stop below, target above) ---------------------------------------

def test_short_is_the_default_side_and_unknown_sides_are_refused():
    assert b.short_geometry(1.0, 1.03, 0.97, SIG_H, 6.0) == b.short_geometry(1.0, 1.03, 0.97, SIG_H, 6.0, side="short")
    with pytest.raises(ValueError):
        b.short_geometry(1.0, 1.03, 0.97, SIG_H, 6.0, side="buy")


def test_long_geometry_has_no_edge_without_drift():
    g = b.short_geometry(entry=1.0, stop=0.95, target=1.10, sigma=SIG_H * 5, t=24 * 365, side="long")
    assert g["ev_R_no_edge"] == pytest.approx(0.0, abs=0.01)   # R:R alone creates no edge for a long either
    assert g["p_target"] == pytest.approx(0.05 / 0.15, abs=2e-3)  # price martingale: stop/(stop+target)
    assert g["reward_R"] == pytest.approx(2.0)


def test_long_geometry_mirrors_the_three_way_barriers():
    g = b.short_geometry(1.0, 0.98, 1.03, SIG_H, 4.0, side="long")
    tw = b.three_way(1.0, 1.03, 0.98, SIG_H, 4.0, -(SIG_H**2) / 2)
    assert (g["p_target"], g["p_stop"], g["p_time"]) == (tw["up"], tw["down"], tw["time"])
    assert g["p_target_no_time_limit"] == pytest.approx(b.race_up_first(1.0, 1.03, 0.98, SIG_H, -(SIG_H**2) / 2))


def test_montecarlo_long_matches_closed_form_on_gaussian_history():
    import numpy as np

    from shortbot.probability.montecarlo import simulate_short
    rng = np.random.default_rng(1)
    sig_step = 0.004
    closes = list(np.exp(np.cumsum(rng.normal(0, sig_step, 3000))))
    sig_h = sig_step * math.sqrt(12)
    mc = simulate_short(closes, 1.0, 0.97, [1.03], 6.0, sig_h, n_paths=8000, side="long")
    ref = b.short_geometry(1.0, 0.97, 1.03, sig_h, 6.0, side="long")
    t = mc["targets"][0]
    assert t["p_target_first"] == pytest.approx(ref["p_target"], abs=0.03)
    assert t["p_stop_first"] == pytest.approx(ref["p_stop"], abs=0.03)


def test_fat_tails_change_long_hit_odds_but_not_ev():
    import numpy as np

    from shortbot.probability.montecarlo import simulate_short
    rng = np.random.default_rng(3)
    closes = list(np.exp(np.cumsum(rng.standard_t(2.5, 4000) * 0.004)))
    r = simulate_short(closes, 1.0, 0.95, [1.10], 48.0, 0.02, n_paths=8000, side="long")["targets"][0]
    assert abs(r["ev_R"]) < 4 * r["ev_R_se"] + 0.03        # no edge from tails
    assert r["avg_stop_loss_R"] >= 1.0                        # overshoot below a long's stop = slippage


def test_montecarlo_long_orders_targets_nearest_first_and_short_is_unchanged():
    import numpy as np

    from shortbot.probability.montecarlo import simulate_short
    closes = list(np.exp(np.cumsum(np.random.default_rng(2).normal(0, 0.004, 2000))))
    long = simulate_short(closes, 1.0, 0.97, [1.06, 1.03], 12.0, 0.014, n_paths=2000, side="long")
    assert [t["target"] for t in long["targets"]] == [1.03, 1.06]
    assert long["targets"][0]["p_target_first"] > long["targets"][1]["p_target_first"]
    short = simulate_short(closes, 1.0, 1.03, [0.97, 0.94], 12.0, 0.014, n_paths=2000)
    assert short == simulate_short(closes, 1.0, 1.03, [0.97, 0.94], 12.0, 0.014, n_paths=2000, side="short")
