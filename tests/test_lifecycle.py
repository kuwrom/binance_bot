from shortbot.signals.controller import controller_labels
from shortbot.signals.lifecycle import stage_of


def daily(closes):
    return [[i, c, c * 1.02, c * 0.98, c, 1000.0] for i, c in enumerate(closes)]


def test_stage_priorities():
    flat = daily([1.0] * 60)
    assert stage_of(400, True, False, False, flat)[0] == "delisting_risk"
    assert stage_of(10, False, False, False, flat)[0] == "new_listing"
    assert stage_of(400, False, False, True, flat)[0] == "unlock_ahead"
    assert stage_of(400, False, False, False, flat)[0] == "mature"
    assert stage_of(100, False, False, False, flat)[0] == "young"


def test_pump_then_post_pump_then_bleeding():
    pump = daily([1.0] * 40 + [1.2, 1.5, 2.0, 2.4, 2.6])
    assert stage_of(400, False, False, False, pump)[0] == "pumping"
    post = daily([1.0] * 30 + [2.5, 2.2, 2.0, 1.8, 1.7, 1.6, 1.6, 1.5])
    s, _, n = stage_of(400, False, False, False, post)
    assert s == "post_pump" and n["lc_pump_multiple_30d"] >= 1.8
    bleed = daily([10.0] * 20 + [10 - i * 0.05 for i in range(150)])
    assert stage_of(400, False, False, False, bleed)[0] == "bleeding"


def test_controller_labels():
    assert controller_labels(15, 5, 400, 0.1, 0.0001, False, 1.0, 8) == ["thin_float"]
    assert controller_labels(30, 3, 90, 0.05, 0.0001, False, 1.0, 8) == ["insider_overhang"]
    assert controller_labels(80, 1.1, 800, 0.02, 0.0001, False, 0.2, 1.5) == ["mm_range"]
    assert controller_labels(90, 1.0, 800, 0.02, 0.0001, False, 1.0, 5) == ["organic"]
    assert controller_labels(None, None, None, None, None, False, None, None) == ["unlabelled"]
    assert "thin_float" in controller_labels(50, 1.9, 2000, 0.14, -0.0013, False, 1.8, 26)   # ONE today


def test_one_day_pump_in_an_old_coin_is_pumping():
    d = daily([1.0] * 60 + [1.35])
    assert stage_of(2000, False, False, False, d)[0] == "pumping"
