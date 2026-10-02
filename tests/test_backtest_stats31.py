"""#31 in the backtest statistics: the reported avg R gets its own clustered t, and the Deflated Sharpe uses the
effective sample length."""

import random

import pytest

from shortbot import cluster_se as C
from shortbot.backtest import stats


def _trades(rng, days=30, coins=25, day_sd=0.6, mu=0.05):
    shock = {d: rng.gauss(0, day_sd) for d in range(days)}
    out = []
    for k in range(days * 8):
        d, s = k % days, k % coins
        r = mu + shock[d] + rng.gauss(0, 1)
        out.append({"r": r, "r_gross": r + 0.05, "day": f"2026-08-{d + 1:02d}", "symbol": f"C{s}", "exit_ts": k,
                    "entry_ts": k, "signal_i": k, "pnl_pct": r, "fees_pct": 0.1, "slip_pct": 0.05, "funding_pct": 0.0,
                    "risk_pct": 1.0, "mae_pct": 1.0, "bars": 5, "reason": "time"})
    return out


def test_cluster_t_is_the_t_of_the_pooled_mean():
    # a busy losing day and many small winning days: the pooled mean is < 0, the mean of day means > 0
    trades = [{"r": -1.0, "day": "d0", "symbol": f"s{k}"} for k in range(30)]
    trades += [{"r": 0.25, "day": f"d{1 + k}", "symbol": f"s{k}"} for k in range(20)]
    t, g = stats.cluster_t(trades, "day")
    assert g == 21 and t < 0
    ref = C.mean_inference([x["r"] for x in trades], [x["day"] for x in trades], [x["symbol"] for x in trades])
    assert t == round(ref["t_day"], 2)


def test_summarize_reports_the_two_way_t_and_effective_n():
    s = stats.summarize(_trades(random.Random(2)))
    assert {"t_day", "t_coin", "t_2way", "n_eff", "se_2way"} <= set(s)
    assert s["t_2way"] is not None and abs(s["t_2way"]) <= min(abs(s["t_day"]), abs(s["t_coin"])) + 0.01
    assert 0 < s["n_eff"] < s["n"]          # a day shock: far fewer than n independent trades


def test_dsr_uses_the_effective_sample_length():
    xs = [1.0, -0.5, 0.8, -1.0, 1.2, 0.3, -0.2, 0.9, -1.0, 0.7] * 5
    full, eff = stats.dsr(xs, 1, 0.01), stats.dsr(xs, 1, 0.01, t_eff=12.0)
    assert full["t"] == len(xs) and eff["t"] == 12.0
    sk, ku = stats.moments(xs)
    assert eff["psr0"] == round(stats.psr(stats.sharpe(xs), 12.0, sk, ku, 0.0), 3)
    assert 0.5 < eff["psr0"] < full["psr0"]    # a positive SR is less certain on fewer independent observations


def test_trade_dsr_takes_t_from_the_trades_clusters():
    tr = _trades(random.Random(4), mu=0.3)
    row = stats.trade_dsr(tr, 10, 0.01)
    s = stats.summarize(tr)
    assert row["t"] == pytest.approx(s["n_eff"], abs=0.11)
    assert row["dsr"] == stats.dsr([t["r"] for t in tr], 10, 0.01, t_eff=row["t"])["dsr"]


def test_paired_edge_reports_the_two_way_t():
    rng = random.Random(6)
    tr = _trades(rng, mu=0.2)
    ctl = [{**t, "r": t["r"] - 0.2 + rng.gauss(0, 0.3), "meta": {"donor": [t["symbol"], t["signal_i"]]}} for t in tr]
    pe = stats.paired_edge(tr, ctl, split_ts=len(tr) // 2)
    assert pe["t_2way"] is not None and pe["t_2way"] <= pe["t_day"] + 0.01 and pe["edge"] > 0


def test_rungs_and_card_tests_t_belong_to_the_pooled_mean():
    from shortbot.backtest import card_tests, rungs
    # one busy day of losers, many quiet days of small winners: pooled mean < 0, mean of day means > 0
    diffs = [("d0", -1.0)] * 30 + [(f"d{1 + k}", 0.25) for k in range(20)]
    assert sum(x for _, x in diffs) < 0 and rungs._t_day(diffs) < 0
    pairs = [(0, -1.0)] * 30 + [((1 + k) * 3_600_000, 0.25) for k in range(20)]
    c = card_tests._clustered(pairs)
    assert c["mean"] < 0 and c["t_clustered"] < 0 and c["t_day"] is None      # one UTC day: no day t
