"""Round-2 backtest logic: sell-stop entries, the funding budget, breakdown entry types, exit variants, re-entry,
and the continuation / paired-difference stats (docs/FAILURES.md Round 2)."""

import itertools

import pytest

from shortbot.backtest import report_v3 as r3
from shortbot.backtest import strategies_v3 as v3
from shortbot.backtest.features import Market
from shortbot.backtest.sim import COSTS, FREE, Costs, Plan, simulate
from shortbot.backtest.strategies_v2 import ledger as ledger_v2
from tests.test_backtest import T0, _synthetic, _truncate, bars

H = 3_600_000
NOFEE = Costs(taker_bps=0, maker_bps=0, slip_k_bps=0, slip_min_bps=0, funding=True)


def _stop_plan(**kw):
    base = dict(stop=106.0, entry="stop", limit_px=98.0, arm_px=101.0, limit_ttl=6, time_stop=5)
    return Plan(**{**base, **kw})


def test_sell_stop_needs_a_bounce_attempt_before_the_trigger():
    r = bars()
    r[12].update(l=97.0)                                          # trigger, but no bounce yet
    assert simulate(r, 10, _stop_plan(), costs=FREE) == {"filled": False, "signal_i": 10}
    r[11].update(h=101.5)                                         # bounce attempt, then the trigger
    t = simulate(r, 10, _stop_plan(), costs=FREE)
    assert t["entry_i"] == 12 and t["entry_px"] == pytest.approx(98.0)
    r[12].update(o=96.0, h=96.5, l=95.0, c=95.5)                  # gapped below the trigger: filled at the open
    assert simulate(r, 10, _stop_plan(), costs=FREE)["entry_px"] == pytest.approx(96.0)


def test_sell_stop_is_cancelled_when_the_stop_trades_first_and_pays_stop_slippage():
    r = bars()
    r[11].update(h=107.0)                                         # invalidated
    r[12].update(l=97.0)
    assert simulate(r, 10, _stop_plan(), costs=FREE)["filled"] is False
    r[11].update(h=101.5)
    t = simulate(r, 10, _stop_plan(), costs=COSTS)
    slip = COSTS.slip(sum(b["qv"] for b in r[:11]))
    assert t["entry_px"] == pytest.approx(98.0 * (1 - COSTS.stop_slip_mult * slip))
    assert simulate(r, 10, _stop_plan(limit_px=101.0), costs=FREE) is None   # trigger must sit below the close


def test_funding_budget_exits_once_funding_paid_exceeds_half_r_without_follow_through():
    r = bars()
    fund = [[k * H, -0.01] for k in range(12, 40)]                # -1% per hour vs a 5% risk = 0.2R per print
    p = Plan(stop=105.0, time_stop=20, fund_budget_r=0.5, fund_follow_r=0.5)
    t = simulate(r, 10, p, funding=fund, costs=NOFEE)
    assert t["reason"] == "fund_budget" and t["exit_i"] == 14      # 0.6R paid after the prints at 12, 13, 14
    for k in range(11, 40):
        r[k].update(o=97.0, h=97.4, l=96.6, c=97.0)                # +0.6R open gain: follow-through, keep holding
    t2 = simulate(r, 10, p, funding=fund, costs=NOFEE)
    assert t2["reason"] == "time"
    assert simulate(r, 10, Plan(stop=105.0, time_stop=20), funding=fund, costs=NOFEE)["reason"] == "time"


def _bd_market(drop_at=90, n=140):
    raws = _synthetic(n=n)
    c = raws[1]                                                    # AAA: add a clean breakdown
    for k in range(drop_at - 24, n):
        b = c["perp"][k]
        f = 0.992 ** (min(k, drop_at) - (drop_at - 24))
        b.update(o=100 * f / 0.992, h=100 * f / 0.992 * 1.002, l=100 * f * 0.998, c=100 * f)
    return raws


def test_breakdown_plans_share_the_stop_and_differ_only_in_the_entry():
    m = Market(_bd_market())
    c = m.coins["AAA/USDT:USDT"]
    arms = list(v3.breakdown_arms(c, -15))
    assert arms, "the injected breakdown must arm"
    i = arms[0]
    pls = {k: v3.breakdown_plan(c, i, k) for k in ("limit", "stop", "market")}
    assert len({p.stop for p in pls.values()}) == 1
    assert pls["limit"].limit_px == pytest.approx(c.ema20[i]) and pls["limit"].entry == "limit"
    assert pls["stop"].limit_px < min(b["l"] for b in c.rows[i - 11:i + 1])
    assert pls["stop"].arm_px == pytest.approx(c.rows[i]["c"] + 0.5 * v3.atr_abs(c, i))
    assert all(b - a >= v3.BD_COOLDOWN for a, b in itertools.pairwise(arms))


def test_exit_variants_convert_the_live_percent_trail_into_r():
    pl = Plan(stop=105.0, time_stop=12, be_r=1.0)
    t = v3.with_exits(pl, 100.0, "trail", (10.0, 4.0))
    assert t.be_r is None and t.trail_act_r == pytest.approx(2.0) and t.trail_dist_r * 0.05 == pytest.approx(0.04)
    f = v3.with_exits(pl, 100.0, "fixed", (10.0, 4.0))
    assert f.be_r is None and f.trail_act_r is None
    b = v3.with_exits(pl, 100.0, "both", (5.0, 3.0))
    assert b.be_r == 1.0 and b.trail_act_r == pytest.approx(1.0)


def test_reentry_needs_a_close_back_below_the_stop_within_three_bars():
    m = Market(_synthetic())
    c = m.coins["AAA/USDT:USDT"]
    for k in range(100, 110):
        c.rows[k].update(o=100.0, h=100.5, l=99.5, c=100.0)
    c.rows[102].update(h=106.0, c=104.0)                            # stop 105 hit, close above
    c.rows[103].update(h=105.8, c=105.5)
    c.rows[104].update(h=103.0, c=103.0)                            # 2nd bar after: closes back below
    pl = Plan(stop=105.0, targets=((95.0, 0.5), (90.0, 0.5)), time_stop=12, be_r=1.0)
    res = simulate(c.rows, 100, pl, costs=FREE)
    assert res["reason"] == "stop" and res["exit_i"] == 102
    k, p2 = v3.reentry_plan(c, res, pl, 1.0)
    assert k == 104 and p2.stop == pytest.approx(106.0 + v3.atr_abs(c, 104)) and p2.meta["reentry"]
    assert p2.targets == pl.targets
    c.rows[104].update(c=105.5)
    c.rows[105].update(c=105.5)
    assert v3.reentry_plan(c, res, pl, 1.0) is None                 # no close below 105 in bars 103-105


def test_round2_ledger_adds_23_uniquely_named_trials_and_references_exist():
    names = [t.name for t in v3.trials_v3()]
    assert len(names) == 23 == len(set(names))
    full = [t.name for t in v3.ledger()]
    assert len(full) == len(ledger_v2()) + 23 == 76 and len(set(full)) == 76
    assert set(v3.REFERENCE.values()) <= set(full)
    assert sum("placebo@fill" in t.notes for t in v3.trials_v3()) == 6


def test_round2_signals_do_not_look_ahead():
    coins = _bd_market()
    full, cut_i = Market(coins), 120
    cut = Market(_truncate(coins, T0 + cut_i * H))
    for t in v3.trials_v3():
        if "reentry" in t.name:
            continue                        # re-entries depend on the base trade's own (causal) stop-out
        for s, c in full.coins.items():
            a = {(i, p.stop, p.entry) for i, p in t.fn(full, c, t.params) if i <= cut_i}
            b = {(i, p.stop, p.entry) for i, p in t.fn(cut, cut.coins[s], t.params)}
            assert a == b, t.name


def test_continuation_counts_two_r_before_back_to_entry():
    m = Market(_synthetic())
    c = m.coins["AAA/USDT:USDT"]
    for k in range(100, 130):
        px = 100.0 if k <= 102 else 95.0                            # stays below entry after +1R
        c.rows[k].update(o=px, h=px + 0.2, l=px - 0.2, c=px)
    base = {"symbol": "AAA/USDT:USDT", "entry_i": 100, "entry_px": 100.0, "risk_pct": 5.0, "stop0": 105.0,
            "time_stop": 12}
    c.rows[102].update(l=94.0, c=95.0)                              # +1R
    c.rows[104].update(l=89.0, c=90.0)                              # +2R before any return to entry
    assert r3.continuation([base], m)["p2"] == 1.0
    c.rows[103].update(h=100.5)                                     # back to entry first
    out = r3.continuation([base], m)
    assert out["p2"] == 0.0 and out["back_pct"] == 100


def test_paired_diff_and_verdict_follow_the_failures_bar():
    a = [{"symbol": "X", "signal_i": k, "r": 1.0, "day": f"d{k}", "entry_ts": k} for k in range(6)]
    b = [{"symbol": "X", "signal_i": k, "r": 0.5 + 0.1 * k, "day": f"d{k}", "entry_ts": k} for k in range(6)]
    d = r3.paired_diff(a, b, mid=3)
    assert d["n"] == 6 and d["d"] == pytest.approx(0.25) and d["is"] == pytest.approx(0.4)
    v = {"all": {"n": 100, "t_day": 1.0}, "is": {"n": 50, "avg_r": 0.1}, "oos": {"n": 50, "avg_r": 0.1},
         "paired_time": {"is": 0.1, "oos": 0.1}, "stress_r": 0.0, "edge_vs_placebo": 0.1, "edge_vs_time": 0.1,
         "dsr": 0.2}
    assert r3.verdict2(v) == "adopt-to-shadow"
    assert r3.verdict2({**v, "oos": {"n": 20, "avg_r": 1.0}}) == "needs more data"
    assert r3.verdict2({**v, "stress_r": -0.01}) == "reject"
