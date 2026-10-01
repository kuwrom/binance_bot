import pytest

from shortbot import indicators as ind


def candles(closes_, vol=100.0):
    return [[i, c, c * 1.01, c * 0.99, c, vol] for i, c in enumerate(closes_)]


def test_rsi_extremes():
    assert ind.rsi(list(range(1, 40))) == 100.0
    assert ind.rsi(list(range(40, 1, -1))) == pytest.approx(0.0)
    assert ind.rsi([1, 2]) is None


def test_ema_converges():
    assert ind.ema([5.0] * 10, 3)[-1] == 5.0


def test_atr_constant_range():
    c = candles([100.0] * 30)
    assert ind.atr(c) == pytest.approx(2.0)


def test_vwap_z_positive_after_pump():
    c = candles([100.0] * 20 + [150.0])
    b = ind.vwap_bands(c)
    assert b["z"] > 2 and b["pct_above"] > 0


def test_relative_volume():
    c = candles([1.0] * 21)
    c[-1][5] = 500
    assert ind.relative_volume(c) == 5.0


def test_lower_high():
    up = candles(list(range(1, 13)))
    assert not ind.lower_high(up)
    down = candles(list(range(12, 0, -1)))
    assert ind.lower_high(down)
