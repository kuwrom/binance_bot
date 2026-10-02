import pytest

from shortbot.backtest import card_tests as C

DAY = C.DAY
D0 = 1_577_836_800_000


def test_hold_return_subtracts_funding_paid_by_the_long():
    assert C.hold_ret([100, 110, 121], [0, 0.01, 0.01], 0, 2) == pytest.approx(0.21 - 0.02)
    assert C.hold_ret([100, 110], [0, 0], 0, 5) is None


def test_negative_streak_fires_once_per_streak_after_30_negative_days_above_the_20dma():
    n = 200
    days = [D0 + k * DAY for k in range(n)]
    closes = [100 + k * 0.1 for k in range(n)]                         # rising: above its 20DMA
    fund = [0.0001] * 40 + [-0.0002] * 100 + [0.0001] * 60             # one long negative streak
    r = C.negative_funding_streak(days, closes, fund)
    assert len(r["events"]) == 1 and r["30d"]["n"] == 1 and r["30d"]["baseline_mean"] is not None


def test_zscore_long_triggers_on_a_funding_plunge_and_exits_when_z_recovers():
    n = 120
    days = [D0 + k * DAY for k in range(n)]
    closes = [100.0] * n
    fund = [0.0001 + (k % 3) * 1e-5 for k in range(n)]
    fund[60] = -0.002                                                   # one deep dip
    r = C.funding_zscore_long(days, closes, fund)
    assert r["n_events"] == 1 and r["avg_hold_d"] >= 1


def test_cycle_low_needs_all_three_conditions():
    n = 500
    days = [D0 + k * DAY for k in range(n)]
    closes = [100 + k for k in range(300)] + [399 - 3 * k for k in range(200)]   # -60% from the high
    fund = [0.0001] * 420 + [-0.0001] * 80
    r = C.cycle_low(days, closes, fund)
    assert r["events"] and all(e >= "2021" for e in r["events"])
    assert C.cycle_low(days, closes, [0.0001] * n)["events"] == []              # funding never negative
