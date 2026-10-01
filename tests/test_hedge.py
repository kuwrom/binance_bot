"""Hedge overlay math (docs/HEDGE.md): leg P&L with fees, slippage and funding (a LONG pays positive funding),
the entry / exit / funding windows, ratios, R conversion, the alt basket and the decision rules."""

import math

import pytest

from shortbot.backtest.hedge import Ctx, Series, beta, leg, shrink_ratio, summarize, window
from shortbot.backtest.hedge_diag import idio_stop, regime, splits, verdicts
from shortbot.backtest.sim import COSTS, FREE, Costs

H = 3_600_000
T0 = 1_790_000_000_000 // H * H
NOSLIP = Costs(slip_k_bps=0, slip_min_bps=0)


def series(sym, closes, vol=1e9, funding=(), sector="other"):
    """Bars at T0 + k h; open = previous close."""
    bars, prev = {}, closes[0]
    for k, c in enumerate(closes):
        bars[T0 + k * H] = (prev, c, vol)
        prev = c
    return Series(sym, bars, list(funding), sector)


def trade(**kw):
    base = dict(symbol="ALT/USDT:USDT", day="2026-09-01", entry_ts=T0 + 80 * H, exit_ts=T0 + 84 * H, r=-1.0,
                risk_pct=5.0, reason="time", entry_kind="market", entry_i=80, signal_i=80, bars=4,
                f={"beta": 1.0, "btc_ret_24h": 1.0, "btc_ret_4h": 0.5, "sector": "other"})
    return {**base, **kw}


def test_long_leg_pays_positive_funding_and_receives_negative():
    s = series("BTC/USDT:USDT", [100.0] * 10, funding=[(T0 + 3 * H, 0.0001), (T0 + 5 * H, -0.0002)])
    lg = leg(s, 100.0, 100.0, T0 + 2 * H, T0 + 4 * H, T0, Costs(taker_bps=0, slip_k_bps=0, slip_min_bps=0))
    assert lg["funding"] == pytest.approx(-0.0001)                  # long PAYS +0.01%
    lg = leg(s, 100.0, 100.0, T0 + 2 * H, T0 + 6 * H, T0, Costs(taker_bps=0, slip_k_bps=0, slip_min_bps=0))
    assert lg["funding"] == pytest.approx(-0.0001 + 0.0002)         # ... and RECEIVES negative funding
    assert lg["pnl"] == pytest.approx(0.0001)


def test_leg_fees_and_slippage_on_both_legs():
    s = series("BTC/USDT:USDT", [100.0] * 30, vol=1e9 / 24)        # $1B / 24h -> slippage floor 1 bp
    lg = leg(s, 100.0, 110.0, 0, 0, T0 + 25 * H, COSTS)
    assert lg["gross"] == pytest.approx(0.10)
    assert lg["fees"] == pytest.approx(5e-4 * 2.1)
    assert lg["slip"] == pytest.approx(1e-4 * 2.1)
    assert lg["pnl"] == pytest.approx(0.10 - 5e-4 * 2.1 - 1e-4 * 2.1)
    assert leg(s, 100.0, 110.0, 0, 0, T0 + 25 * H, FREE)["pnl"] == pytest.approx(0.10)


def test_windows_follow_the_sim_funding_rule():
    t = trade()
    e, x = t["entry_ts"], t["exit_ts"]
    assert window(t) == (e, "close", e + H, x, "close", x + H)     # market entry, time exit at a close
    assert window(trade(reason="stop")) == (e, "close", e + H, x, "mid", x)
    assert window(trade(entry_kind="limit", reason="target")) == (e, "mid", e, x, "mid", x)


def _ctx(btc_closes, alt_closes, funding=()):
    s = {"BTC/USDT:USDT": series("BTC/USDT:USDT", btc_closes, funding=funding),
         "ETH/USDT:USDT": series("ETH/USDT:USDT", btc_closes),
         "ALT/USDT:USDT": series("ALT/USDT:USDT", alt_closes, vol=1e6)}
    return Ctx(s, NOSLIP)


def test_overlay_converts_hedge_pnl_into_the_shorts_R():
    btc = [100.0] * 81 + [101.0, 102.0, 103.0, 104.0, 102.0, 100.0]  # close of bar 80 = 100, bar 84 = 104
    ctx = _ctx(btc, [10.0] * len(btc), funding=[(T0 + 81 * H, 0.0001), (T0 + 85 * H, 0.0001), (T0 + 86 * H, 0.001)])
    row = ctx.overlay(trade(), "btc_1")
    # long 1x notional: +4% move, fees 5 bp x (1 + 1.04); the entry comes right after the 81h print (not paid), the
    # time exit right after the 85h print (paid at that bar's open, 104); the 86h print is after the exit
    pnl = 0.04 - 5e-4 * 2.04 - 0.0001 * 1.04
    assert row["hedge_r"] == pytest.approx(pnl * 100 / 5.0)
    assert row["r"] == pytest.approx(-1.0 + pnl * 20)
    assert row["cost_r"] == pytest.approx(-(5e-4 * 2.04 + 0.0001 * 1.04) * 20)
    assert row["btc_ret"] == pytest.approx(0.04)
    assert ctx.overlay(trade(), "none")["r"] == -1.0
    # stop exit: the hedge sells the exit bar's (open + close) / 2 and the print at the exit bar's close is not paid
    row = ctx.overlay(trade(reason="stop"), "btc_1")
    assert row["btc_ret"] == pytest.approx((103.0 + 104.0) / 2 / 100 - 1)


def test_ratios_beta_shrink_clamp_and_conditional_hedge():
    btc = [100 * math.exp(0.01 * math.sin(k)) for k in range(90)]
    alt = [10 * math.exp(0.02 * math.sin(k)) for k in range(90)]  # log returns exactly 2x BTC's
    ctx = _ctx(btc, alt)
    assert beta(ctx.s["ALT/USDT:USDT"], [ctx.btc], T0 + 80 * H) == pytest.approx(2.0)
    assert shrink_ratio(2.0) == 1.5 and shrink_ratio(-3.0) == 0.5 and shrink_ratio(None) == 1.0
    assert shrink_ratio(0.6) == pytest.approx(0.8)
    t = trade(f={"beta": 2.0, "btc_ret_24h": -1.0, "btc_ret_4h": -0.2})
    assert ctx.overlay(t, "btc_ols")["ratio"] == 2.0
    assert ctx.overlay(t, "btc_shrink")["ratio"] == 1.5
    assert ctx.overlay(t, "btc_cond")["ratio"] == 0.0                # tide down: no hedge
    t["f"]["btc_ret_4h"] = 0.0
    assert ctx.overlay(t, "btc_cond")["ratio"] == 1.5
    assert ctx.overlay(t, "btc_mv")["ratio"] == pytest.approx(2.0, abs=0.1)
    assert ctx.overlay(t, "eth_shrink")["ratio"] == 1.5             # beta on ETH computed, then clamped


def test_alt_basket_excludes_btc_eth_and_the_coin():
    s = {f"C{k}/USDT:USDT": series(f"C{k}/USDT:USDT", [1.0] * 90, vol=1e6 * (k + 1)) for k in range(25)}
    s["BTC/USDT:USDT"] = series("BTC/USDT:USDT", [1.0] * 90, vol=1e12)
    s["ETH/USDT:USDT"] = series("ETH/USDT:USDT", [1.0] * 90, vol=1e11)
    ctx = Ctx(s)
    t = trade(symbol="C24/USDT:USDT")
    b = ctx.alts(t, window(t))
    names = {m.symbol for m in b}
    assert len(b) == 20 and "C24/USDT:USDT" not in names and not any(n.startswith(("BTC", "ETH")) for n in names)
    assert "C4/USDT:USDT" in names and "C3/USDT:USDT" not in names  # the 20 most liquid others


def test_summary_variance_ratio_and_decision_rules():
    rows = [{"symbol": "A", "day": f"d{k % 5}", "entry_ts": k, "exit_ts": k, "r": 0.5 * x, "r0": x,
             "btc_ret": x / 100, "hedge_r": 0.0, "cost_r": -0.01, "ratio": 1.0}
            for k, x in enumerate([1.0, -1.0, 0.5, -0.5, 2.0, -0.2, 0.3, 0.1, -0.4, 0.6])]
    s = summarize(rows, mid=5)
    assert s["var_ratio"] == pytest.approx(0.25) and s["n"] == 10 and s["coins"] == 1
    base = {"n": 10, "avg_r": 0.1, "sr_is": 0.1, "sr_oos": 0.1, "t_day": 1.0}
    better = {"n": 10, "avg_r": 0.05, "sr_is": 0.2, "sr_oos": 0.2, "t_day": 1.5, "var_ratio": 0.5}
    worse = {"n": 10, "avg_r": -0.05, "sr_is": 0.2, "sr_oos": 0.05, "t_day": 0.5, "var_ratio": 0.5}
    v = verdicts({"none": base, "a": better, "b": worse})
    assert v == {"a": "risk+ ADOPT", "b": "risk-"}


def test_regime_splits_and_the_squeeze_tail():
    assert (regime(0.02), regime(-0.02), regime(0.001)) == ("up", "down", "chop")
    rows = [{"r": -0.5, "r0": -1.0, "btc_ret": -0.02}, {"r": -0.2, "r0": -1.2, "btc_ret": 0.03},
            {"r": 0.4, "r0": 0.5, "btc_ret": 0.0}]
    sp = splits({"none": {"rows": rows}})
    assert sp["tail"]["squeeze+BTC down"]["none"] == (-0.5, 1)
    assert sp["regime"]["up"]["none"] == (-0.2, 1)


def test_idiosyncratic_stop_needs_a_residual_beyond_2_sigma():
    btc = [100 * math.exp(0.002 * math.sin(k)) for k in range(90)]
    alt = [10 * math.exp(0.002 * math.sin(k) + 0.001 * math.cos(3 * k)) for k in range(90)]
    ctx = _ctx(btc, alt)
    assert idio_stop(ctx, trade(reason="time")) is None
    assert idio_stop(ctx, trade(reason="stop", risk_pct=20.0)) is True      # +20% with BTC flat: idiosyncratic
    assert idio_stop(ctx, trade(reason="stop", risk_pct=0.01)) is False
