import pytest

from shortbot import tape_math as tm


def bar(o, h, l, c, v=100.0, buy=50.0, ts=0):
    return [ts, o, h, l, c, v, buy]


def test_taker_imbalance_and_cvd():
    bars = [bar(1, 1, 1, 1, 100, 80), bar(1, 1, 1, 1, 100, 20)]
    assert tm.taker_imbalance(bars) == 0.0
    assert tm.taker_imbalance(bars[:1]) == pytest.approx(0.6)
    assert tm.cvd(bars) == [60.0, 0.0]


def test_ti_zscore_flags_buying_burst():
    calm = [bar(1, 1, 1, 1, 100, 46 + (i // 12) * 3) for i in range(48)]   # windows differ slightly
    burst = [bar(1, 1, 1, 1, 100, 90) for _ in range(12)]
    now, z = tm.ti_zscore(calm + burst, 12)
    assert now == pytest.approx(0.8) and z > 2


def test_cvd_bearish_divergence():
    early = [bar(1, 1.0 + i * 0.01, 0.99, 1.0, 100, 80) for i in range(8)]     # strong buying, highs rising
    late = [bar(1, 1.10 + i * 0.001, 1.0, 1.05, 100, 30) for i in range(8)]    # higher highs, sellers dominate
    assert tm.cvd_bearish_divergence(early + late)


def test_round_levels():
    assert tm.round_step(1.27) == pytest.approx(0.05) and tm.next_round_above(1.2969) == pytest.approx(1.30)
    assert tm.round_step(259) == 5 and tm.next_round_above(272.3) == 275


def test_failed_sweep_vs_accepted_breakout():
    base = [bar(1.0, 1.05, 0.98, 1.0) for _ in range(32)]           # prior high 1.05
    failed = base + [bar(1.0, 1.02, 0.99, 1.01), bar(1.01, 1.06, 1.0, 1.04), bar(1.04, 1.045, 1.0, 1.02)]
    s = tm.sweep_state(failed)
    assert s["state"] == "failed_sweep" and s["swept"] == 1.05 and s["sweep_high"] == 1.06
    accepted = base + [bar(1.0, 1.02, 0.99, 1.01), bar(1.01, 1.07, 1.0, 1.06), bar(1.06, 1.08, 1.05, 1.07)]
    assert tm.sweep_state(accepted)["state"] == "accepted"


def test_pump_in_progress_then_decaying():
    base = [bar(1, 1.01, 0.99, 1.0, 100) for _ in range(12)]
    pump = [bar(1.0, 1.2, 1.0, 1.15, 1000)]
    assert tm.pump_state(base + pump)["state"] == "in_progress"
    after = base + pump + [bar(1.15, 1.16, 1.0, 1.02, 300)]
    s = tm.pump_state(after)
    assert s["state"] == "decaying" and s["pump_vwap"] > 1.02
