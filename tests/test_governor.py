from datetime import datetime, timedelta, timezone

from shortbot.config import RiskLimits
from shortbot.governor import GovInputs, daily_cap, govern, loss_streak
from shortbot.risk_budget import BudgetInputs, compute_budget, quarter_start
from shortbot.tilts import cell_mult, conviction_mult, regime_cell, streak_test

LIM = RiskLimits(risk_per_trade_pct=1.0, daily_loss_limit_pct=3.0)
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)   # a Wednesday


def g(**kw):
    base = dict(equity=10_000, peak_equity=10_000, day_pnl=0.0, regime="favorable", now=NOW)
    base.update(kw)
    return govern(LIM, GovInputs(**base))


def iso(h_ago: float) -> str:
    return (NOW - timedelta(hours=h_ago)).isoformat()


def test_calm_day_is_neutral():
    r = g()
    assert not r["halt"] and r["m_gov"] == 1.0 and r["daily"]["cap_usd"] == 300


def test_daily_cap_shrinks_with_regime_and_vol_but_floors_at_half():
    assert daily_cap(LIM, GovInputs(10_000, 10_000, 0, regime="caution"))["cap_usd"] == 225
    hot = GovInputs(10_000, 10_000, 0, regime="hostile", btc_vol_now=0.9, btc_vol_target=0.45)
    assert daily_cap(LIM, hot)["f"] == 0.5


def test_daily_cap_hit_halts_until_midnight():
    r = g(equity=9_700, day_pnl=-300)
    assert r["halt"] and r["until"].startswith("2026-10-01T00:00")


def test_weekly_cap_halts_until_monday():
    r = g(equity=9_550, week_pnl=-450, day_pnl=-10)
    assert r["halt"] and r["until"].startswith("2026-10-05T00:00")


def test_drawdown_tiers():
    assert g(equity=9_100, peak_equity=10_000)["m_gov"] == 0.5
    r = g(equity=8_700, peak_equity=10_000)
    assert r["halt"] and r["until"] is None      # human review


def test_loss_streak_halves_then_pauses():
    three = [(iso(5), 10.0), (iso(3), -5.0), (iso(2), -5.0), (iso(1), -5.0)]
    assert g(results=three)["m_gov"] == 0.5
    assert loss_streak(three)["current"] == 3
    one_win = three + [(iso(0.5), 4.0)]
    assert g(results=one_win)["m_gov"] == 0.5    # needs 2 wins
    assert g(results=one_win + [(iso(0.2), 4.0)])["m_gov"] == 1.0
    five = [(iso(6 - i), -5.0) for i in range(5)]
    r = g(results=five)
    assert r["halt"] and "cool-off" in r["reasons"][-1]


def test_old_streak_does_not_linger():
    old = [(iso(40 + i), -5.0) for i in range(3)]
    assert g(results=old)["m_gov"] == 1.0


def test_giveback_stop():
    assert not g(equity=10_120, day_pnl=120, day_peak_pnl=150)["halt"]           # 1.2% of 1.5% peak
    assert g(equity=10_060, day_pnl=60, day_peak_pnl=150)["halt"]                 # gave back > half
    assert g(equity=10_090, day_pnl=90, day_peak_pnl=250)["halt"]                 # peak 2.5% -> lock +1%


def test_budget_respects_governor_and_daily_room():
    halted = compute_budget(LIM, BudgetInputs(10_000, 10_000, "favorable", gov=g(equity=9_700, day_pnl=-300)), "X/USDT:USDT")
    assert halted["blocked"] and halted["halted_until"]
    near = g(equity=9_750, day_pnl=-250)    # cap 300 of start 10k -> 50 room
    r = compute_budget(LIM, BudgetInputs(9_750, 10_000, "favorable", gov=near), "X/USDT:USDT")
    assert r["risk_usd"] == 50


def test_conviction_a_requires_evidence():
    assert conviction_mult("A", [1.0] * 5)[1] == "B"
    assert conviction_mult("A", [0.5, -1.0] * 20)[1] == "B"
    m, tier, _ = conviction_mult("A", [0.8, -0.5, 1.2] * 12)
    assert (m, tier) == (1.5, "A")
    assert conviction_mult("C", [])[0] == 0.5


def test_moonshot_uses_its_own_bucket():
    r = compute_budget(LIM, BudgetInputs(10_000, 10_000, "hostile"), "X/USDT:USDT", "M")
    assert r["risk_usd"] == 125 and r["conviction"] == "M"     # 25% of a 500 bucket; ignores regime
    spent = compute_budget(LIM, BudgetInputs(10_000, 10_000, "favorable", moon_quarter_pnl=-450), "X/USDT:USDT", "M")
    assert spent["risk_usd"] == 50


def test_tilt_is_capped():
    lim = RiskLimits(risk_per_trade_pct=1.0, max_conviction_mult=1.2)
    r = compute_budget(lim, BudgetInputs(10_000, 10_000, "favorable", setup_rs=[0.8, -0.5, 1.2] * 12),
                       "X/USDT:USDT", "A")
    assert r["risk_usd"] == 120


def test_cells_and_shrinkage():
    assert regime_cell([100.0] * 19 + [110.0], 0.6, 0.5) == "up/highvol"
    assert regime_cell([100.0] * 10, 0.6, 0.5) is None
    assert cell_mult([-1.0] * 10, [0.2] * 50)[0] == 1.0          # too few
    assert cell_mult([-1.0, -0.8] * 20, [0.1] * 60)[0] == 0.5


def test_streak_test_finds_no_effect_in_iid():
    import random
    rng = random.Random(1)
    rs = [rng.choice([1.5, -1.0]) for _ in range(250)]
    out = streak_test(rs, n_perm=500)
    assert out["perm_p_value"] > 0.01 and "no usable" in out["verdict"]


def test_quarter_start():
    assert quarter_start(NOW).startswith("2026-07-01")
