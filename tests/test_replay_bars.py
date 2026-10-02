"""Bar math for the live-pipeline replay (backtest/replay_bars.py): REST-shaped klines with the forming bar last,
the rolling 24h ticker and the predicted funding rate, all as of a moment, never after it (task #29)."""

import pytest

from shortbot.backtest import replay_bars as RB
from shortbot.backtest.replay_bars import DAY, HOUR, MIN

T0 = 1_790_208_000_000          # 2026-09-24T00:00Z (day- and hour-aligned)


def minute(ts, px, v=10.0, tb=6.0):
    """(ts, o, h, l, c, v, qv, n, tbv, tbq): opens at px, high px+1, low px-1, closes at px+0.5."""
    return (ts, px, px + 1, px - 1, px + 0.5, v, v * px, 3, tb, tb * px)


def series(n, start=T0, px0=100.0):
    return [minute(start + i * MIN, px0 + i) for i in range(n)]


def test_tf_ms():
    assert RB.tf_ms("1m") == MIN and RB.tf_ms("15m") == 15 * MIN and RB.tf_ms("4h") == 4 * HOUR
    assert RB.tf_ms("1d") == DAY
    with pytest.raises(ValueError):
        RB.tf_ms("7m")


def test_forming_bar_is_last_and_built_from_closed_minutes():
    bars = series(200)                                     # 00:00 .. 03:19
    t = T0 + 2 * HOUR + 30 * MIN + 40_000                  # 02:30:40 -> minutes 02:00..02:29 are closed
    k = RB.klines(bars, "1h", t, 3)
    assert [b[RB.TS] for b in k] == [T0, T0 + HOUR, T0 + 2 * HOUR]
    first = bars[:60]
    assert k[0] == (T0, first[0][1], max(b[2] for b in first), min(b[3] for b in first), first[-1][4],
                    600.0, sum(b[6] for b in first), 180, 360.0, sum(b[9] for b in first))
    forming = bars[120:150]
    assert k[-1][RB.O] == forming[0][1] and k[-1][RB.C] == forming[-1][4] and k[-1][RB.V] == 300.0


def test_forming_bar_with_no_closed_minute_is_flat_at_the_last_close():
    bars = series(200)
    t = T0 + 2 * HOUR + 20_000                             # 02:00:20: nothing closed in the 02:00 bar yet
    k = RB.klines(bars, "1h", t, 2)
    assert k[-1] == (T0 + 2 * HOUR, bars[119][4], bars[119][4], bars[119][4], bars[119][4], 0.0, 0.0, 0, 0.0, 0.0)


def test_limit_and_minute_bars():
    bars = series(200)
    t = T0 + 100 * MIN + 1
    k = RB.klines(bars, "1m", t, 5)
    assert [b[RB.TS] for b in k[:-1]] == [T0 + i * MIN for i in (96, 97, 98, 99)]
    assert k[-1][RB.TS] == T0 + 100 * MIN and k[-1][RB.V] == 0.0     # 01:40 forming, flat
    assert len(RB.klines(bars, "5m", t, 500)) == 21                    # no bars before the data


def test_no_look_ahead_on_klines_and_ticker():
    bars = series(3000)
    t = T0 + 1500 * MIN + 30_000
    cut = [b for b in bars if b[0] + MIN <= t]
    poisoned = cut + [minute(b[0], 1e9) for b in bars if b[0] + MIN > t]
    for tf, lim in (("1m", 60), ("5m", 288), ("15m", 96), ("1h", 30), ("4h", 10), ("1d", 3)):
        assert RB.klines(poisoned, tf, t, lim) == RB.klines(cut, tf, t, lim)
    assert RB.ticker(poisoned, t) == RB.ticker(cut, t)


def test_coarse_bars_are_used_for_closed_history():
    bars = series(3000)
    hourly = RB.group(bars, HOUR)
    t = T0 + 1500 * MIN + 30_000
    fine_tail = [b for b in bars if b[0] >= T0 + 24 * HOUR]                # coarse covers the start
    assert RB.klines(fine_tail, "1h", t, 26, coarse=hourly, coarse_step=HOUR) == RB.klines(bars, "1h", t, 26)
    assert RB.klines(fine_tail, "4h", t, 7, coarse=hourly, coarse_step=HOUR) == RB.klines(bars, "4h", t, 7)
    t2 = T0 + 1530 * MIN + 30_000                                           # window [01:30, 25:30)
    edges = [b for b in bars if b[0] < T0 + 2 * HOUR or b[0] >= T0 + 25 * HOUR]
    assert RB.ticker(edges, t2, coarse=hourly) == RB.ticker(bars, t2)


def test_rolling_ticker_minute_aligned_24h():
    bars = series(3000)
    t = T0 + 1500 * MIN + 42_000                           # 1d 01:00:42
    tk = RB.ticker(bars, t)
    w = [b for b in bars if T0 + 60 * MIN <= b[0] < T0 + 1500 * MIN]        # [floor_min(t) - 24h, floor_min(t))
    assert len(w) == 1440
    assert tk["open"] == w[0][1] and tk["last"] == w[-1][4] and tk["close"] == w[-1][4]
    assert tk["percentage"] == pytest.approx((w[-1][4] - w[0][1]) / w[0][1] * 100)
    assert tk["quoteVolume"] == pytest.approx(sum(b[6] for b in w)) and tk["baseVolume"] == pytest.approx(14400)
    assert tk["high"] == max(b[2] for b in w) and tk["low"] == min(b[3] for b in w) and tk["timestamp"] == t


def test_ticker_of_a_young_coin_starts_at_its_first_bar_and_none_without_data():
    bars = series(100)
    tk = RB.ticker(bars, T0 + 50 * MIN)
    assert tk["open"] == bars[0][1]
    assert RB.ticker(bars, T0 - HOUR) is None
    assert RB.ticker([], T0) is None


def test_predicted_funding_follows_binance_formula():
    prem = [(T0 + i * MIN, 0, 0, 0, 0.0001, 0, 0, 0, 0, 0) for i in range(120)]
    assert RB.predicted_funding(prem, T0 + 60 * MIN, T0, 8) == pytest.approx(0.0001)   # P = I: F = I
    hot = [(b[0], 0, 0, 0, 0.002, 0, 0, 0, 0, 0) for b in prem]
    assert RB.predicted_funding(hot, T0 + 60 * MIN, T0, 8) == pytest.approx(0.002 - 0.0005)  # clamp(I - P)
    assert RB.predicted_funding(hot, T0 + 60 * MIN, T0, 4) == pytest.approx((0.002 - 0.0005) / 2)   # / (8 / N)
    ramp = [(T0 + i * MIN, 0, 0, 0, float(i) / 1e4, 0, 0, 0, 0, 0) for i in range(3)]   # time-weighted 1,2,3
    p = (0 * 1 + 1 * 2 + 2 * 3) / 6 / 1e4
    assert RB.predicted_funding(ramp, T0 + 3 * MIN, T0, 8, clamp=1) == pytest.approx(p + (0.0001 - p))
    assert RB.predicted_funding(ramp, T0 + 3 * MIN, T0, 1) == pytest.approx(0.0001 / 8)   # equal weights: P = I
    huge = [(b[0], 0, 0, 0, 0.5, 0, 0, 0, 0, 0) for b in prem]
    assert RB.predicted_funding(huge, T0 + 60 * MIN, T0, 8) == 0.02                         # capped
    assert RB.predicted_funding(prem, T0 + 60 * MIN, T0 + 59 * MIN + 1, 8) is None       # nothing since settling


def test_funding_interval_from_settlements():
    h = HOUR
    assert RB.funding_interval_h([T0, T0 + 8 * h, T0 + 16 * h]) == 8
    assert RB.funding_interval_h([T0, T0 + 8 * h, T0 + 12 * h, T0 + 16 * h]) == 4
    assert RB.funding_interval_h([T0]) == 8


def test_hourly_taker_ratio_uses_closed_hours():
    bars = series(200)                                     # each minute: v 10, taker buy 6 -> buy/sell 1.5
    r = RB.taker_ratios(bars, T0 + 3 * HOUR + 5 * MIN, 6)
    assert r == [pytest.approx(1.5)] * 3
