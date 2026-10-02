"""Stops and liquidation (#17): the guardian's restored stop sits inside the position's real liquidation price (minus
liq_buffer_pct), and every entry check reads the symbol's leverage bracket."""

import pytest

from shortbot.config import RiskLimits
from shortbot.entries import EntryDesk
from shortbot.journal import Journal
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks

TEN_X = RiskLimits(max_leverage=10)


def trader(limits=None, maint=None):
    fake, alerts = FakeCcxt(), []

    async def alert(kind, msg, symbol=None, urgent=False):
        alerts.append((kind, msg, urgent))
    ex = FakeEx(fake, limits)
    asked = []

    async def bracket(symbol, notional):
        asked.append((symbol, round(notional, 2)))
        return maint
    ex.maint_margin = bracket
    t = Trader(ex, Journal(":memory:"), alert=alert, prechecks=ok_prechecks, budget=neutral_budget)
    return t, fake, alerts, asked


async def short_at(fake, lev, qty=100):
    """A short shortbot didn't open: no journal row, so no plan stop."""
    await fake.set_leverage(lev, SYM)
    await fake.create_order(SYM, "market", "sell", qty)


async def test_a_10x_short_without_a_plan_stop_gets_its_fallback_inside_liquidation():
    t, fake, alerts, _ = trader()
    await short_at(fake, 10)
    liq = fake.liq_price(SYM)                         # 1.1 / 1.01: before the flat +10% (1.10)
    r = await t.ensure_stop(SYM, 10)
    assert r["action"] == "stop_placed" and list(fake.stops.values()) == [r["stop"]]
    assert r["stop"] == pytest.approx(liq * 0.98, abs=1e-4) and r["stop"] < liq < 1.10 and r["liquidation"] == liq


async def test_a_2x_short_keeps_the_flat_fallback():
    t, fake, alerts, _ = trader()
    await short_at(fake, 2)                           # liquidation ~1.485
    r = await t.ensure_stop(SYM, 10)
    assert r["action"] == "stop_placed" and list(fake.stops.values()) == [1.1]


async def test_price_already_within_the_buffer_closes_before_liquidation():
    t, fake, alerts, _ = trader()
    await short_at(fake, 10)
    fake.price = 1.075                                # past liq - 2%, short of liquidation
    r = await t.ensure_stop(SYM, 10)
    assert r["action"] == "closed" and fake.position == 0 and fake.stops == {}


async def test_a_plan_stop_past_liquidation_is_capped_with_an_urgent_alert():
    t, fake, alerts, _ = trader()
    r = await t.open_short(SYM, stop_price=1.20, setup="pump_fade", thesis="t", notional=100, leverage=2)
    assert r["ok"], r
    fake.sym[SYM]["lev"] = 10                         # margin taken out since: liquidation ~1.089, before 1.20
    fake.stops.clear()
    res = await t.ensure_stop(SYM, 10)
    liq = fake.liq_price(SYM)
    assert res["action"] == "stop_placed" and res["stop"] == pytest.approx(liq * 0.98, abs=1e-4)
    (kind, msg, urgent), = [a for a in alerts if a[0] == "plan_stop_unreachable"]
    assert urgent and "1.2" in msg
    assert t.j.active_trade(SYM)["stop_price"] == 1.20   # the plan is the human's to change


async def test_a_plan_stop_inside_liquidation_is_restored_as_is():
    t, fake, alerts, _ = trader()
    r = await t.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100)
    assert r["ok"], r
    assert (await t.positions())[0]["leverage"] == 2   # positionRisk v3 carries none: the journal's
    fake.stops.clear()
    assert (await t.ensure_stop(SYM, 10))["stop"] == 1.05
    assert "plan_stop_unreachable" not in [a[0] for a in alerts]


async def test_open_short_checks_liquidation_at_the_symbols_bracket():
    # 6% stop at 10x: past (liquidation - 2%) at the 2.5% fallback rate, inside it at the 1% bracket
    t, fake, _, _ = trader(TEN_X)
    r = await t.open_short(SYM, stop_price=1.06, setup="pump_fade", thesis="t", notional=100, leverage=10)
    assert not r["ok"] and "liquidat" in " ".join(r["rejected_by_risk"])
    t, fake, _, asked = trader(TEN_X, maint=(0.01, 0.0))
    r = await t.open_short(SYM, stop_price=1.06, setup="pump_fade", thesis="t", notional=100, leverage=10)
    assert r["ok"], r
    assert asked == [(SYM, 100)]


def prop(limit, stop, **kw):
    return {"symbol": SYM, "limit_price": limit, "stop_price": stop, "setup": "pump_fade", "thesis": "sweep",
            "level_type": "prior_high", "level_reason": "1.05 swing high", "leverage": 10, **kw}


async def favorable():
    return "favorable"


async def test_limit_entries_check_liquidation_at_the_symbols_bracket():
    t, fake, _, _ = trader(TEN_X)
    r = await EntryDesk(t, regime_fn=favorable).propose_group([prop(1.04, 1.1076)])   # 6.5% at 10x
    assert not r["ok"] and "liquidation" in str(r["rejected"])
    t, fake, _, asked = trader(TEN_X, maint=(0.01, 0.0))
    r = await EntryDesk(t, regime_fn=favorable).propose_group([prop(1.04, 1.1076)])
    assert r["ok"], r
    assert asked and asked[0][0] == SYM


async def test_lowering_an_entry_checks_liquidation_at_the_symbols_bracket():
    for maint, ok in ((None, False), ((0.01, 0.0), True)):
        t, fake, _, _ = trader(TEN_X, maint=(0.01, 0.0))
        d = EntryDesk(t, regime_fn=favorable)
        r = await d.propose_group([prop(1.06, 1.10)])                  # 3.8%: fine either way
        assert r["ok"], r

        async def bracket(symbol, notional, m=maint):
            return m
        t.ex.maint_margin = bracket
        r = await d.amend(r["placed"][0]["entry_id"], 1.035, "level moved down")   # 6.3% at 10x
        assert r["ok"] is ok, r
        if not ok:
            assert "liquidat" in " ".join(r["rejected_by_risk"])
