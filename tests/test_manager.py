from datetime import datetime, timedelta, timezone

from shortbot.manager import plan_actions

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def trade(**plan):
    base_plan = {"auto": {"breakeven": True, "trail": True, "targets": False, "time_stop": False}}
    base_plan.update(plan)
    return {"entry_price": 1.0, "amount": 100, "opened_at": (NOW - timedelta(hours=2)).isoformat(),
            "setup": "pump_fade", "plan": base_plan, "targets_hit": [], "mfe_pct": 0, "mae_pct": 0,
            "low_since_entry": 1.0}


def pos(mark, stop=1.05, contracts=100):
    return {"mark": mark, "exchange_stop": stop, "contracts": contracts}


def kinds(acts):
    return [a.kind for a in acts]


def test_breakeven_moves_stop_below_entry():
    t = trade(breakeven_at_pct=5)
    t["entry_price"] = 1.03   # away from round numbers
    mv = [a for a in plan_actions(t, pos(0.97), NOW) if a.kind == "move_stop"][0]
    assert mv.price == 1.03 * 0.999 and mv.reason.startswith("breakeven")


def test_trailing_uses_low_since_entry_and_only_tightens():
    t = trade(trail_activate_pct=10, trail_pct=4)
    t["low_since_entry"] = 0.85
    mv = [a for a in plan_actions(t, pos(0.86), NOW) if a.kind == "move_stop"][0]
    assert abs(mv.price - 0.884) < 1e-9
    assert "move_stop" not in kinds(plan_actions(t, pos(0.86, stop=0.87), NOW))  # already tighter


def test_targets_alert_or_execute_on_original_size():
    t = trade(targets=[{"price": 0.9, "fraction": 0.5}])
    assert [a for a in plan_actions(t, pos(0.89), NOW) if a.target_price == 0.9][0].kind == "alert"
    t["plan"]["auto"]["targets"] = True
    c = [a for a in plan_actions(t, pos(0.89, contracts=100), NOW) if a.kind == "close"][0]
    assert c.fraction == 0.5 and c.target_price == 0.9
    t["targets_hit"] = [0.9]
    assert "close" not in kinds(plan_actions(t, pos(0.89), NOW))


def test_manual_partial_counts_toward_targets_and_keeps_the_runner():
    """ONDO #5: half closed by hand, then T1 (50%) must NOT close the rest; T2 (cumulative 100%) closes it."""
    t = trade(targets=[{"price": 0.9, "fraction": 0.5}, {"price": 0.8, "fraction": 0.5}])
    t["plan"]["auto"]["targets"] = True
    t["amount"] = 100
    acts = plan_actions(t, pos(0.89, contracts=50), NOW)            # 50 of 100 already bought back manually
    assert "close" not in kinds(acts) and any(a.kind == "target_done" and a.target_price == 0.9 for a in acts)
    t["targets_hit"] = [0.9]
    c = [a for a in plan_actions(t, pos(0.79, contracts=50), NOW) if a.kind == "close"][0]
    assert c.fraction == 1.0 and c.target_price == 0.8


def test_both_targets_hit_at_once_close_in_steps():
    t = trade(targets=[{"price": 0.9, "fraction": 0.5}, {"price": 0.8, "fraction": 0.5}])
    t["plan"]["auto"]["targets"] = True
    t["amount"] = 100
    closes = [a for a in plan_actions(t, pos(0.79, contracts=100), NOW) if a.kind == "close"]
    assert [round(a.fraction, 3) for a in closes] == [0.5, 1.0]


def test_time_stop_alert_vs_auto_close():
    t = trade(time_stop_hours=1)
    assert any(a.urgent and "time stop" in a.reason for a in plan_actions(t, pos(1.0), NOW))
    t["plan"]["auto"]["time_stop"] = True
    acts = plan_actions(t, pos(1.0), NOW)
    assert kinds(acts) == ["close"] and acts[0].fraction == 1.0


def test_no_stop_move_when_it_would_sit_at_or_below_mark():
    t = trade(breakeven_at_pct=0.05)
    assert "move_stop" not in kinds(plan_actions(t, pos(0.9995), NOW))


def test_breakeven_stop_never_rests_in_a_round_number_zone():
    # entry 1.0 (a round): plain breakeven 0.999 sits right under it and gets tagged by a mere touch of 1.0;
    # just past the zone costs 0.35% (< 0.5% cap), so the stop goes there
    mv = [a for a in plan_actions(trade(breakeven_at_pct=5), pos(0.94), NOW) if a.kind == "move_stop"][0]
    assert 1.003 < mv.price <= 1.005 and "above round" in mv.reason


def test_breakeven_clears_above_round_when_cheap():
    # entry 1.648 -> breakeven 1.6464; not in 1.65's zone (1.6467+), so unchanged
    t = trade(breakeven_at_pct=3)
    t["entry_price"] = 1.648
    mv = [a for a in plan_actions(t, pos(1.59, stop=1.72), NOW) if a.kind == "move_stop"][0]
    assert abs(mv.price - 1.648 * 0.999) < 1e-9
    # entry 1.6495 -> breakeven 1.6479 is in 1.65's zone; just past it (1.655 + tick) gives back ~0.6% > cap
    t["entry_price"] = 1.6495
    mv = [a for a in plan_actions(t, pos(1.59, stop=1.72), NOW) if a.kind == "move_stop"][0]
    assert mv.price < 1.65 * 0.998 and "under round" in mv.reason
    # entry 1.652 -> breakeven 1.6503 is just past 1.65; clearing above (1.6555) is within 0.5% of entry
    t["entry_price"] = 1.652
    mv = [a for a in plan_actions(t, pos(1.59, stop=1.72), NOW) if a.kind == "move_stop"][0]
    assert 1.65 * 1.003 < mv.price <= 1.652 * 1.005 and "above round" in mv.reason


def test_dense_grids_are_not_treated_as_levels():
    from shortbot.tape_math import round_zone
    assert round_zone(0.884) == 0.885          # raw grid: every 0.57%
    assert round_zone(0.884, 1.0) is None      # tier-2 grid 0.88/0.89: clear
    assert round_zone(0.8898, 1.0) == 0.89


def test_manual_partial_marks_a_non_auto_target_done_instead_of_nagging():
    t = trade(targets=[{"price": 0.9, "fraction": 0.5}, {"price": 0.8, "fraction": 0.5}])
    acts = plan_actions(t, pos(0.89, contracts=50), NOW)          # human already took half
    assert [a.kind for a in acts if a.target_price == 0.9] == ["target_done"]


def test_funding_budget_exits_a_non_working_short_that_paid_half_its_risk():
    t = trade()
    t.update(initial_stop=1.05, amount=100, funding=-2.6)       # 1R = 5 USDT; paid 2.6 >= 0.5R
    acts = plan_actions(t, pos(1.01), NOW)                       # price against us: no follow-through
    assert acts[0].kind == "close" and "funding budget" in acts[0].reason
    assert not [a for a in plan_actions(t, pos(0.95), NOW) if "funding" in a.reason]   # working: keep it
    t["plan"]["auto"]["funding_budget"] = False
    assert plan_actions(t, pos(1.01), NOW)[0].kind == "alert"
