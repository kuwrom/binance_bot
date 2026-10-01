import pytest

from shortbot.config import RiskLimits
from shortbot.risk_budget import BudgetInputs, btc_vol_now_and_target, cluster_of, compute_budget

LIM = RiskLimits(risk_per_trade_pct=1.0, max_drawdown_pct=20, max_portfolio_heat_pct=5, max_cluster_heat_pct=3.5,
                max_positions=1)   # one slot: these tests isolate the multipliers (book rules: test_book.py)


def b(**kw):
    base = dict(equity=10_000, peak_equity=10_000, regime="favorable")
    base.update(kw)
    return compute_budget(LIM, BudgetInputs(**base), "PUMP/USDT:USDT")


def test_neutral_budget_is_base():
    r = b()
    assert r["risk_usd"] == 100 and r["multipliers"] == {"m_dd": 1.0, "m_vol": 1.0, "m_regime": 1.0, "m_corr": 1.0, "tilt": 1.0, "m_gov": 1.0, "m_div": 1.0}


def test_drawdown_scales_down_and_stops_at_max():
    assert b(equity=9_000, peak_equity=10_000)["multipliers"]["m_dd"] == pytest.approx(0.5)  # 10% of 20%
    r = b(equity=7_900, peak_equity=10_000)
    assert r["blocked"] and "STOP trading" in r["reasons"][0]


def test_vol_only_dampens_never_levers_up():
    assert b(btc_vol_now=0.8, btc_vol_target=0.4)["multipliers"]["m_vol"] == 0.5
    assert b(btc_vol_now=0.2, btc_vol_target=0.4)["multipliers"]["m_vol"] == 1.0


def test_regime_and_correlation():
    assert b(regime="caution")["risk_usd"] == 75
    r = b(open_risks={"OTHER/USDT:USDT": 50})
    assert r["multipliers"]["m_corr"] == 0.5 and r["risk_usd"] == 50
    assert cluster_of("SOL/USDT:USDT") == "major:SOL" and cluster_of("QNT/USDT:USDT") == "alts"


def test_heat_caps_limit_new_risk():
    r = b(open_risks={"A/USDT:USDT": 330})   # cluster cap 350 -> 20 room
    assert r["risk_usd"] == 20 and "heat room" in r["reasons"][0]


def test_btc_vol_target_is_median_of_rolling_vol():
    closes = [100 * (1.01 if i % 2 else 0.99) ** (i % 7) for i in range(400)]
    now, tgt = btc_vol_now_and_target(closes)
    assert now > 0 and tgt > 0
