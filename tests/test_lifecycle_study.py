from shortbot.lifecycle_study import (
    coin_metrics, consistent, days_below, days_to_drawdown, dead_cat_bounces, km_by, km_median, pump_window,
    quantile, short_trade, spearman, trade_stats, value_at, volume_ratio,
)

DAY = 86_400_000


def series(closes, qv=None):
    """Daily bars (ts, o, h, l, c, qv): open = previous close, high 2% above the larger of the two."""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append((i * DAY, prev, max(prev, c) * 1.02, min(prev, c) * 0.98, c, (qv or [100.0] * len(closes))[i]))
        prev = c
    return out


def test_value_at_carries_last_close_only_when_delisted():
    assert value_at([1, 2, 3], 1, False) == 2
    assert value_at([1, 2, 3], 10, False) is None          # too young: censored
    assert value_at([1, 2, 3], 10, True) == 3              # delisted: settled at the last close


def test_drawdowns_from_peak_and_from_open():
    c = [100, 150, 120, 74, 60]
    assert days_to_drawdown(c, 0.5) == 3                   # 74 <= 150 * 0.5 = 75
    assert days_to_drawdown(c, 0.3) == 3                   # 120 is only -20% from 150
    assert days_to_drawdown(c, 0.9) is None
    assert days_below(c, 100, 0.3) == 4 and days_below(c, 100, 0.5) is None


def test_pump_window_and_dead_cat_bounces():
    pw = pump_window([10, 11, 12, 13], [11, 30, 14, 13])
    assert pw == {"ath_day": 1, "ath_vs_open": 2.0}
    # after the top: 100 -> 40 -> 60 (+50%, bounce 1) -> 30 -> 44 (not +50%) -> 46 (+53%, bounce 2)
    assert dead_cat_bounces([100, 40, 60, 30, 44, 46], 0) == 2
    assert dead_cat_bounces([100, 90, 80, 70], 0) == 0


def test_volume_ratio_needs_enough_history():
    qv = [100.0] * 7 + [10.0] * 30
    assert volume_ratio(qv, 30) == 0.1
    assert volume_ratio(qv[:20], 30) is None


def test_short_trade_return_and_squeeze_risk():
    h = [10, 12, 20, 9, 6]
    c = [10, 11, 15, 8, 5]
    t = short_trade(h, c, 0, 4, complete=False)
    assert t["ret"] == 0.5 and t["mae"] == 1.0             # ran +100% against us before paying 50%
    assert short_trade(h, c, 0, 10, complete=False) is None    # not enough history
    assert short_trade(h, c, 0, 10, complete=True)["ret"] == 0.5
    assert short_trade(h, c, 3, 1, complete=False)["mae"] == 0.0  # never above entry


def test_trade_stats_stop_caps_squeezes():
    s = trade_stats([{"ret": 0.5, "mae": 1.0}, {"ret": 0.2, "mae": 0.1}, None])
    assert s["n"] == 2 and s["win"] == 1.0 and s["mae100"] == 0.5
    assert abs(s["mean_stop"] - (-0.3 + 0.2) / 2) < 1e-9     # the squeezed winner was stopped out first


def test_km_handles_censoring():
    # 4 coins: events at day 10 and 20, two censored at day 5 (too young to know)
    d = [(10, True), (20, True), (5, False), (5, False)]
    assert km_median(d) == 10                              # after censoring, 1 of 2 at risk -> S = 0.5
    assert km_by(d, 9) == 0 and km_by(d, 20) == 1.0
    assert km_median([(3, False), (4, False)]) is None


def test_spearman_quantile_consistency():
    r, n = spearman(list(range(10)), [x * x for x in range(10)])
    assert r == 1.0 and n == 10
    assert spearman([1, 2], [2, 1])[0] is None             # too few points
    assert quantile([1, 2, 3, 4, 5], 0.5) == 3 and quantile([], 0.5) is None
    assert consistent({"a": [-1] * 5, "b": [-2] * 6, "c": [1] * 5, "d": [5]}) == (2, 3)


def test_coin_metrics_on_a_listing_that_pumps_then_bleeds():
    closes = [100, 130, 160, 140, 120] + [120 * 0.97 ** i for i in range(1, 200)]
    bars = series(closes)
    btc = {i * DAY: (50.0, 50.0) for i in range(300)}      # flat BTC: excess return == raw return
    m = coin_metrics(bars, btc, complete=False)
    assert m["ath_day"] == 2 and m["r_open_30"] < -0.4 and m["x_open_30"] == m["r_open_30"]
    assert m["dd_peak_50"][1] and m["dd_peak_50"][0] > 5
    assert m["trades"]["d7"][30]["ret"] > 0.5
    assert m["trades"]["ath*"][7]["mae"] == 0.0
    assert m["trades"]["w1_low_break"][7] is not None      # broke the week-1 low (100) after day 7
