import random

import numpy as np
import pytest

from shortbot.backtest import xs_factors as X
from shortbot.backtest.xs_features import indicators, rank_normalise

DAY = X.DAY
D0 = 1_577_836_800_000            # 2020-01-01 00:00 UTC (a Wednesday)


def test_rank_normalise_handles_ties_and_missing():
    r = rank_normalise({"a": 1.0, "b": 2.0, "c": 2.0, "d": None, "e": 3.0})
    assert r["a"] == -0.5 and r["e"] == 0.5 and r["b"] == r["c"] == 0.0 and r["d"] == 0.0


def test_indicators_are_causal_and_sane():
    rng = random.Random(1)
    px, bars = 100.0, []
    for k in range(260):
        px *= 1 + rng.gauss(0.001, 0.02)
        bars.append((D0 + k * DAY, px, px * 1.01, px * 0.99, px, 1e6 * (1 + rng.random())))
    full, cut = indicators(bars), indicators(bars[:201])
    assert full[200] == cut[200]                                   # the future never changes a past value
    assert full[199]["sma200"] is not None and full[198]["sma200"] is None
    assert 0 <= full[-1]["rsi14"] <= 100 and 0 <= full[-1]["stoch_k"] <= 100


def test_ridge_recovers_a_planted_coefficient():
    rng = np.random.default_rng(0)
    x = rng.uniform(-0.5, 0.5, (2000, 3))
    y = 0.4 * x[:, 1] + rng.normal(0, 0.01, 2000)
    b = X.ridge(x, y)
    assert b[1] == pytest.approx(0.4 * (1 / 1.1), rel=0.15) and abs(b[0]) < 0.02 and abs(b[2]) < 0.02


def _panel(n_coins=40, n_days=600, signal=True):
    """Coins whose next-week return is driven by their own 1-week return sign (a momentum world) when signal."""
    rng = random.Random(7)
    bars, funding = {}, {}
    for c in range(n_coins):
        s = f"C{c}USDT"
        px, rows, drift = 100.0, [], 0.0
        for k in range(n_days):
            if k % 7 == 0:
                drift = rng.choice((-0.01, 0.01))
            step = (drift if signal else 0.0) + rng.gauss(0, 0.005)
            px *= 1 + step
            rows.append((D0 + k * DAY, px, px * 1.002, px * 0.998, px, 1e7 * (1 + c / n_coins)))
        bars[s] = rows
        funding[s] = [(D0 + k * DAY + j * DAY // 3, 0.0) for k in range(n_days) for j in range(3)]
    return X.Panel({"bars": bars, "funding": funding})


def test_ctrend_learns_only_from_completed_weeks_and_earns_on_a_planted_signal():
    p = _panel()
    rows = X.ctrend(p, top=40, window=20, min_weeks=8)
    assert rows and min(r["day"] for r in rows) >= D0 + (60 + 8 * 7) * DAY   # warm-up + 8 known weeks
    ls = [r["ls"] for r in rows]
    assert np.mean(ls) > 0
    flat = X.ctrend(_panel(signal=False), top=40, window=20, min_weeks=8)
    assert abs(np.mean([r["ls"] for r in flat])) < abs(np.mean(ls))


def test_blend_builds_a_book_and_the_buffer_cuts_turnover():
    p = _panel(n_coins=35, n_days=200)
    rows = X.blend(p, top=30, buffer=0.02)
    assert rows and all(r["cost"] >= 0 for r in rows)
    assert 0.7 * X.COST <= rows[0]["cost"] <= X.COST      # first day: build the book (tiny targets stay in the band)
    assert np.mean([r["cost"] for r in rows[1:]]) < rows[0]["cost"]       # the buffer keeps later turnover low


def test_turnover_and_summary():
    assert X._turnover({"a": 0.5, "b": -0.5}, {"a": 0.5, "c": -0.5}) == pytest.approx(1.0)
    rows = [{"day": D0 + k * 7 * DAY, "ls": 0.01 + (k % 2) * 0.002} for k in range(30)]
    s = X.summarise(rows, "ls", 52)
    assert s["n"] == 30 and s["t"] > 10 and 2020 in s["by_year"]
