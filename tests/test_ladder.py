from datetime import UTC, datetime, timedelta

from shortbot.ladder import decide, evaluate_ladder, exposure_target, ladder_levels, race_with_edge, state_score

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def test_race_reduces_to_martingale_and_edge_helps_the_short():
    assert abs(race_with_edge(0.02, 0.04, 0.0) - 1 / 3) < 1e-9
    assert race_with_edge(0.02, 0.04, 20) > 1 / 3 > race_with_edge(0.02, 0.04, -20)


def test_levels_are_nested_and_edge_moves_odds():
    t = ladder_levels(100, 104, [97], 0.03)
    assert t[0] == 97 and t[0] > t[1] > t[2]
    flat = evaluate_ladder(100, 104, t, 0.01, 12, 0.0)
    good = evaluate_ladder(100, 104, t, 0.01, 12, 2.0)
    assert flat[0]["p0"] >= flat[1]["p0"] >= flat[2]["p0"]
    assert all(g["p_model"] > f["p_model"] for g, f in zip(good, flat))
    assert all(x["ev_R"] < 0.05 for x in flat)            # no edge -> EV ~ -cost


def test_state_score_squeeze_and_tape():
    s, p = state_score({"funding_8h": -0.0001, "ti_z_1h": 2.5},
                       {"ret_1h_pct": 1.0, "oi_chg_1h_pct": 2.0, "game": "accepted_breakout"})
    assert s == -2 and {"squeeze_oi_price_up", "funding_negative", "taker_buy_burst", "tape_against"} <= set(p)
    s2, _ = state_score({"breadth_pct": 10}, {"game": "failed_sweep"})
    assert s2 == 1.5
    assert exposure_target(2) == 1 and exposure_target(0) == 0.5 and exposure_target(-2) == 0


def test_decide_needs_confirmation_gap_and_caps():
    iso = lambda m: (NOW - timedelta(minutes=m)).isoformat()  # noqa: E731
    assert decide([], 1.0, 0.9, NOW) == ("hold", 1.0)
    assert decide([], 1.0, 0.5, NOW)[0] == "wait"                               # first sighting: confirm
    h = [{"ts": iso(15), "x_star": 0.5, "x_virtual": 1.0, "action": "wait"}]
    assert decide(h, 1.0, 0.5, NOW) == ("reduce", 0.5)
    h2 = h + [{"ts": iso(10), "x_star": 0.5, "x_virtual": 0.5, "action": "reduce"}]
    assert decide(h2, 0.5, 0.0, NOW)[0] == "wait"                               # 30 min gap
    capped = [{"ts": iso(300), "x_star": 0, "x_virtual": 0.5, "action": "reduce"}] * 2
    assert decide(capped + h, 1.0, 0.5, NOW)[0] == "reduce_capped"


def test_path_r_and_costs():
    from shortbot.shadow_eval import path_r
    # short from 100, risk 10 per unit; price falls to 90 -> +1R holding full size
    assert abs(path_r([100, 95, 90], [1, 1, 1], 10) - 1.0) < 1e-9
    # halving at 95 costs a little and earns only half of the second leg
    r = path_r([100, 95, 90], [1, 0.5, 0.5], 10)
    assert 0.74 < r < 0.75
    # closed trade: last segment to the exit price
    assert abs(path_r([100], [1], 10, exit_price=110) + 1.0) < 1e-9


def test_placebo_and_adaptive_stop():
    from shortbot.ladder import adaptive_stop, placebo_score
    assert placebo_score(105, 100, 10) == -1.0 and placebo_score(90, 100, 10) == 2.0
    c = [[0, 0, 101, 99, 100, 1], [0, 0, 103, 99, 100, 1], [0, 0, 102, 98, 99, 1], [0, 0, 100, 97, 98, 1]]
    s, why = adaptive_stop(110, c, 2.0, 98)
    assert s == 104 and why                                          # swing 103 + 0.5*2, tighter than 110
    assert adaptive_stop(103.5, c, 2.0, 98)[0] == 103.5               # never loosens
