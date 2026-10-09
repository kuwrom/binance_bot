"""The next-level check (exit_odds.py): odds to the next target vs the give-back to the stop, net of fees and funding,
in reassess and in the ladder's shadow rows. Advice only: nothing here trades."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from shortbot import exit_odds as xo
from shortbot.journal import Journal
from shortbot.positioning import reassess
from shortbot.probability.baseline import short_geometry

NOW = datetime(2026, 10, 6, 17, 40, tzinfo=UTC)


def flat_1h(px, n=60, rng=0.01):
    return [[i, px, px * (1 + rng), px * (1 - rng), px, 1] for i in range(n)]


def nmr_trade(**kw):
    """NMR #29 at its best price (learning #97): T1 15.0 close below, the stop 17.62 far above, 1.4h into 12h."""
    t = {"id": 29, "symbol": "NMR/USDT:USDT", "entry_price": 16.341, "initial_stop": 17.62, "amount": 10,
         "mfe_pct": 6.6, "opened_at": (NOW - timedelta(hours=1.4)).isoformat(), "targets_hit": [],
         "plan": {"targets": [{"price": 15.0, "fraction": 0.5}], "time_stop_hours": 12}}
    t.update(kw)
    return t


NMR_POS = {"mark": 15.258, "contracts": 10, "exchange_stop": 17.62}


def kinds(r):
    return [a["action"] for a in r["actions"]]


def test_nmr_like_peak_gets_protect_profit_with_breakeven_odds():
    # NMR paid -1.36% per 4h: shorts pay. Close T1 (0.2R), ~1.85R back to the stop.
    r = reassess(nmr_trade(), NMR_POS, flat_1h(15.3), -0.0136, 12.79, NOW, sigma_h=0.015, funding_interval_h=4)
    nl = r["next_level"]
    assert nl["target"] == 15.0 and nl["p_target_first"] > 0.7 and nl["reward_R"] < 0.25 < 1.5 < nl["giveback_R"]
    assert nl["hours"] == pytest.approx(10.6) and nl["ev_R_net"] < 0 < nl["ev_R_bracket"]
    assert nl["funding_R_expected"] > 0 and nl["expected_hold_h"] < nl["hours"]   # funding runs only until the exit
    pp = next(a for a in r["actions"] if a["action"] == "protect_profit")
    assert pp["breakeven_stop"] == 16.341
    assert pp["at_breakeven"]["giveback_R"] < nl["giveback_R"]
    assert {"protect_profit", "funding_drag", "tighten_or_take_profit"} <= set(kinds(r))   # existing actions stay


def test_geometry_alone_protects_profit_when_the_target_is_out_of_reach_in_the_time_left():
    t = {"symbol": "X/USDT:USDT", "entry_price": 104.0, "initial_stop": 108.0, "amount": 1, "mfe_pct": 4,
         "opened_at": (NOW - timedelta(hours=10)).isoformat(), "targets_hit": [],
         "plan": {"targets": [{"price": 90.0}], "time_stop_hours": 12}}
    r = reassess(t, {"mark": 100.0, "contracts": 1, "exchange_stop": 106.0}, flat_1h(100), 0.0, 4.0, NOW,
                 sigma_h=0.03, funding_interval_h=8)
    nl = r["next_level"]   # 2h left: T1 2.5R away is out of reach, the stop 1.5R away is not
    assert nl["funding_R_expected"] == 0 and nl["p_target_first"] < 0.05 < nl["p_stop_first"]
    assert nl["ev_R_bracket"] < -0.1
    assert "protect_profit" in kinds(r) and "funding_drag" not in kinds(r)


def test_kaia_like_hourly_funding_gives_funding_drag_not_protect_profit():
    # 1h settlement at -0.2% per hour: the carry swamps a 1R target with ~0.3 odds; flat at entry (not in profit)
    t = {"symbol": "KAIA/USDT:USDT", "entry_price": 0.1, "initial_stop": 0.105, "amount": 1000, "mfe_pct": 0,
         "opened_at": NOW.isoformat(), "targets_hit": [], "plan": {"targets": [{"price": 0.095}],
                                                                  "time_stop_hours": 24}}
    r = reassess(t, {"mark": 0.1, "contracts": 1000, "exchange_stop": 0.105}, flat_1h(0.1), -0.002, 5.0, NOW,
                 sigma_h=0.01, funding_interval_h=1)
    nl = r["next_level"]
    assert nl["funding_interval_h"] == 1 and nl["funding_R_expected"] > nl["expected_reward_R"] > 0
    fd = next(a for a in r["actions"] if a["action"] == "funding_drag")
    assert "per 1h" in fd["why"] and "protect_profit" not in kinds(r)
    same_8h = reassess(t, {"mark": 0.1, "contracts": 1000, "exchange_stop": 0.105}, flat_1h(0.1), -0.002 / 8, 5.0,
                       NOW, sigma_h=0.01, funding_interval_h=8)["next_level"]
    assert same_8h["funding_R_expected"] == pytest.approx(nl["funding_R_expected"] / 64, abs=0.002)


def test_plan_intact_adds_nothing_and_no_sigma_means_no_block():
    t = nmr_trade(entry_price=15.3, initial_stop=16.0, mfe_pct=0, plan={"targets": [{"price": 14.6}],
                                                                         "time_stop_hours": 24})
    pos = {"mark": 15.3, "contracts": 10, "exchange_stop": 16.0}
    r = reassess(t, pos, flat_1h(15.3, rng=0.002), 0.0001, 7.0, NOW, sigma_h=0.01, funding_interval_h=8)
    assert r["next_level"]["funding_R_expected"] < 0                    # positive funding: the short earns it
    assert not {"protect_profit", "funding_drag"} & set(kinds(r))
    assert reassess(t, pos, flat_1h(15.3, rng=0.002), 0.0001, 7.0, NOW)["next_level"] is None


def test_next_target_skips_hit_and_crossed_levels_and_none_left_flags_the_carry():
    plan = [15.0, 14.0, 15.4]
    assert xo.pick_target("short", 15.2, plan, []) == 15.0             # 15.4 is above the price: not a short's target
    assert xo.pick_target("short", 15.2, plan, [15.0]) == 14.0
    assert xo.pick_target("long", 15.2, plan, []) == 15.4
    nl = xo.next_level("short", 13.5, 14.2, None, 1.0, 0.01, 24, -0.001, 8)
    assert nl["target"] is None and nl["ev_R_net"] is None and 0 < nl["p_stop_first"] < 1
    assert [a["action"] for a in xo.actions(nl, 2.0, 16.0)] == ["funding_drag"]


def test_long_side_mirrors_the_short_block():
    lg = xo.next_level("long", 100.0, 95.0, 110.0, 5.0, 0.01, 48, 0.0005, 8)
    ref = short_geometry(100.0, 95.0, 110.0, 0.01, 48, side="long")
    assert lg["p_target_first"] == round(ref["p_target"], 4) and lg["reward_R"] == 2.0 and lg["giveback_R"] == 1.0
    assert lg["funding_R_expected"] > 0                                 # a positive rate: longs pay
    assert xo.next_level("long", 100.0, 101.0, 110.0, 5.0, 0.01, 48, 0.0, 8) is None   # stop on the wrong side


def test_expected_hold_matches_the_strip_exit_time_and_caps_at_the_window():
    s = 0.05
    long_run = xo.expected_hold("short", 1.0, 1.05, 0.90, s, 24 * 365)
    assert long_run == pytest.approx(0.0488 * 0.1054 / s**2, rel=0.05)   # a·b/σ² with no time limit
    assert xo.expected_hold("short", 1.0, 1.05, 0.90, s, 1.0) < 1.0


def test_horizon_is_the_plans_time_left_or_24h():
    assert xo.horizon_hours({"time_stop_hours": 12}, 1.4) == pytest.approx(10.6)
    assert xo.horizon_hours({"time_stop_hours": 12}, 30) == 1.0
    assert xo.horizon_hours({}, 5) == 24.0


async def test_market_inputs_never_raise(monkeypatch):
    import shortbot.probability.engine as eng

    async def sig(ctx, hours=24.0, scale=1.0, model=None, har=None):
        return {"used": 0.02 * scale, "hours": hours, "model": model, "har_logged": har}

    async def broken():
        raise RuntimeError("fundingInfo down")

    async def hourly():
        return {"KAIAUSDT": {"interval_h": 1, "cap": 0.02}}

    monkeypatch.setattr(eng, "sigma_hourly", sig)
    _, s, iv = await xo.market_inputs(SimpleNamespace(funding_intervals=hourly), "KAIA/USDT:USDT", None, 6.0)
    assert (s["used"], s["hours"], s["model"], s["har_logged"], iv) == (0.02, 6.0, "ewma", True, 1)
    _, _, iv = await xo.market_inputs(SimpleNamespace(funding_intervals=hourly), "BTC/USDT:USDT")
    assert iv == 8                                                       # not listed = the default 8h
    _, _, iv = await xo.market_inputs(SimpleNamespace(funding_intervals=broken), "KAIA/USDT:USDT")
    assert iv is None and xo.next_level("short", 1.0, 1.05, 0.95, 0.05, 0.01, 24, -0.001, iv)[
        "funding_interval_read"] is False


async def test_ladder_shadow_rows_log_the_next_level_without_changing_decisions(monkeypatch):
    import shortbot.market as market
    import shortbot.probability.engine as eng
    import shortbot.signals as signals
    import shortbot.tape as tape
    from shortbot import ladder

    class Ctx:
        async def candles(self, tf, n):
            return flat_1h(15.3, n)

        async def oi_history(self, period, n):
            return [{"oi": 100.0}] * n

    async def evaluate(ctx):
        return SimpleNamespace(features={"funding_rate": -0.0136, "funding_interval_h": 4, "funding_8h": -0.027},
                               vetoes=[])

    async def sig(ctx, hours=24.0, scale=1.0, model=None, har=None):
        return {"used": 0.015}

    monkeypatch.setattr(market, "DataHub", lambda ex: SimpleNamespace(context=lambda s: Ctx()))
    monkeypatch.setattr(signals, "evaluate", evaluate)
    monkeypatch.setattr(eng, "sigma_hourly", sig)
    monkeypatch.setattr(tape, "classify", lambda f: {"state": "post_pump_decay"})
    j = Journal(":memory:")
    tid = j.create_trade("NMR/USDT:USDT", "pump_fade", "t", 17.62, 1, plan=nmr_trade()["plan"])
    j.activate_trade(tid, 16.341, 10, 163.41)
    pos = {"symbol": "NMR/USDT:USDT", "entry": 16.341, "mark": 15.258, "contracts": 10, "exchange_stop": 17.62}
    out = await ladder.build_ladder(SimpleNamespace(limits=None), j, pos)
    await j.flush()
    rows = j.db.execute("SELECT variant, action, next_level FROM ladder_shadow WHERE trade_id=?", (tid,)).fetchall()
    assert len(rows) == len(ladder.VARIANTS)
    logged = json.loads(rows[0][2])
    assert logged["target"] == 15.0 and logged["funding_interval_h"] == 4 and logged == out["next_level"]
    assert {r[1] for r in rows} <= {"hold", "wait", "add", "reduce", "stopped"}


def test_shorten_counts_an_hourly_coins_funding_8x_per_8h():
    # -0.03% per settlement over the 21h left: ~0.08% of notional on 8h settlement (under 0.1R), 8x that hourly
    t = {"symbol": "KAIA/USDT:USDT", "entry_price": 0.1, "initial_stop": 0.105, "amount": 1000, "mfe_pct": 0,
         "opened_at": (NOW - timedelta(hours=3)).isoformat(), "targets_hit": [], "plan": {"time_stop_hours": 24}}
    p = {"mark": 0.1, "contracts": 1000, "exchange_stop": 0.105}
    slow = reassess(t, p, flat_1h(0.1), -0.0003, 5.0, NOW, funding_interval_h=8)
    fast = reassess(t, p, flat_1h(0.1), -0.0003, 5.0, NOW, funding_interval_h=1)
    assert "shorten" not in kinds(slow) and "shorten" in kinds(fast)
    assert fast["funding_8h"] == pytest.approx(-0.0024) and slow["funding_8h"] == pytest.approx(-0.0003)
    assert "shorten" not in kinds(reassess(t, p, flat_1h(0.1), -0.0003, 5.0, NOW))   # interval unread: 8h
