import pytest

from shortbot.config import RiskLimits
from shortbot.risk import (AccountState, RiskViolation, ShortRequest, check_open_short,
                           max_notional_for_risk)

LIMITS = RiskLimits()
ACCT = AccountState(equity=10_000, open_symbols=(), total_open_notional=0, day_pnl=0)


def req(**kw):
    base = dict(symbol="XYZ/USDT:USDT", notional=100, entry_price=1.0, stop_price=1.05,
                leverage=2, quote_volume_24h=50_000_000)
    base.update(kw)
    return ShortRequest(**base)


def reasons(r, acct=ACCT):
    with pytest.raises(RiskViolation) as e:
        check_open_short(r, acct, LIMITS)
    return " | ".join(e.value.reasons)


def test_valid_short_passes():
    check_open_short(req(), ACCT, LIMITS)


def test_stop_below_entry_rejected():
    assert "ABOVE entry" in reasons(req(stop_price=0.95))


def test_stop_too_tight_and_too_wide():
    assert "stop distance" in reasons(req(stop_price=1.001))
    assert "stop distance" in reasons(req(stop_price=1.5))


def test_leverage_cap():
    assert "leverage" in reasons(req(leverage=10))


def test_per_coin_and_total_caps():
    assert "per-coin max" in reasons(req(notional=150))
    acct = AccountState(equity=10_000, open_symbols=("A", "B"), total_open_notional=350, day_pnl=0)
    assert "total notional" in reasons(req(), acct)


def test_duplicate_and_max_positions():
    acct = AccountState(10_000, ("XYZ/USDT:USDT",), 100, 0)
    assert "already short" in reasons(req(), acct)
    acct = AccountState(10_000, ("A", "B", "C", "D", "E"), 0, 0)
    assert "max 5 positions" in reasons(req(), acct)


def test_liquidity_floor():
    assert "liquidity floor" in reasons(req(quote_volume_24h=1_000_000))


def test_risk_per_trade():
    small = AccountState(equity=200, open_symbols=(), total_open_notional=0, day_pnl=0)
    # 100 notional * 5% stop = 5 USDT = 2.5% of 200 equity > 1%
    assert "loss at stop" in reasons(req(), small)
    assert max_notional_for_risk(200, 1.0, 1.05, LIMITS) == pytest.approx(40)


def test_daily_loss_limit_is_measured_against_start_of_day_equity():
    """As the kill switch and the governor: -300 on a 10k start is 3%; -295 (3.04% of today's 9705) is not."""
    acct = AccountState(equity=9_700, open_symbols=(), total_open_notional=0, day_pnl=-300)
    assert "daily loss limit" in reasons(req(), acct)
    check_open_short(req(), AccountState(equity=9_705, open_symbols=(), total_open_notional=0, day_pnl=-295), LIMITS)


def test_multiple_violations_all_reported():
    msg = reasons(req(leverage=20, notional=500, stop_price=0.9))
    assert "leverage" in msg and "per-coin" in msg and "ABOVE" in msg


def test_liquidation_before_stop_rejected():
    lim = RiskLimits(max_leverage=10)
    # 18% stop at 10x: liquidation ~9% away -> would be liquidated first
    with pytest.raises(RiskViolation) as e:
        check_open_short(req(stop_price=1.18, leverage=10, notional=50), ACCT, lim)
    assert "liquidated before the stop" in " ".join(e.value.reasons)
    # 5% stop at 10x: liquidation ~9%, buffer 2% -> ok
    check_open_short(req(stop_price=1.05, leverage=10, notional=100), ACCT, lim)


def test_liquidity_tiers():
    from shortbot.risk import liquidity_tier
    assert liquidity_tier(50e6, 40e6) == ("A", 1.0)
    assert liquidity_tier(8e6, 12e6) == ("B", 0.5)
    assert liquidity_tier(8e6, None) == ("B", 0.5)          # unknown OI: volume decides
    assert liquidity_tier(3e6, 50e6) == (None, 0.0)          # costs eat the edge below ~$5M/24h
    assert liquidity_tier(30e6, 8e6) == (None, 0.0)
