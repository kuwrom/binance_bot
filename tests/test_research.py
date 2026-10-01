import pytest

from shortbot.journal import Journal
from shortbot.research import outcome_from_candles, report


def c(o, h, l, cl):
    return [0, o, h, l, cl, 1]


def test_outcome_short_positive_when_price_falls():
    candles = [c(1, 1.1, 0.95, 1.0)] + [c(1, 1.2, 0.8, 0.9)] * 80
    o = outcome_from_candles(1.0, candles)
    assert o["short_ret_4h"] == pytest.approx(10.0) and o["short_ret_72h"] == pytest.approx(10.0)
    assert o["max_adverse_24h"] == pytest.approx(20.0) and o["max_favorable_24h"] == pytest.approx(20.0)


def test_report_groups_by_veto_setup_and_feature():
    j = Journal(":memory:")
    rows = []
    for i in range(12):
        rows.append({"symbol": f"C{i}", "price": 1.0, "score": 70 if i % 2 else 10,
                     "vetoes": [] if i % 3 else ["negative funding -0.01%: crowded"],
                     "setups": ["pump_fade"], "features": {"rsi_1h": 60 + i * 3, "lower_high": True}})
    j.log_candidates("t", 0, rows, "caution")
    for cand in j.unlabelled_candidates(older_than_ms=1):
        good = int(cand["symbol"][1:]) % 2 == 1
        j.label_candidate(cand["id"], {"short_ret_24h": 8.0 if good else -4.0, "max_adverse_24h": 20 if not good else 3})
    r = report(j, min_n=3)
    assert r["labelled_candidates"] == 12
    assert r["by_score"]["60+"]["avg_short_ret_24h"] == 8.0 and r["by_score"]["0-29"]["squeeze_rate_pct"] == 100.0
    assert "negative funding #%" in r["by_veto_reason"] and "rsi_1h" in r["by_feature_tertile"]   # numbers dropped
    assert "lower_high" not in r["by_feature_tertile"]   # booleans are not bucketed as numbers


def test_report_empty():
    assert report(Journal(":memory:"))["labelled_candidates"] == 0


def test_archetypes_group_by_stage_and_game():
    from shortbot.research import archetype_of, archetypes
    assert archetype_of({"lifecycle_stage": "post_pump", "pump_state": "decaying"}) == "post_pump|post_pump_decay"
    assert archetype_of({"lifecycle_stage": "mature"}) == "unclassified"
    assert archetype_of({"lifecycle_stage": "new_listing"}) == "new_listing|no_edge"
    rows = [{"features": {"lifecycle_stage": "pumping", "controller_label": "thin_float", "session_bucket": "asia"}}]
    a = archetypes(rows)
    assert list(a["lifecycle"]) == ["pumping"] and list(a["controller"]) == ["thin_float"]


def test_liquidity_buckets():
    from shortbot.research import by_liquidity
    mk = lambda oi, ret: {"features": {"oi_value": oi}, "short_ret_24h": ret, "max_adverse_24h": 20.0}  # noqa: E731
    out = by_liquidity([mk(3e6, 5.0), mk(12e6, -2.0), mk(45e6, 1.0), mk(500e6, 0.5)])
    assert list(out) == ["0-5M", "10-20M", "30-60M", ">150M"]
    assert out["0-5M"]["avg_short_ret_24h"] == 5.0 and out["0-5M"]["squeeze_rate_pct"] == 100.0
