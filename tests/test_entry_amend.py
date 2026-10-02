"""#12: amending a resting entry lower re-runs the risk check (a lower limit widens the stop distance at the same
size) and every amend is a logged decision; one trade per symbol: no market short beside a resting entry, no limit
placed beside an unresolved market entry."""

import json

from shortbot.config import RiskLimits
from shortbot.entries import EntryDesk
from shortbot.journal import Journal
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, ok_prechecks
from tests.test_killswitch import favorable, prop


def desk(limits=None):
    fake, budget = FakeCcxt(), {"risk_usd": 100.0, "blocked": False, "reasons": []}

    async def live_budget(trader, symbol, regime, conviction="B", setup=None):
        return {**budget, "multipliers": {}}
    t = Trader(FakeEx(fake, limits), Journal(":memory:"), prechecks=ok_prechecks, budget=live_budget)
    return EntryDesk(t, regime_fn=favorable), t, fake, budget


async def one(d, p):
    r = await d.propose_group([p])
    assert r["ok"], r
    return r


def decisions(t, action):
    return [json.loads(x["snapshot"] or "{}") for x in t.j.decisions(limit=100) if x["action"] == action]


async def test_lowering_past_the_current_budget_is_refused_and_logged():
    d, t, fake, budget = desk()
    eid = (await one(d, prop(1.06)))["placed"][0]["entry_id"]
    budget["risk_usd"] = 5.0                       # heat elsewhere since placement
    r = await d.amend(eid, 1.01, "level moved down")
    assert not r["ok"] and "budget" in r["rejected_by_budget"][0]
    assert fake.limits[next(iter(fake.limits))]["price"] == 1.06 and d.pending()[0]["replaced"] == 0
    assert decisions(t, "rejected")[0]["entry_id"] == eid


async def test_lowering_within_the_budget_is_applied_and_logged_with_its_risk():
    d, t, fake, budget = desk()
    e = (await one(d, prop(1.06)))["placed"][0]
    r = await d.amend(e["entry_id"], 1.03, "level moved down")
    assert r["ok"] and fake.limits[next(iter(fake.limits))]["price"] == 1.03
    (snap,) = decisions(t, "amend_entry")
    assert snap["old_limit"] == 1.06 and snap["new_limit"] == 1.03
    assert snap["risk_usdt"] > snap["old_risk_usdt"] and snap["budget_risk_usd"] == 100.0


async def test_lowering_refused_by_the_hard_caps_and_a_blocked_budget():
    d, t, fake, budget = desk(RiskLimits(max_stop_distance_pct=8.0))
    eid = (await one(d, prop(1.04)))["placed"][0]["entry_id"]   # 5.8% from the 1.10 stop
    r = await d.amend(eid, 1.01, "lower")                         # 8.9%: outside the stop-distance range
    assert not r["ok"] and "stop distance" in r["rejected_by_risk"][0]
    budget.update(blocked=True, reasons=["governor: daily loss cap hit"])
    r = await d.amend(eid, 1.03, "lower")
    assert not r["ok"] and r["rejected_by_budget"] == ["governor: daily loss cap hit"]
    assert fake.limits[next(iter(fake.limits))]["price"] == 1.04


async def test_raising_needs_no_budget_and_is_logged():
    d, t, fake, budget = desk()
    eid = (await one(d, prop(1.04)))["placed"][0]["entry_id"]
    budget.update(risk_usd=0.0, blocked=True, reasons=["halt"])
    assert (await d.amend(eid, 1.06, "closer to the stop: less risk"))["ok"]
    assert decisions(t, "amend_entry")[0]["new_limit"] == 1.06


async def test_a_split_legs_limit_may_be_raised_but_never_lowered():
    d, t, fake, budget = desk()
    r = await one(d, prop(None, tranches=[{"limit_price": 1.03, "share": 0.5}, {"limit_price": 1.06, "share": 0.5}]))
    near, far = sorted(r["placed"], key=lambda x: x["limit_price"])
    x = await d.amend(far["entry_id"], 1.05, "lower")
    assert not x["ok"] and "split entry" in x["error"]
    assert (await d.amend(near["entry_id"], 1.04, "raise"))["ok"]


# ------------------------------------------------------------ one trade per symbol
async def test_no_market_short_beside_our_own_resting_entry():
    d, t, fake, budget = desk()
    r = await one(d, prop(1.04))
    o = await t.open_short(SYM, stop_price=1.12, setup="pump_fade", thesis="market", notional=50)
    assert not o["ok"] and r["group_id"] in o["error"] and o["resting_entries"]
    assert not [x for x in fake.orders if x[0] == "market"] and t.j.active_trade(SYM) is None
    assert (await d.cancel_group(r["group_id"], "enter at market instead"))["ok"]
    assert (await t.open_short(SYM, stop_price=1.12, setup="pump_fade", thesis="market", notional=50))["ok"]


async def test_no_market_short_beside_an_external_resting_order():
    from shortbot import entry_intents
    d, t, fake, budget = desk()
    await fake.create_order(SYM, "limit", "sell", 50, 1.2, {"timeInForce": "GTC"})   # placed outside shortbot
    await entry_intents.reconcile(d)
    d._swept_at = 0
    await entry_intents.reconcile(d)
    assert d._rows("status='external'")
    o = await t.open_short(SYM, stop_price=1.12, setup="pump_fade", thesis="market", notional=50)
    assert not o["ok"] and "external" in o["error"]


async def test_no_limit_entry_beside_an_unresolved_market_entry():
    d, t, fake, budget = desk()
    t.j.create_trade(SYM, "pump_fade", "market send unknown", 1.12, 2)   # an 'opening' row: it may still fill
    r = await d.propose_group([prop(1.04)])
    assert not r["ok"] and "unresolved" in r["rejected"][0]["error"] and fake.limits == {}
