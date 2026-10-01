"""Trader against a strict fake exchange (real ccxt error classes, one closePosition stop, income records)."""

import pytest

from shortbot.journal import Journal
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks


def trader(prechecks=ok_prechecks, **kw):
    fake = FakeCcxt(**kw)
    t = Trader(FakeEx(fake), Journal(":memory:"), prechecks=prechecks, budget=neutral_budget)
    return t, fake


async def open_default(t, **kw):
    args = dict(stop_price=1.05, setup="pump_fade", thesis="lower high after +60%", notional=100)
    args.update(kw)
    return await t.open_short(SYM, **args)


async def test_open_on_one_way_account_with_real_error_classes():
    t, fake = trader()
    r = await open_default(t)
    assert r["ok"], r
    assert fake.position == 100 and list(fake.stops.values()) == [1.05]
    tr = t.j.get_trade(r["trade_id"])
    assert tr["status"] == "open" and tr["plan"]["time_stop_hours"] == 12 and tr["entry_features"]["rsi_1h"] == 88
    pos = (await t.positions())[0]
    assert pos["exchange_stop"] == 1.05 and not pos["UNPROTECTED"] and pos["trade_id"] == r["trade_id"]


async def test_hedge_account_switched_to_one_way():
    t, fake = trader(hedged=True)
    assert (await open_default(t))["ok"] and fake.hedged is False


async def test_vetoes_block_unless_overridden():
    async def vetoed(ex, s, tk):
        return {"vetoes": ["negative funding"], "regime": "hostile", "features": {}}
    t, fake = trader(prechecks=vetoed)
    r = await open_default(t)
    assert not r["ok"] and fake.orders == [] and "market regime is hostile" in r["vetoed"]
    r = await open_default(t, veto_override="testing whether regime filter costs us winners")
    assert r["ok"]
    assert t.j.get_trade(r["trade_id"])["entry_features"]["vetoes_overridden"]


async def test_unknown_setup_and_bad_plan_rejected():
    t, fake = trader()
    with pytest.raises(ValueError, match="unknown setup"):
        await open_default(t, setup="yolo")
    r = await open_default(t, plan={"targets": [{"price": 1.2, "fraction": 1}]})
    assert not r["ok"] and "below entry" in r["plan_errors"][0] and fake.orders == []


async def test_risk_rejection_places_no_orders():
    t, fake = trader()
    r = await open_default(t, stop_price=0.95)
    assert not r["ok"] and fake.orders == [] and "ABOVE" in r["rejected_by_risk"][0]


async def test_auto_size_respects_risk_cap():
    t, fake = trader(equity=1000)
    r = await open_default(t, stop_price=1.10, notional=None)
    assert r["ok"] and r["auto_sized"] and r["risk_at_stop_usdt"] <= 10.0


async def test_failed_stop_closes_and_journals_real_pnl():
    t, fake = trader()
    fake.fail_stop = 2
    r = await open_default(t)
    assert not r["ok"] and fake.position == 0
    tr = t.j.trades()[0]
    assert tr["status"] == "closed" and tr["fees"] < 0 and tr["pnl"] < 0   # fees paid on the round trip


async def test_stop_and_close_both_failing_alerts_naked():
    t, fake = trader()
    fake.fail_stop, fake.fail_close = 5, 5
    r = await open_default(t)
    assert "UNPROTECTED" in r["error"] and t.j.alerts()[0]["kind"] == "NAKED_POSITION"
    assert t.j.get_trade(r["trade_id"])["status"] == "open"   # guardian will see it and retry


async def test_entry_timeout_after_fill_still_protected():
    t, fake = trader()
    fake.fail_entry_after_fill = True
    r = await open_default(t)
    assert r["ok"] and fake.position == 100 and fake.stops


async def test_move_stop_swaps_single_closeposition_stop():
    t, fake = trader()
    await open_default(t)
    assert not (await t.move_stop(SYM, 1.08, "widen"))["ok"]
    r = await t.move_stop(SYM, 1.02, "lock")
    assert r["ok"] and list(fake.stops.values()) == [1.02]
    assert t.j.active_trade(SYM)["stop_price"] == 1.02 and t.j.active_trade(SYM)["initial_stop"] == 1.05


async def test_move_stop_failure_restores_old_stop():
    t, fake = trader()
    await open_default(t)
    fake.fail_stop = 1
    r = await t.move_stop(SYM, 1.02, "lock")
    assert not r["ok"] and "restored" in r["error"] and list(fake.stops.values()) == [1.05]


async def test_move_stop_drift_after_cancel_restores_old_stop():
    """ONDO #5: the cancel executed, then -1021; the position must not be left naked."""
    t, fake = trader()
    await open_default(t)
    fake.drift_on_cancel = 1
    r = await t.move_stop(SYM, 1.02, "trail")
    assert not r["ok"] and "restored" in r["error"] and list(fake.stops.values()) == [1.05]
    assert t.ex.resyncs >= 1


async def test_move_stop_drift_before_cancel_keeps_old_stop():
    t, fake = trader()
    await open_default(t)
    fake.drift_before_cancel = 1
    r = await t.move_stop(SYM, 1.02, "trail")
    assert not r["ok"] and "still in place" in r["error"] and list(fake.stops.values()) == [1.05]


async def test_move_stop_same_tick_is_a_noop_without_swapping():
    t, fake = trader()
    await open_default(t)
    before = dict(fake.stops)
    r = await t.move_stop(SYM, 1.0500001, "trail")
    assert not r["ok"] and r["noop"] and fake.stops == before


async def test_close_full_uses_income_pnl():
    t, fake = trader()
    r = await open_default(t)
    fake.price = 0.9
    res = await t.close(SYM, "target hit", "patience paid")
    assert res["ok"] and fake.stops == {}
    tr = t.j.get_trade(r["trade_id"])
    assert tr["status"] == "closed" and tr["gross_pnl"] == pytest.approx(10.0)
    assert tr["pnl"] == pytest.approx(10.0 - 100 * 1.0 * 0.0005 - 100 * 0.9 * 0.0005)
    assert tr["exit_price"] == pytest.approx(0.9)


async def test_partial_close_keeps_trade_open():
    t, fake = trader()
    await open_default(t)
    fake.price = 0.95
    r = await t.close(SYM, "first target", fraction=0.5)
    assert r["ok"] and fake.position == 50 and t.j.active_trade(SYM)["status"] == "open"


async def test_ensure_stop_restores_or_force_closes():
    t, fake = trader()
    await open_default(t)
    fake.stops.clear()
    assert (await t.ensure_stop(SYM, 10))["action"] == "stop_placed" and list(fake.stops.values()) == [1.05]
    fake.stops.clear()
    fake.price = 1.2
    assert (await t.ensure_stop(SYM, 10))["action"] == "closed" and fake.position == 0
    assert t.j.trades()[0]["status"] == "closed"


def test_protective_stop_detection_matches_real_ccxt_shape():
    from shortbot.trading import is_protective_stop
    real = {"type": "market", "side": "buy", "reduceOnly": True, "triggerPrice": 188.6,
            "info": {"orderType": "STOP_MARKET", "closePosition": True, "algoType": "CONDITIONAL"}}
    assert is_protective_stop(real)
    tp = {**real, "info": {**real["info"], "orderType": "TAKE_PROFIT_MARKET"}}
    assert not is_protective_stop(tp)
    assert not is_protective_stop({**real, "side": "sell"})


async def test_symbol_not_on_account_refused_before_any_order():
    t, fake = trader()
    r = await t.open_short("OTHER/USDT:USDT", stop_price=1.05, setup="pump_fade", thesis="t", notional=100)
    assert not r["ok"] and "not listed" in r["error"] and fake.orders == []
