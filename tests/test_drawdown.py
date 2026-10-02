import random

import pytest

from shortbot.backtest import drawdown as D


def test_max_drawdown_from_the_running_peak():
    assert D.max_drawdown([1, 1, -3, 1, -1, 2]) == 3.0
    assert D.max_drawdown([1, 2, 3]) == 0.0


def test_gaussian_formula_matches_bailey_lopez_de_prado():
    g = D.maxdd_gaussian(0.1, 1.0, 0.95)
    z = 1.6448536
    assert g["maxdd_r"] == pytest.approx(round(z * z / 0.4, 2))
    assert g["at_trade"] == pytest.approx(round((z / 0.2) ** 2, 1))


def test_exact_one_peak_quantile_matches_simulation_and_exceeds_the_path_shortcut():
    rng = random.Random(1)
    worst = []
    for _ in range(3000):                                  # deepest loss below the starting peak, 1,500 trades
        cum = low = 0.0
        for _ in range(1500):
            cum += rng.gauss(0.2, 1.0)
            low = min(low, cum)
        worst.append(-low)
    worst.sort()
    exact = D.first_passage_quantile(0.2, 1.0, 0.95)       # ln(20) / 0.4 = 7.49
    assert exact == pytest.approx(7.49, abs=0.01)
    assert worst[int(0.95 * len(worst))] == pytest.approx(exact, rel=0.15)   # discrete steps sit a bit lower
    assert D.maxdd_gaussian(0.2, 1.0, 0.95)["maxdd_r"] < exact / 2


def test_horizon_max_drawdown_exceeds_any_single_episode():
    rng = random.Random(3)
    resid = [rng.gauss(0, 1) for _ in range(3000)]
    boot = D.bootstrap_maxdd(resid, 0.2, 500, paths=800, block=1)
    assert boot[0.95] > D.first_passage_quantile(0.2, 1.0, 0.80)


def test_profile_needs_30_trades_and_reports_tables():
    assert D.profile([{"r": 1.0}] * 5) == {"n": 5}
    rng = random.Random(2)
    p = D.profile([{"r": rng.gauss(0, 1.2), "entry_ts": k} for k in range(200)])
    assert p["n"] == 200 and "boot_n100" in p["mu=0.1"] and p["mu=0.05"]["exact_one_peak"][0.95] > \
        p["mu=0.2"]["exact_one_peak"][0.95]
