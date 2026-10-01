"""Round-4 corrections (docs/failures/ROUND4.md): the volatility / liquidity-matched same-hour placebo and its
parity fixes, the gross paired edge, the effective-N Deflated Sharpe, and the 5-minute replay of the exits."""

import random
import zlib
from dataclasses import replace

import numpy as np
import pytest

from shortbot.backtest import placebo as P
from shortbot.backtest import trail5m, trials_eff
from shortbot.backtest.features import Market
from shortbot.backtest.report import build
from shortbot.backtest.run import Runner, run_all
from shortbot.backtest.sim import COSTS, FREE, Plan, simulate
from shortbot.backtest.stats import dsr, paired_edge, sharpe
from shortbot.backtest.strategies import trials
from shortbot.backtest.strategies_more import placebo_signals
from tests.test_backtest import T0, _synthetic

H = 3_600_000
AAA = "AAA/USDT:USDT"


def _market(n_extra=24, seed=3):
    """The synthetic market + n_extra quiet coins with spread-out volatility and volume."""
    rng, coins = random.Random(seed), _synthetic()
    for k in range(n_extra):
        sd, vol, px, perp = 0.002 + 0.002 * k, 1.5e6 * (1 + k % 6) ** 2, 50.0, []
        for j in range(160):
            o = px
            px *= 1 + rng.gauss(0, sd)
            perp.append({"ts": T0 + j * H, "o": o, "h": max(o, px) * (1 + sd), "l": min(o, px) * (1 - sd), "c": px,
                         "qv": vol, "n": 5, "tbq": vol / 2})
        coins.append({"symbol": f"Q{k:02d}/USDT:USDT", "onboard_ms": 1, "tags": [], "delist_ms": None, "perp": perp,
                      "oi": {}, "premium": {}, "funding": [], "spot": [], "ls_top": {}, "ls_global": {}})
    return Market(coins)


def _donor(m, sym=AAA, i=100, **kw):
    d = {"symbol": sym, "signal_i": i, "entry_i": i, "entry_kind": "market", "time_stop": 12, "be_r": 1.0,
         "trail_act_r": None, "trail_dist_r": 1.0, "meta": {"stop_atr": 1.5, "targets_r": ((1.0, 0.5), (2.0, 0.5))}}
    d.update(kw)
    return d


def test_matched_placebo_draws_from_the_donors_atr_decile_and_volume_bucket_at_the_same_hour():
    m = _market()
    d = _donor(m)
    donor = m.coins[AAA]
    xs = P.cross_section(m, donor.rows[100]["ts"])
    pa0 = P._pctile(xs.atrs, donor.atr[100])
    cell = {c.symbol for c, _ in xs.cells[(P._decile(pa0), P.vol_bucket(P.vol24_at(m, donor, 100)))]} - {AAA}
    out = P.matched_signals(m, [d], per=3, seed=1, mode="time")
    assert len(out) == 3 and all(c.symbol != AAA for c, _, _ in out)
    assert all(c.rows[i]["ts"] == donor.rows[100]["ts"] for c, i, _ in out)
    for c, i, pl in out:
        assert (c.symbol in cell) == (pl.meta["match"] == "cell")
        assert pl.meta["donor"] == (AAA, 100)
        assert pl.stop == pytest.approx(c.rows[i]["c"] * (1 + 1.5 * c.atr[i] / 100))
    if len(cell) >= 3:
        assert all(pl.meta["match"] == "cell" for _, _, pl in out)


def test_matched_placebo_falls_back_to_the_nearest_coins_when_the_cell_is_empty():
    m = _market()
    donor = m.coins[AAA]
    xs = P.cross_section(m, donor.rows[100]["ts"])
    xs.cells = {}                                                   # no coin shares the donor's cell
    pa0, pv0 = P._pctile(xs.atrs, donor.atr[100]), P._pctile(xs.vols, P.vol24_at(m, donor, 100))
    out = P.matched_signals(m, [_donor(m)], per=2, seed=1, mode="time")
    dist = sorted((abs(pa - pa0) + abs(pv - pv0), c.symbol) for c, _, pa, pv in xs.rows if c.symbol != AAA)
    assert [c.symbol for c, _, _ in out] == [s for _, s in dist[:2]]
    assert all(pl.meta["match"] == "nearest" for _, _, pl in out)


def test_matched_placebo_enters_limit_donors_at_the_fill_bar_and_keeps_the_funding_budget():
    m = _market()
    d = _donor(m, entry_kind="limit", entry_i=103, fund_budget_r=0.5, fund_follow_r=0.4)
    out = P.matched_signals(m, [d], per=3, seed=2, mode="time")
    ts = m.coins[AAA].rows[103]["ts"]
    assert out and all(c.rows[i]["ts"] == ts for c, i, _ in out)
    assert all(pl.fund_budget_r == 0.5 and pl.fund_follow_r == 0.4 and pl.meta["donor"] == (AAA, 100)
               for _, _, pl in out)
    coin = P.matched_signals(m, [d], per=2, seed=2, mode="coin")
    assert coin and all(c.symbol == AAA and pl.fund_budget_r == 0.5 for c, _, pl in coin)
    legacy = placebo_signals(m, [d], per=3, seed=2, mode="time")        # the legacy draw is untouched
    assert all(c.rows[i]["ts"] == m.coins[AAA].rows[100]["ts"] and pl.fund_budget_r is None for c, i, pl in legacy)


def test_paired_edge_reports_the_gross_edge_next_to_the_net_one():
    trades = [{"symbol": "X", "signal_i": k, "r": 0.2, "r_gross": 0.3, "day": f"d{k}", "entry_ts": k} for k in range(6)]
    ctl = [{"r": -0.3, "r_gross": 0.1, "meta": {"donor": ("X", k)}} for k in range(6) for _ in range(3)]
    pe = paired_edge(trades, ctl, split_ts=3)
    assert pe["edge"] == pytest.approx(0.5) and pe["edge_gross"] == pytest.approx(0.2)
    assert pe["is_gross"] == pytest.approx(0.2) and pe["oos_gross"] == pytest.approx(0.2)


def _book(rng, days, mu=0.0, n_per_day=3, base=None, noise=1.0):
    out = []
    for k, d in enumerate(days):
        for j in range(n_per_day):
            r = (base[k * n_per_day + j]["r"] if base else 0.0) + rng.gauss(mu, noise)
            out.append({"r": r, "day": d})
    return out


def test_clones_collapse_into_one_family_and_tiny_partitions_leave_v_sr():
    rng = random.Random(7)
    days = [f"2026-08-{k:02d}" for k in range(1, 29)]
    a = _book(rng, days, 0.05, n_per_day=5)
    books = {"a": a, "a_clone": [dict(t) for t in a], "a_near": _book(rng, days, base=a, n_per_day=5, noise=0.1),
             "b": _book(rng, days, -0.05, n_per_day=5), "tiny": _book(rng, days[:4], 0.9, n_per_day=3)}
    fams = trials_eff.families(books)
    assert sorted(len(f) for f in fams) == [1, 1, 3]
    assert sorted(next(f for f in fams if len(f) == 3)) == ["a", "a_clone", "a_near"]
    v, used = trials_eff.corrected_var_sr(books, fams, min_n=100)
    assert used == 2                                                   # family {a...} and b; tiny has n 12
    srs = [sharpe([t["r"] for t in books[n]]) for n in ("a", "a_clone", "a_near")]
    assert v == pytest.approx(np.var([np.mean(srs), sharpe([t["r"] for t in books["b"]])], ddof=1))
    out = trials_eff.deflate(books, "clustered")
    assert out["n_trials"] == 5 and out["n_eff"] == 3
    row = out["per_trial"]["a"]
    assert row["dsr"] == row["dsr_effN"] and row["dsr_effN"] >= row["dsr_rawN"]


def test_legacy_deflate_reproduces_the_old_report_formula():
    rng = random.Random(1)
    books = {f"t{k}": [{"r": rng.gauss(0.01 * k, 1), "day": f"d{j % 9}"} for j in range(20 + 10 * k)] for k in range(6)}
    out = trials_eff.deflate(books, "legacy")
    srs = [sharpe([t["r"] for t in v]) for v in books.values()]
    var = float(np.var(srs, ddof=1))
    assert out["var_sr"] == round(var, 4) and out["n_eff"] == 6
    assert out["per_trial"]["t3"]["dsr"] == dsr([t["r"] for t in books["t3"]], 6, var)["dsr"]


def test_average_linkage_merges_only_above_the_cut():
    c = np.array([[1, .9, .8, .1], [.9, 1, .75, .2], [.8, .75, 1, .1], [.1, .2, .1, 1.]])
    assert sorted(sorted(x) for x in trials_eff.average_linkage(c, 0.7)) == [[0, 1, 2], [3]]
    assert len(trials_eff.average_linkage(c, 0.95)) == 4


def test_run_all_matched_and_clustered_report_end_to_end():
    m = _market(n_extra=8)
    led = [t for t in trials() if t.strategy in ("pump_fade", "level_fade")]
    res = run_all(m, ledger=led, placebo="matched")
    assert res["placebo_mode"] == "matched"
    pt = [t for r in res["placebo_time"].values() for t in r["trades"]]
    assert pt and all(t["meta"]["match"] in ("cell", "nearest") for t in pt)
    for name, r in res["trials"].items():                            # non-exclusive: every draw is simulated
        donors = [t for t in r["trades"] if t["meta"].get("stop_atr")]
        assert len(res["placebo_time"].get(name, {}).get("trades", [])) == 3 * len(donors)
    rep = build(res, m, dsr_mode="clustered")
    assert rep["n_trials"] == len(led) and 1 <= rep["n_eff"] <= len(led)
    assert rep["placebo_mode"] == "matched" and rep["dsr_mode"] == "clustered"
    legacy = run_all(m, ledger=led[:1])
    name = led[0].name
    seed = zlib.crc32(name.encode()) % 10_000
    ps = placebo_signals(m, res["trials"][name]["trades"], per=3, seed=seed, mode="time")
    again = Runner(m).execute("x", sorted(ps, key=lambda x: (x[0].symbol, x[1])), "x")["trades"]
    assert [t["r"] for t in legacy["placebo_time"][name]["trades"]] == [t["r"] for t in again]


def _five(rows1h):
    """Split each 1h bar into 12 flat-ish 5m bars that reproduce its o / h / l / c (high at 5m bar 3, low at 8)."""
    out = []
    for b in rows1h:
        for k in range(12):
            px = b["o"] if k < 11 else b["c"]
            h, lo = (b["h"] if k == 3 else max(px, b["o"])), (b["l"] if k == 8 else min(px, b["o"]))
            out.append({"ts": b["ts"] + k * 300_000, "o": b["o"] if k == 0 else out[-1]["c"], "h": h, "l": lo,
                        "c": px, "qv": b["qv"] / 12})
    return out


def test_five_minute_replay_matches_1h_on_a_quiet_path_and_orders_events_inside_the_bar():
    r1 = [{"ts": k * H, "o": 100.0, "h": 100.5, "l": 99.5, "c": 100.0, "qv": 1e9} for k in range(40)]
    r5 = _five(r1)
    pl = Plan(stop=105.0, targets=((95.0, 1.0),), time_stop=6)
    a = simulate(r1, 10, pl, costs=COSTS)
    b = simulate(r5, 10 * 12 + 11, replace(pl, time_stop=72), costs=COSTS, vol24_usd=24e9)
    assert b["r"] == pytest.approx(a["r"]) and b["entry_px"] == pytest.approx(a["entry_px"])
    r1[12].update(h=106.0, l=94.0)          # stop and target in one hour: 1h assumes the stop
    r5 = _five(r1)
    for k in range(12 * 12, 12 * 12 + 12):  # ...but the 5m path reaches the target first
        r5[k].update(h=min(r5[k]["h"], 100.6), l=r5[k]["l"])
    r5[12 * 12 + 2].update(l=94.0)
    r5[12 * 12 + 8].update(l=99.5, h=106.0)
    assert simulate(r1, 10, pl, costs=FREE)["reason"] == "stop"
    t5 = simulate(r5, 10 * 12 + 11, replace(pl, time_stop=72), costs=FREE)
    assert t5["reason"] == "target" and t5["r"] == pytest.approx(1.0)


def test_five_minute_window_must_be_gap_free():
    m = _market(n_extra=0)
    c = m.coins[AAA]
    f = trail5m.Five.__new__(trail5m.Five)
    f.rows = _five(c.rows)
    f.pos = {r["ts"]: k for k, r in enumerate(f.rows)}
    assert f.start(c, 100, 12) == 100 * 12 + 11
    del f.rows[100 * 12 + 40]
    f.pos = {r["ts"]: k for k, r in enumerate(f.rows)}
    assert f.start(c, 100, 12) is None and f.start(c, 110, 12) is not None
