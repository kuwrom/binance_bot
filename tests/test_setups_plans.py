import pytest

from shortbot.plans import Target, build_plan, validate_plan
from shortbot.setups import SETUPS, detect_all, resolve


def test_resolve_registered_experimental_and_unknown():
    assert resolve("pump_fade").skill == "pump-fade"
    assert resolve("exp_weekend_fade").status == "experimental"
    with pytest.raises(ValueError, match="unknown setup"):
        resolve("yolo")


def test_detect_tags_candidates():
    m = {"symbol": "N/USDT:USDT", "change_24h": 40, "quote_volume": 1e8, "listing_age_days": 7,
         "delisting_scheduled": False, "spot_available": True}
    assert set(detect_all(m)) == {"pump_fade", "listing_fade"}
    d = {"symbol": "D/USDT:USDT", "change_24h": 2, "quote_volume": 1e8, "delisting_scheduled": True}
    assert detect_all(d) == ["delisting"]


def test_plan_defaults_overrides_and_validation():
    p = build_plan(SETUPS["pump_fade"].default_plan,
                   {"targets": [{"price": 0.9, "fraction": 0.5}], "auto": {"targets": True}})
    assert p.time_stop_hours == 12 and p.auto["targets"] and p.auto["breakeven"]
    assert p.targets == [Target(0.9, 0.5)]
    assert validate_plan(p, entry=1.0, stop=1.05) == []
    bad = build_plan({}, {"targets": [{"price": 1.1, "fraction": 0.7}, {"price": 0.8, "fraction": 0.6}],
                          "trail_pct": 3})
    errs = " | ".join(validate_plan(bad, 1.0, 1.05))
    assert "below entry" in errs and "sum to" in errs and "go together" in errs
    with pytest.raises(ValueError, match="unknown plan fields"):
        build_plan({}, {"take_profit": 1})


def test_uses_learnings_is_normalised_and_bad_citations_are_refused():
    assert build_plan({}, None).uses_learnings == []
    assert build_plan({}, {"uses_learnings": [11, 81, 11]}).uses_learnings == [11, 81]   # each once, in order
    for bad in (["11"], [True], [0], [-3], "11", 11, [1.5], list(range(1, 12))):
        with pytest.raises(ValueError, match="uses_learnings"):
            build_plan({}, {"uses_learnings": bad})


def test_breakdown_detects_losers_with_lower_highs():
    m = {"symbol": "DUMP/USDT:USDT", "change_24h": -15, "quote_volume": 1e8, "lower_high": True}
    assert "breakdown" in detect_all(m)
    assert "breakdown" not in detect_all({"symbol": "X/USDT:USDT", "change_24h": -15, "quote_volume": 1e8})


def test_listing_fade_v2_only_spot_listed_day6_to_30():
    from shortbot.setups import SETUPS
    d = SETUPS["listing_fade"].detect
    assert d({"listing_age_days": 7, "spot_available": True})
    assert not d({"listing_age_days": 7, "spot_available": False})       # Alpha / perp-only
    assert not d({"listing_age_days": 2, "spot_available": True})        # too early: fatter squeeze tails
    assert not d({"listing_age_days": 40, "spot_available": True})
    assert SETUPS["listing_fade"].default_plan["time_stop_hours"] == 24 * 45


def test_analyst_p_is_a_probability_set_at_entry_only():
    from shortbot.plans import revise_plan
    assert build_plan({}, None).analyst_p is None
    assert build_plan({}, {"analyst_p": 0.55}).analyst_p == 0.55
    for bad in (0, 1, 1.2, -0.1, "0.5", True):
        with pytest.raises(ValueError, match="analyst_p"):
            build_plan({}, {"analyst_p": bad})
    plan, _ = revise_plan(build_plan({}, {"analyst_p": 0.6}).to_dict(), {"time_stop_hours": 30})
    assert plan.analyst_p == 0.6                      # kept through a plan edit
    with pytest.raises(ValueError, match="analyst_p"):
        revise_plan(plan.to_dict(), {"analyst_p": 0.9})   # stated after the fact it would score itself
