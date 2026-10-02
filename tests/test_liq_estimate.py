"""The liquidation estimate (#17): Binance's formula at the leverage bracket's maintenance rate and amount, and the
bracket read (GET /fapi/v1/leverageBracket) per symbol, cached."""

import pytest

from shortbot.config import RiskLimits, Settings
from shortbot.exchange import Exchanges
from shortbot.risk import (FALLBACK_MAINT_MARGIN_PCT, AccountState, RiskViolation, ShortRequest, bracket_for,
                           check_open_short, est_liquidation_distance_pct, liquidation_price, max_leverage_for_stop)
from tests.fake_engine import BRACKETS
from tests.fakes import SYM, FakeCcxt

ACCT = AccountState(equity=10_000, open_symbols=(), total_open_notional=0, day_pnl=0)
LIM = RiskLimits(max_leverage=10, max_position_notional=1000, max_total_notional=10_000, min_quote_volume_24h=0)


def real_ex(fake: FakeCcxt | None = None) -> Exchanges:
    ex = Exchanges(Settings("demo", "", "", ":memory:", "", "", RiskLimits()))
    if fake:
        ex.trade, ex.public = fake, fake.prod
    return ex


def test_formula_reproduces_binance_faq_example():
    # binance.com FAQ "How to Calculate Liquidation Price of USDⓈ-M Futures Contracts": cross, one-way, two longs
    wb = 1_535_443.01
    eth = liquidation_price(1, 3683.979, 1456.84, wb, 0.10, 135_365.00, other_mm=71_200.81, other_upnl=-56_354.57)
    btc = liquidation_price(1, 109.488, 32_481.98, wb, 0.025, 16_300.00, other_mm=356_512.51, other_upnl=-448_192.89)
    assert round(eth, 2) == 1153.26 and round(btc, 2) == 26316.89


def test_isolated_short_distance_from_the_bracket():
    # isolated short (TMM = UPNL = 0, WB = notional / leverage): LP / EP = (1 + 1/L + cum/N) / (1 + MMR)
    assert est_liquidation_distance_pct(10, (0.01, 0.0), 1000) == pytest.approx((1.1 / 1.01 - 1) * 100)
    assert est_liquidation_distance_pct(10, (0.02, 500.0), 60_000) == pytest.approx(
        ((1 + 0.1 + 500 / 60_000) / 1.02 - 1) * 100)
    # no bracket: the conservative rate, below the old 100/L - 1
    fallback = est_liquidation_distance_pct(10)
    assert FALLBACK_MAINT_MARGIN_PCT == 2.5 and fallback == pytest.approx((1.1 / 1.025 - 1) * 100)
    assert fallback < est_liquidation_distance_pct(10, (0.01, 0.0), 1000) < 100 / 10 - 1


def test_bracket_by_notional():
    rows = [dict(r) for r in reversed(BRACKETS)]          # any order
    assert bracket_for(rows, 0) == (0.01, 0.0)
    assert bracket_for(rows, 49_999) == (0.01, 0.0)
    assert bracket_for(rows, 50_000) == (0.02, 500.0)     # notionalFloor <= notional: ccxt parse_position_risk too
    assert bracket_for(rows, 5e6) == (0.05, 8000.0)


def test_check_open_short_uses_the_request_bracket():
    # 6% stop at 10x: liquidation ~7.3% away at the fallback rate (minus a 2% buffer: rejected), ~8.9% at the bracket
    req = ShortRequest(SYM, 500, 1.0, 1.06, 10, 1e9)
    with pytest.raises(RiskViolation, match="liquidat"):
        check_open_short(req, ACCT, LIM)
    check_open_short(ShortRequest(SYM, 500, 1.0, 1.06, 10, 1e9, maint=(0.01, 0.0)), ACCT, LIM)


def test_max_leverage_for_a_stop_is_the_one_check_open_short_allows():
    # coin_snapshot's per-stop hint (#66): the old "100/(stop% + buffer + 1)" assumed a 1% maintenance rate
    assert max_leverage_for_stop(6, 2, 10) == 9                       # 10x: ~7.3% - 2 < 6 at the fallback rate
    assert max_leverage_for_stop(6, 2, 10, (0.01, 0.0), 500) == 10    # ~8.9% at a 1% bracket
    assert max_leverage_for_stop(6, 2, 3) == 3                        # never above the cap
    assert max_leverage_for_stop(30, 2, 3) == 2                       # 3x liquidates ~30.1% away
    assert max_leverage_for_stop(100, 2, 3) == 0                      # not even 1x
    for dist, maint in ((6, None), (6, (0.01, 0.0)), (12.5, None), (20, None)):
        lev = max_leverage_for_stop(dist, LIM.liq_buffer_pct, LIM.max_leverage, maint, 500)
        check_open_short(ShortRequest(SYM, 500, 1.0, 1 + dist / 100, lev, 1e9, maint), ACCT, LIM)
        if lev < LIM.max_leverage:
            with pytest.raises(RiskViolation, match="liquidat"):
                check_open_short(ShortRequest(SYM, 500, 1.0, 1 + dist / 100, lev + 1, 1e9, maint), ACCT, LIM)


async def test_maint_margin_reads_the_symbol_bracket_once():
    fake = FakeCcxt()
    calls = []
    real = fake.fapiPrivateGetLeverageBracket

    async def counted(req=None):
        calls.append(req)
        return await real(req)
    fake.fapiPrivateGetLeverageBracket = counted
    ex = real_ex(fake)
    assert await ex.maint_margin(SYM, 1000) == (0.01, 0.0)
    assert await ex.maint_margin("PUMPUSDT", 60_000) == (0.02, 500.0)     # same symbol, cached
    assert calls == [{"symbol": "PUMPUSDT"}]


async def test_maint_margin_accepts_an_object_answer_and_degrades_to_none_or_stale():
    fake = FakeCcxt()
    ex = real_ex(fake)

    async def one(req=None):   # the doc's schema allows an object for one symbol
        return {"symbol": "PUMPUSDT", "brackets": [dict(BRACKETS[0])]}
    fake.fapiPrivateGetLeverageBracket = one
    assert await ex.maint_margin(SYM, 10) == (0.01, 0.0)
    ex._brackets[SYM] = (0.0, ex._brackets[SYM][1])   # expired

    async def down(req=None):
        raise TimeoutError("read timed out")
    fake.fapiPrivateGetLeverageBracket = down
    assert await ex.maint_margin(SYM, 10) == (0.01, 0.0)   # stale beats none
    assert await ex.maint_margin("NOPE/USDT:USDT", 10) is None
    assert await real_ex().maint_margin(SYM, 10) is None    # no keys
