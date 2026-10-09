"""The advisory next-level pass (exit_marks.py, guardian_prob.py): one row per open position per closed 5m bar, Monte
Carlo at 15m bars, failures isolated per position, reassess reading the newest row. Shadow only."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from shortbot import exit_marks as xm
from shortbot import exit_odds
from shortbot.journal import Journal

SYM, BAD = "NMR/USDT:USDT", "BAD/USDT:USDT"
BAR15 = 1_760_000_400_000 // 900_000 * 900_000


def journal_with_trades(*symbols):
    j = Journal(":memory:")
    for s in symbols:
        tid = j.create_trade(s, "pump_fade", "t", 17.62, 1, plan={"targets": [{"price": 15.0}], "time_stop_hours": 12,
                                                                  "analyst_p": 0.45})
        j.activate_trade(tid, 16.341, 10, 163.41)
    return j


def pos(sym, mark=15.258):
    return {"symbol": sym, "mark": mark, "entry": 16.341, "contracts": 10, "exchange_stop": 17.62}


class Flow:
    async def flow_candles(self, tf, n):
        import numpy as np
        rng = np.random.default_rng(1)
        c = 15.258 * np.exp(np.cumsum(rng.normal(0, 0.004, 400)))
        return [[i * 300_000, x, x, x, x, 1] for i, x in enumerate(c)]


def stub_inputs(monkeypatch, fail=()):
    async def inputs(ex, symbol, journal=None, hours=24.0):
        if symbol in fail:
            raise RuntimeError("klines down")
        return SimpleNamespace(context=lambda s: Flow()), {"used": 0.015, "ewma": 0.017, "har": 0.015,
                                                           "model": "har", "scale": 1.0}, 4
    monkeypatch.setattr(exit_odds, "market_inputs", inputs)


async def funding(sym):
    return {"fundingRate": -0.0136}


EX = SimpleNamespace(public=SimpleNamespace(fetch_funding_rate=funding))


def test_due_bar_is_each_closed_5m_bar_once():
    assert xm.due_bar(BAR15 + 1000, None) == BAR15
    assert xm.due_bar(BAR15 + 299_000, BAR15) is None
    assert xm.due_bar(BAR15 + 300_000, BAR15) == BAR15 + 300_000


async def test_a_pass_writes_one_row_per_position_and_a_failure_stays_local(monkeypatch):
    stub_inputs(monkeypatch, fail={BAD})
    j = journal_with_trades(SYM, BAD)
    n = await xm.run_pass(EX, j, [pos(SYM), pos(BAD), pos("ZZZ/USDT:USDT")], BAR15)   # ZZZ: no journal trade
    await j.flush()
    assert n == 1
    (sym, data), = j.db.execute("SELECT symbol, data FROM next_level_marks").fetchall()
    d = json.loads(data)
    assert sym == SYM and d["target"] == 15.0 and d["analyst_p"] == 0.45 and d["sigma"]["model"] == "har"
    assert d["funding_interval_h"] == 4 and d["funding_rate_read"] and d["ev_R_net"] < 0
    assert {"protect_profit", "funding_drag"} <= set(d["advice"])
    assert d["mc"]["p_target_first"] > 0.5                       # a 15m bar: Monte Carlo too


async def test_a_slow_pass_stops_inside_its_budget_and_keeps_what_it_has(monkeypatch):
    import asyncio
    stub_inputs(monkeypatch)
    slow = "SLOW/USDT:USDT"
    real = exit_odds.market_inputs

    async def inputs(ex, symbol, journal=None, hours=24.0):
        if symbol == slow:
            await asyncio.sleep(10)   # a hung read: cut at the pass budget left, never the lane's timeout
        return await real(ex, symbol, journal, hours)
    monkeypatch.setattr(exit_odds, "market_inputs", inputs)
    monkeypatch.setattr(xm, "PASS_BUDGET_S", 1.3)
    j = journal_with_trades(SYM, slow, "LAST/USDT:USDT")
    n = await asyncio.wait_for(xm.run_pass(EX, j, [pos(SYM), pos(slow), pos("LAST/USDT:USDT")], BAR15 + 300_000), 5)
    await j.flush()
    assert n == 1 and [r[0] for r in j.db.execute("SELECT symbol FROM next_level_marks")] == [SYM]


async def test_closed_forms_only_between_15m_bars_and_latest_reads_the_newest(monkeypatch):
    stub_inputs(monkeypatch)
    j = journal_with_trades(SYM)
    await xm.run_pass(EX, j, [pos(SYM)], BAR15)
    await xm.run_pass(EX, j, [pos(SYM, 15.4)], BAR15 + 300_000)
    await j.flush()
    tid = j.active_trade(SYM)["id"]
    last = xm.latest(j, tid)
    assert last["price"] == 15.4 and "mc" not in last and last["age_min"] > 0
    assert xm.latest(j, tid + 99) is None


async def test_guardian_job_marks_each_bar_once(monkeypatch):
    from shortbot.guardian_prob import ProbJobs
    stub_inputs(monkeypatch)
    j = journal_with_trades(SYM)

    async def positions():
        return [pos(SYM)]
    g = ProbJobs()
    g.ex, g.j, g.app = EX, j, SimpleNamespace(trader=SimpleNamespace(positions=positions))
    monkeypatch.setattr("time.time", lambda: BAR15 / 1000 + 5)
    await g.next_level_marks()
    await g.next_level_marks()
    await j.flush()
    assert j.db.execute("SELECT COUNT(*) FROM next_level_marks").fetchone()[0] == 1 and g._nl_bar == BAR15


async def test_guardian_recal_job_stores_a_verdict():
    from shortbot.guardian_prob import ProbJobs
    from shortbot.probability import recal
    g = ProbJobs()
    g.j = Journal(":memory:")
    await g.prob_recal()
    await g.j.flush()
    v = json.loads(g.j.get(recal.KV))
    assert v["applied"] is False and v["scale"] == 1.0 and "fitted_at" in v


def test_pure_reassess_without_sigma_has_no_block_and_the_horizon_is_the_time_left():
    from shortbot.positioning import reassess
    now = datetime.now(UTC)
    t = {"symbol": SYM, "entry_price": 16.341, "initial_stop": 17.62, "amount": 10, "mfe_pct": 0,
         "opened_at": (now - timedelta(hours=1)).isoformat(), "plan": {"time_stop_hours": 12}, "targets_hit": []}
    r = reassess(t, pos(SYM), [[i, 15, 15.1, 14.9, 15, 1] for i in range(60)], None, 5.0, now)
    assert r["next_level"] is None                               # no σ given: the pure call has no block
    assert pytest.approx(exit_odds.trade_horizon(t, now)) == 11.0
