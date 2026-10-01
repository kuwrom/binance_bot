import random

import pytest

from shortbot.backtest import stats
from shortbot.backtest.features import Market, features
from shortbot.backtest.sim import COSTS, FREE, Costs, Plan, simulate
from shortbot.backtest.strategies import trials

H = 3_600_000
NOFEE = Costs(taker_bps=0, maker_bps=0, slip_k_bps=0, slip_min_bps=0, funding=True)


def bars(n=40, px=100.0):
    return [{"ts": k * H, "o": px, "h": px + 0.5, "l": px - 0.5, "c": px, "qv": 1e9} for k in range(n)]


def test_stop_fills_at_open_when_the_bar_gaps_past_it():
    r = bars()
    r[11].update(o=107.0, h=108.0, l=106.0, c=107.5)
    t = simulate(r, 10, Plan(stop=105.0, time_stop=20), costs=FREE)
    assert t["reason"] == "stop_gap" and t["exit_i"] == 11
    assert t["pnl_pct"] == pytest.approx(-7.0) and t["r"] == pytest.approx(-7 / 5)


def test_stop_and_target_in_the_same_bar_assume_the_stop_first():
    r = bars()
    r[12].update(h=106.0, l=90.0)
    t = simulate(r, 10, Plan(stop=105.0, targets=((95.0, 1.0),), time_stop=20), costs=FREE)
    assert t["reason"] == "stop" and t["r"] == pytest.approx(-1.0)


def test_partial_targets_are_fractions_of_the_initial_size_and_the_rest_runs_to_the_time_stop():
    r = bars()
    r[12].update(l=94.0, c=96.0)                                # T1 (95) hit
    for k in range(13, 40):
        r[k].update(o=96.0, h=96.4, l=95.6, c=96.0)
    t = simulate(r, 10, Plan(stop=105.0, targets=((95.0, 0.5), (90.0, 0.5)), time_stop=6), costs=FREE)
    # half at 95 (+5%), half at the time-stop close 96 (+4%)
    assert t["reason"] == "time" and t["exit_i"] == 16
    assert t["pnl_pct"] == pytest.approx(0.5 * 5 + 0.5 * 4) and t["r"] == pytest.approx(4.5 / 5)
    r[14].update(l=89.0)
    t2 = simulate(r, 10, Plan(stop=105.0, targets=((95.0, 0.5), (90.0, 0.5)), time_stop=6), costs=FREE)
    assert t2["reason"] == "target" and t2["r"] == pytest.approx(7.5 / 5)


def test_breakeven_moves_the_stop_after_the_bar_that_reached_the_threshold():
    r = bars()
    r[12].update(l=94.0, c=96.0)                                # MFE 6% >= 1R (5%)
    r[13].update(o=99.0, h=101.0)                               # back through entry
    t = simulate(r, 10, Plan(stop=105.0, time_stop=20, be_r=1.0), costs=FREE)
    assert t["reason"] == "breakeven" and t["exit_i"] == 13 and t["r"] == pytest.approx(0.0)


def test_funding_is_received_on_positive_prints_held_through_only():
    r = bars()
    fund = [[11 * H, 0.001], [12 * H + 3, 0.002], [16 * H, 0.004], [17 * H, 0.5]]
    # entry at the close of bar 10 (= 11*H, right after that print) -> exit at the close of bar 15 (= 16*H, after it)
    t = simulate(r, 10, Plan(stop=105.0, time_stop=5), funding=fund, costs=NOFEE)
    assert t["exit_i"] == 15
    assert t["funding_pct"] == pytest.approx((0.002 + 0.004) * 100)
    neg = simulate(r, 10, Plan(stop=105.0, time_stop=5), funding=[[12 * H, -0.003]], costs=NOFEE)
    assert neg["funding_pct"] == pytest.approx(-0.3) and neg["r"] == pytest.approx(-0.3 / 5)


def test_fees_and_slippage_are_charged_and_grow_as_liquidity_falls():
    r = bars()
    free = simulate(r, 10, Plan(stop=105.0, time_stop=5), costs=FREE)
    paid = simulate(r, 10, Plan(stop=105.0, time_stop=5), costs=Costs(funding=False))
    assert free["pnl_pct"] == pytest.approx(0.0)
    assert paid["fees_pct"] == pytest.approx(0.10, abs=1e-3)          # 5 bp in + 5 bp out
    assert paid["pnl_pct"] < -0.10
    thin = [dict(b, qv=1e4) for b in r]                               # $0.24M/24h: ~40 bp slippage per side
    t = simulate(thin, 10, Plan(stop=105.0, time_stop=5), costs=Costs(funding=False))
    assert t["slip_pct"] > 5 * paid["slip_pct"] and COSTS.slip(24e4) > COSTS.slip(2.4e10)


def test_limit_entry_needs_a_trade_through_and_can_be_stopped_in_the_fill_bar():
    r = bars()
    r[12].update(h=102.0)                                             # touches, never trades through
    p = Plan(stop=106.0, entry="limit", limit_px=102.0, limit_ttl=3, time_stop=5)
    assert simulate(r, 10, p, costs=FREE) == {"filled": False, "signal_i": 10}
    r[12].update(h=102.5)
    t = simulate(r, 10, p, costs=FREE)
    assert t["entry_i"] == 12 and t["entry_px"] == 102.0
    r[12].update(h=107.0)                                             # fill and stop in one bar -> stopped
    t = simulate(r, 10, p, costs=FREE)
    assert t["reason"] == "stop" and t["exit_i"] == 12 and t["r"] == pytest.approx(-1.0)


T0 = 1000 * H
EVENTS = {"AAA/USDT:USDT": {80: "pump", 81: "retest", 125: "pump", 127: "retest"},
          "BBB/USDT:USDT": {104: "pump", 106: "retest"}, "DDD/USDT:USDT": {110: "crash", 111: "crash", 112: "crash"}}


def _synthetic(seed=5, n=160):
    """5 coins (BTC + a 4-coin Meme sector), random walks with injected pumps + retests, a downtrend (CCC), a
    leader breakdown (DDD, the most traded) and a listing (EEE) so every strategy family emits signals."""
    rng = random.Random(seed)
    meme = [rng.gauss(0, 0.012) for _ in range(n)]            # shared sector factor (laggards correlate)
    out = []
    for sym, tags, vol, drift, sd in (("BTC/USDT:USDT", ["PoW"], 5e7, 0, 0.01), ("AAA/USDT:USDT", ["Meme"], 2e6, 0, 0.01),
                                      ("BBB/USDT:USDT", ["Meme"], 2e6, 0, 0.01),
                                      ("CCC/USDT:USDT", ["Meme"], 2e6, -0.004, 0.02),
                                      ("DDD/USDT:USDT", ["Meme"], 8e6, 0, 0.01), ("EEE/USDT:USDT", ["AI"], 3e6, -0.001, 0.01)):
        px, perp, oi, ev = 100.0, [], {}, EVENTS.get(sym, {})
        for k in range(n):
            o, kind = px, ev.get(k)
            qv = vol * (1 + rng.random())
            px *= 1 + rng.gauss(drift, sd) + (meme[k] if "Meme" in tags else 0)
            h, low = max(o, px) * (1 + rng.random() * 0.01), min(o, px) * (1 - rng.random() * 0.01)
            if kind == "pump":
                px, h, qv = o * 1.10, o * 1.12, vol * 20
            elif kind == "retest":
                h, px = perp[-2]["h"] * 1.01, o * 0.97
            elif kind == "crash":
                px, low = o * 0.96, o * 0.95
            perp.append({"ts": T0 + k * H, "o": o, "h": max(h, o, px), "l": min(low, o, px), "c": px, "qv": qv,
                         "n": 10, "tbq": qv / 2})
            oi[T0 + (k + 1) * H] = 5e6 * (1 + rng.gauss(0, 0.02)) * (0.9 if kind == "pump" else 1)
        out.append({"symbol": sym, "onboard_ms": T0 + 3 * H if sym.startswith("EEE") else 1, "tags": tags,
                    "delist_ms": T0 + 150 * H if sym.startswith("EEE") else None,
                    "perp": perp, "oi": oi, "premium": {T0 + k * H: rng.gauss(0.001, 0.003) for k in range(n)},
                    "funding": [[T0 + k * H, rng.gauss(0.0003, 0.0005)] for k in range(0, n, 8)], "spot": [],
                    "ls_top": {T0 + (k + 1) * H: 1 + rng.random() for k in range(n)},
                    "ls_global": {T0 + (k + 1) * H: 1 + rng.random() for k in range(n)}})
    return out


def _truncate(coins, cut_ts):
    """Everything known at the CLOSE of the bar that opens at cut_ts."""
    end = cut_ts + H
    return [{**c, "perp": [b for b in c["perp"] if b["ts"] <= cut_ts],
             "oi": {t: v for t, v in c["oi"].items() if t <= end},
             "premium": {t: v for t, v in c["premium"].items() if t <= cut_ts},
             "funding": [f for f in c["funding"] if f[0] <= end],
             "ls_top": {t: v for t, v in c["ls_top"].items() if t <= end},
             "ls_global": {t: v for t, v in c["ls_global"].items() if t <= end}} for c in coins]


@pytest.mark.parametrize("i", [100, 130])
def test_features_do_not_look_ahead(i):
    coins = _synthetic()
    full = Market(coins)
    cut = Market(_truncate(coins, T0 + i * H))
    for sym in full.coins:
        assert features(full, full.coins[sym], i) == features(cut, cut.coins[sym], i)


def test_strategy_signals_do_not_look_ahead():
    coins = _synthetic()
    full, cut_i, fired = Market(coins), 135, 0
    cut = Market(_truncate(coins, T0 + cut_i * H))
    for t in trials():
        if t.cross:
            a = {(c.symbol, i, p.stop) for c, i, p in t.fn(full, t.params) if i <= cut_i}
            b = {(c.symbol, i, p.stop) for c, i, p in t.fn(cut, t.params)}
        else:
            a = {(s, i, p.stop) for s, c in full.coins.items() for i, p in t.fn(full, c, t.params) if i <= cut_i}
            b = {(s, i, p.stop) for s, c in cut.coins.items() for i, p in t.fn(cut, c, t.params)}
        assert a == b, t.name
        fired += bool(a)
    assert fired >= 30          # the synthetic market must actually exercise (nearly) every strategy


def test_deflated_sharpe_haircuts_for_the_number_of_trials():
    assert stats.expected_max_sr(100, 0.01) > stats.expected_max_sr(10, 0.01) > 0
    xs = [1.0, -0.5, 0.8, -1.0, 1.2, 0.3, -0.2, 0.9, -1.0, 0.7] * 5
    one, many = stats.dsr(xs, 1, 0.01), stats.dsr(xs, 200, 0.01)
    assert one["dsr"] == one["psr0"] and many["dsr"] < one["dsr"]
    trades = [{"r": r, "day": f"d{k % 5}", "symbol": f"s{k % 7}", "exit_ts": k} for k, r in enumerate(xs)]
    assert stats.max_dd_r(trades) <= 0 and stats.cluster_t(trades, "day")[1] == 5
