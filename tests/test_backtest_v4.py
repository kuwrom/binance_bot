"""Round-3 backtest logic: the live-veto checks as as-of rules, blocked / allowed partitions, the OI flush and squeeze
events, and the veto verdict (docs/FAILURES.md Round 3)."""

import pytest

from shortbot.backtest import report_v4 as r4
from shortbot.backtest import strategies_v4 as v4
from shortbot.backtest.features import Market
from shortbot.backtest.strategies_v3 import ledger as ledger_v3
from tests.test_backtest import T0, _synthetic, _truncate

H = 3_600_000
AAA = "AAA/USDT:USDT"


def _m():
    return Market(_synthetic())


def test_round3_ledger_adds_47_uniquely_named_trials_and_base_names_exist():
    names = [t.name for t in v4.trials_v4()]
    assert len(names) == 47 == len(set(names))
    full = [t.name for t in v4.ledger()]
    assert len(full) == len(ledger_v3()) + 47 == 123 and len(set(full)) == 123
    assert {v4.base_name(k) for k in v4.BASES} <= set(full)


def test_premium_buckets_follow_the_pre_registered_edges():
    c = _m().coins[AAA]
    for p, want in ((-0.02, "le-1%"), (-0.01, "le-1%"), (-0.005, "-1..0%"), (0.0, "-1..0%"), (0.002, "0..+0.3%"),
                    (0.003, "0..+0.3%"), (0.004, "gt+0.3%")):
        c.rows[100]["prem"] = p
        assert v4.prem_bucket(c, 100) == want
    c.rows[100]["prem"] = None
    assert v4.prem_bucket(c, 100) is None and v4.prem_ok(None, c, 100)


def test_negative_funding_veto_fires_on_a_negative_print_or_a_1h_interval():
    m = _m()
    c = m.coins[AAA]
    i, t_close = 100, c.rows[100]["ts"] + H
    c.funding = [[t_close - 16 * H, 0.0001], [t_close - 8 * H, 0.0001]]
    assert not v4.neg_funding(m, c, i)
    c.funding[-1][1] = -0.00001
    assert v4.neg_funding(m, c, i)
    c.funding = [[t_close - 2 * H, 0.0001], [t_close - H, 0.0001]]          # capped: prints every hour
    assert v4.neg_funding(m, c, i)
    c.funding = [[t_close + H, -0.01]]                                      # a future print is not known yet
    assert not v4.neg_funding(m, c, i)


def _flat(c, a, b, px=100.0, oi=5e6):
    for k in range(a, b):
        c.rows[k].update(o=px, h=px * 1.002, l=px * 0.998, c=px, oi=oi)
    c.atr = [1.0] * len(c.rows)


def test_flush_and_squeeze_need_a_two_atr_move_with_oi_contracts_down_three_percent():
    m = _m()
    c = m.coins[AAA]
    _flat(c, 90, 110)
    c.rows[100].update(c=97.0, l=96.5, oi=5e6 * 0.97 * 0.96)             # -3%, contracts -4%
    assert v4.oi_move(c, 100, -1) and not v4.oi_move(c, 100, 1)
    assert v4.flush_recent(m, c, 102) and not v4.flush_recent(m, c, 103)  # the veto looks back 3 bars
    c.rows[100].update(oi=5e6 * 0.97)                                     # USD OI down 3% = contracts flat
    assert not v4.oi_move(c, 100, -1)
    c.rows[100].update(c=98.5, l=98.0, oi=5e6 * 0.985 * 0.9)              # -1.5% < 2 ATR
    assert not v4.oi_move(c, 100, -1)
    c.rows[100].update(c=103.0, h=103.5, oi=5e6 * 1.03 * 0.95)            # squeeze: up 3%, contracts -5%
    assert v4.oi_move(c, 100, 1)
    ev = [i for i, _ in v4.oi_event_short(m, c, {"sign": 1, "time": 24})]
    assert 100 in ev


def test_taker_burst_is_a_z_above_two_with_price_up():
    m = _m()
    c = m.coins[AAA]
    for k in range(70, 101):
        c.rows[k].update(qv=1e6, tbq=5e5 + (1e4 if k % 2 else -1e4))
    c.rows[100].update(tbq=8e5, o=100.0, c=101.0)
    assert v4.ti_z(c, 100) > 2 and v4.taker_burst(m, c, 100)
    c.rows[100].update(c=99.0)                                            # buying burst but price fell: no veto
    assert not v4.taker_burst(m, c, 100)


def test_spot_led_and_perp_led_follow_the_live_rules():
    m = _m()
    c = m.coins[AAA]
    _flat(c, 90, 101)
    for k in range(94, 101):
        c.rows[k].update(spot_c=100.0, spot_qv=1e6, prem=0.0)
    c.extra["spot_tbq"] = {c.rows[k]["ts"]: 6e5 for k in range(94, 101)}  # spot TI +0.2
    c.rows[100].update(c=102.0, spot_c=102.5, prem=-0.0005)
    assert v4.spot_led(m, c, 100) and not v4.perp_led(m, c, 100)
    c.rows[100].update(prem=0.0005)                                       # basis above 0: not spot-led
    assert not v4.spot_led(m, c, 100)
    c.rows[100].update(c=103.0, spot_c=101.0, prem=0.0015)                # perp outruns spot, basis +15bp
    assert v4.perp_led(m, c, 100)
    c.extra["spot_tbq"] = {}                                              # no spot flow data: neither fires
    assert not v4.perp_led(m, c, 100) and not v4.spot_led(m, c, 100)


def test_blocked_and_allowed_partition_the_base_signals():
    m = _m()
    base = {t.name: t for t in ledger_v3()}
    for t in v4.trials_v4():
        if "|" not in t.name or ":blocked" not in t.name:
            continue
        key, rest = t.name.split("|", 1)
        other = next(x for x in v4.trials_v4() if x.name == t.name.replace(":blocked", ":allowed"))
        full = base[v4.base_name(key)]
        for c in m.coins.values():
            b = {i for i, _ in t.fn(m, c, t.params)}
            a = {i for i, _ in other.fn(m, c, other.params)}
            assert not a & b and a | b == {i for i, _ in full.fn(m, c, full.params)}, t.name


def test_round3_signals_do_not_look_ahead():
    coins, cut_i = _synthetic(), 120
    full, cut = Market(coins), Market(_truncate(coins, T0 + cut_i * H))
    for t in v4.trials_v4():
        for s, c in full.coins.items():
            a = {(i, p.stop) for i, p in t.fn(full, c, t.params) if i <= cut_i}
            b = {(i, p.stop) for i, p in t.fn(cut, cut.coins[s], t.params)}
            assert a == b, t.name


def _v(n_oos, r_oos, e_oos, **kw):
    return {"all": {"n": 100, "t_day": 1.0}, "is": {"n": 50, "avg_r": 0.1}, "oos": {"n": n_oos, "avg_r": r_oos},
            "paired_time": {"is": 0.1, "oos": e_oos}, "stress_r": -0.1, "edge_vs_placebo": 0.1,
            "edge_vs_time": 0.1, "dsr": 0.1, **kw}


def test_veto_verdict_keep_drop_invert_and_needs_data():
    alw = _v(200, -0.10, 0.05)
    assert r4.veto_verdict(_v(50, -0.20, 0.00), alw) == "KEEP"
    assert r4.veto_verdict(_v(50, -0.20, 0.10), alw) == "DROP"            # worse absolute, better relative
    assert r4.veto_verdict(_v(50, -0.05, 0.00), alw) == "DROP"
    assert r4.veto_verdict(_v(50, 0.10, 0.20, stress_r=0.0), alw) == "INVERT-TO-SHADOW"
    assert r4.veto_verdict(_v(29, -0.5, -0.5), alw) == "needs more data"


def test_diff_t_and_funding_in_r():
    a = [{"r": 1.0 + 0.1 * (k % 3), "day": f"d{k}"} for k in range(9)]
    b = [{"r": 0.0 + 0.1 * (k % 3), "day": f"d{k}"} for k in range(9)]
    d, t = r4.diff_t(a, b)
    assert d == pytest.approx(1.0) and t > 10
    f = r4.funding_r([{"funding_pct": -0.5, "risk_pct": 5.0}, {"funding_pct": 0.1, "risk_pct": 5.0}])
    assert f["n"] == 2 and f["mean"] == pytest.approx(-0.04) and f["paid_ge_0.1R"] == 50
