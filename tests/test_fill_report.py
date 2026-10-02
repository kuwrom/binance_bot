"""#35 report: each recorded fill against its mainnet reference in bps (positive = worse for the short), next to what
backtest/sim.py assumes for the same fill (20 bp / sqrt(24h volume $M), clipped 1-150 bp, 2x on stops, 0 on limits),
by fill type, setup and entry type; and the calibration hook, which refuses to calibrate below its sample."""

import json
import math
from dataclasses import replace

import pytest
from pytest import approx

from shortbot import fill_report as fr
from shortbot.backtest.sim import COSTS, Plan, simulate
from shortbot.journal import Journal

DAY = 86_400_000
T0 = 1_790_000_000_000


def trade(j, setup="pump_fade", entry_type=None, fills=(), vol=1e6, missed=None, closed=True, qv=None, refs=True):
    feats = {**({"entry_type": entry_type} if entry_type else {}), **({"quote_volume": qv} if qv else {})}
    tid = j.create_trade("X/USDT:USDT", setup, "t", 1.1, 2, {}, feats)
    j.activate_trade(tid, 1.0, 10, 10)
    if closed:
        j.close_trade(tid, 0.95, 1.0, "x")
    if refs:
        doc = {"v": 1, "fills": list(fills), "missed": missed or {}, **({"vol24_usd": vol} if vol else {})}
        j.db.execute("UPDATE trades SET fill_refs=? WHERE id=?", (json.dumps(doc), tid))
        j.db.commit()
    return tid


def book(leg, kind, px, mid, ms=T0, order=None, lag=150, half=0.0005):
    return {"leg": leg, "type": kind, "order": order or f"{leg}-{kind}-{px}-{ms}", "px": px, "qty": 10, "ms": ms,
            "src": "book", "ref_ms": ms + lag, "bid": mid * (1 - half), "ask": mid * (1 + half), "mark": mid}


def last(leg, kind, px, ref, ms=T0, order=None, lag=-200, trigger=None):
    return {"leg": leg, "type": kind, "order": order or f"{leg}-{kind}-{px}-{ms}", "px": px, "qty": 10, "ms": ms,
            "src": "trade", "ref_ms": ms + lag, "last": ref, "at": "bid", **({"trigger": trigger} if trigger else {})}


def by_id(rep):
    return {r["trade_id"]: r for r in rep["trades"]}


def test_each_fill_is_signed_worse_for_the_short_and_set_against_the_models_slippage():
    j = Journal(":memory:")
    a = trade(j, fills=[book("entry", "market", 0.999, 1.0), book("exit", "market", 0.951, 0.95)])
    b = trade(j, "exp_level_fade", "limit", [last("entry", "limit", 1.04, 1.03),
                                              last("exit", "exchange", 1.10, 1.098, trigger=1.10)])
    rep = fr.report(j)
    m, lim = by_id(rep)[a], by_id(rep)[b]
    assert (m["entry_bps"], m["exit_bps"]) == (approx(10.0), approx(10.53, abs=0.01))   # sold below / bought above
    assert (m["model_entry_bps"], m["model_exit_bps"]) == (approx(20.0), approx(20.0))   # 20 bp / sqrt($1M), 1x
    assert m["entry_type"] == "market" and m["excess_bps"] == approx(10.0 + 10.53 - 40.0, abs=0.01)
    assert (lim["entry_bps"], lim["model_entry_bps"]) == (approx(-97.09, abs=0.01), 0.0)  # a limit fills at its price
    assert (lim["exit_bps"], lim["model_exit_bps"]) == (approx(18.21, abs=0.01), approx(40.0))   # stops pay 2x
    assert rep["by_fill"]["entry/limit"]["mean_bps"] == approx(-97.09, abs=0.01)
    assert rep["by_fill"]["exit/exchange"]["model_bps"] == approx(40.0)
    assert set(rep["by_setup"]) == {"pump_fade", "exp_level_fade"} and set(rep["by_entry_type"]) == {"market", "limit"}
    assert rep["by_entry_type"]["limit"]["entry_bps"] == approx(-97.09, abs=0.01)
    mark_only = {**book("entry", "market", 1.0, 1.0), "bid": None, "ask": None, "mark": 1.01}
    assert fr.bps(mark_only) == approx(99.0, abs=0.1)


def test_the_model_clips_like_the_backtest_and_falls_back_to_the_entrys_quote_volume():
    j = Journal(":memory:")
    thin = trade(j, fills=[last("exit", "exchange", 1.0, 1.0)], vol=1e4)              # 20/sqrt(0.01) -> 150 cap
    deep = trade(j, fills=[book("entry", "market", 1.0, 1.0)], vol=None, qv=1e12)     # 0.02 bp -> 1 bp floor
    none = trade(j, fills=[book("entry", "market", 1.0, 1.0)], vol=None)
    rows = by_id(fr.report(j))
    assert rows[thin]["model_exit_bps"] == approx(2 * COSTS.slip_max_bps)
    assert rows[deep]["model_entry_bps"] == approx(COSTS.slip_min_bps)
    assert rows[none]["model_entry_bps"] is None and rows[none]["entry_bps"] == approx(0.0)


def test_stale_references_are_shown_but_kept_out_and_coverage_counts_what_was_lost():
    j = Journal(":memory:")
    a = trade(j, fills=[book("entry", "market", 0.999, 1.0, order="1"),
                        book("exit", "market", 0.951, 0.95, order="2", lag=9_000)],
              missed={"1": "timeout", "3": "error"})
    trade(j, refs=False)                                          # closed by a path that records no exit (a gap)
    trade(j, fills=[book("entry", "market", 1.0, 1.0)], closed=False)
    rep = fr.report(j)
    row = by_id(rep)[a]
    assert row["exit_bps"] is None and row["stale"] == 1 and row["entry_bps"] == approx(10.0)
    assert rep["by_fill"]["exit/market"]["n"] == 0
    assert rep["coverage"] == {"closed_trades": 2, "trades_with_refs": 2, "fills": 3, "stale": 1, "missed": 2,
                               "recovered": 1, "lost": 1, "lost_by_reason": {"error": 1},
                               "closed_without_exit_ref": 1}


def test_aggregates_are_clustered_by_day_and_say_when_they_are_only_descriptive():
    j = Journal(":memory:")
    for i, px in enumerate((0.999, 0.998, 0.997)):
        trade(j, fills=[book("entry", "market", px, 1.0, ms=T0 + i * DAY)])
    agg = fr.report(j)["by_fill"]["entry/market"]
    assert (agg["n"], agg["days"], agg["mean_bps"], agg["median_bps"]) == (3, 3, approx(20.0), approx(20.0))
    assert agg["ci95"] is not None and agg["model_bps"] == approx(20.0)
    assert "descriptive" in fr.report(j)["note"]


def test_journal_stats_carry_the_fill_quality_section_of_their_own_mode(tmp_path):
    demo, live = Journal(tmp_path / "j.db"), Journal(tmp_path / "j.db", "live")
    trade(demo, fills=[book("entry", "market", 0.999, 1.0)])
    trade(live, fills=[book("entry", "market", 0.990, 1.0)])
    fq = demo.stats()["fill_quality"]
    assert fq["coverage"]["trades_with_refs"] == 1 and fq["by_fill"]["entry/market"]["mean_bps"] == approx(10.0)
    assert demo.stats(all_modes=True)["fill_quality"]["coverage"]["trades_with_refs"] == 2


def synthetic(j, n, days, k=30.0):
    """Fills that cost exactly what the model says for slip constant k: market entries 1x, stop-outs 2x."""
    for i in range(n):
        vol = (0.25e6, 1e6, 4e6, 16e6)[i % 4]
        slip = k / math.sqrt(vol / 1e6) / 1e4
        ms = T0 + (i % days) * DAY
        trade(j, fills=[book("entry", "market", 1 - slip, 1.0, ms=ms, order=f"e{i}"),
                        last("exit", "exchange", 1 + 2 * slip, 1.0, ms=ms + 3_600_000, order=f"x{i}")], vol=vol)


def test_calibration_recovers_the_slippage_constant_from_logged_fills():
    j = Journal(":memory:")
    synthetic(j, 40, 25)
    trade(j, "pump_fade", "limit", [last("entry", "limit", 1.04, 1.03, order="lim")])     # no slippage modelled: out
    trade(j, fills=[book("entry", "market", 0.9, 1.0, order="stale", lag=60_000)])       # stale: out
    cal = fr.calibrate(j)
    assert cal["ok"] and (cal["k_bps"], cal["k_bps_median"]) == (approx(30.0), approx(30.0))
    assert (cal["n_fills"], cal["n_trades"], cal["days"], cal["current_k_bps"]) == (80, 40, 25, COSTS.slip_k_bps)
    assert cal["n_eff"] is not None and cal["venue"] == "demo"
    costs, info = fr.calibrated_costs(j)
    assert costs.slip_k_bps == approx(30.0) and info["ok"]
    assert (costs.slip_min_bps, costs.slip_max_bps, costs.stop_slip_mult) == (1.0, 150.0, 2.0)


@pytest.mark.parametrize("n,days", [(10, 10), (40, 12)])
def test_calibration_refuses_below_30_trades_or_20_days(n, days):
    j = Journal(":memory:")
    synthetic(j, n, days)
    cal = fr.calibrate(j)
    assert not cal["ok"] and "descriptive" in cal["note"] and cal["k_bps"] == approx(30.0)
    costs, info = fr.calibrated_costs(j)
    assert costs is COSTS and not info["ok"]


def test_calibration_with_no_fills_is_empty_not_an_error():
    cal = fr.calibrate(Journal(":memory:"))
    assert not cal["ok"] and cal["n_fills"] == 0 and cal["k_bps"] is None


def flat(*hl):
    rows = [{"ts": k * 3_600_000, "o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0, "qv": 1.0} for k in range(6)]
    for k, (h, lo) in enumerate(hl, 1):
        rows[k].update(h=h, l=lo)
    return rows


@pytest.mark.parametrize("plan,hl,kinds,reason", [
    (Plan(stop=110, time_stop=2), [], ("entry/market", "exit/time"), "time"),
    (Plan(stop=110), [(111, 100)], ("entry/market", "exit/stop"), "stop"),
    (Plan(stop=110, entry="limit", limit_px=101, targets=((96, 1.0),)), [(102, 100), (100, 95)],
     ("entry/limit", "exit/target"), "target"),
    (Plan(stop=110, entry="stop", limit_px=99, time_stop=2), [(100, 98)], ("entry/stop", "exit/time"), "time")])
def test_the_model_charges_each_fill_what_the_simulator_charges(plan, hl, kinds, reason):
    """Costs.charge is pinned to simulate(): a change to the sim's time-stop or stop charge fails here, not silently."""
    costs = replace(COSTS, funding=False, stop_slip_mult=3.0)
    r = simulate(flat(*hl), 0, plan, costs=costs, vol24_usd=1e6)
    assert r["reason"] == reason
    assert r["slip_pct"] == approx(sum(costs.charge(k) for k in kinds) * costs.slip(1e6) * 100)
    assert {fr.mult({"leg": k.split("/")[0], "type": k.split("/")[1]}, costs) for k in fr.SIM_KIND} == {0.0, 1.0, 3.0}
