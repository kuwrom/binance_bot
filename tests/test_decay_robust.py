import pytest

from shortbot import decay
from shortbot import robust_math as rm


def b(o, h, l, c, v=100.0, buy=50.0):
    return [0, o, h, l, c, v, buy]


def test_weight_half_life_and_unknown_family():
    assert decay.weight("social", 0) == 1.0
    assert decay.weight("social", 4.0) == pytest.approx(0.5)
    assert decay.weight("nope", 10) == 1.0 and decay.decayed_points(10, "sweep", 0.75) == 5


def test_heat_hot_and_cooling():
    rising = [10.0] * 30 + [12, 15, 30, 60]
    h = decay.heat(rising, fast_hl=2, slow_hl=12)
    assert h["hot"] and h["z"] > 2
    fading = [10.0] * 20 + [80, 70, 50, 30, 20]
    c = decay.heat(fading, fast_hl=2, slow_hl=12)
    assert not c["hot"] and c["accel"] < 0


def test_calibrate_half_life_shrinks_to_prior():
    ages = [0, 1, 2, 3, 4, 6, 8]
    ics = [0.10 * 0.5 ** (a / 2) for a in ages]           # true HL = 2h
    hl = decay.calibrate_half_life(ages, ics, prior_h=4.0, k=0)
    assert hl == pytest.approx(2.0, rel=1e-6)
    assert 2.0 < decay.calibrate_half_life(ages, ics, prior_h=4.0, k=30) < 4.0


def test_acceptance_and_sweep_rejection():
    bars = [b(1.0, 1.01, 0.99, 1.0, 100)] * 18 + [b(1.0, 1.06, 1.0, 1.01, 100), b(1.01, 1.02, 1.0, 1.0, 100)]
    r = rm.sweep_rejection(bars, 1.05, mark_high=1.055)
    assert r["rejected"] and r["acceptance_time"] == 0
    held = [b(1.0, 1.01, 0.99, 1.0, 100)] * 10 + [b(1.06, 1.07, 1.055, 1.065, 300)] * 10
    assert not rm.sweep_rejection(held, 1.05, mark_high=1.07)["rejected"]
    assert not rm.sweep_rejection(bars, 1.05, mark_high=1.04)["rejected"]      # mark never pierced: last-only spike


def test_painted_close_detection():
    bars = [b(1.0, 1.002, 0.998, 1.0, 100)] * 13 + [b(1.0, 1.0, 1.0, 1.0, 5), b(1.0, 1.03, 1.0, 1.03, 5)]
    p = rm.painted_close(bars, atr=0.02)
    assert p["painted"] and p["close_dev"] > 0.5
    honest = [b(1.0, 1.002, 0.998, 1.0, 100)] * 13 + [b(1.0, 1.03, 1.0, 1.02, 600), b(1.02, 1.035, 1.02, 1.03, 600)]
    assert not rm.painted_close(honest, atr=0.02)["painted"]


def test_mark_gap_and_close_engineering():
    last = [1.0] * 30 + [1.02]
    mark = [1.0] * 31
    assert rm.mark_gap_z([x * (1 + 0.0001 * (i % 3)) for i, x in enumerate(last)], mark) > 3
    zig = []
    for i in range(60):                       # each 15m block: pushed close (varying size), then reversed
        amp = 0.005 * (1 + (i // 3) % 4)
        if i % 3 == 2:
            zig.append(b(1.0, 1.0, 1.0, 1.0 + amp))
        elif i % 3 == 0 and i:
            zig.append(b(1.0, 1.0, 1.0, 1.0 - 0.005 * (1 + ((i - 1) // 3) % 4)))
        else:
            zig.append(b(1.0, 1.0, 1.0, 1.0))
    s = rm.close_engineering_score(zig)
    assert s is not None and s < -0.5
