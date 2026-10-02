"""A size under one step is each order path's own "too small" refusal, never ccxt's InvalidOrder, and nothing is sent
(#71). ccxt 4.5.84 base/exchange.py amount_to_precision raises InvalidOrder when the truncated amount is '0'; the fake
does too (tests/fakes.py, #65)."""

from shortbot import steps
from shortbot.entries import EntryDesk
from shortbot.journal import Journal
from shortbot.positioning import PositionOps
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks


def trader(fake):
    return Trader(FakeEx(fake), Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)


async def favorable():
    return "favorable"


def coarse(fake, step=1000.0):   # 100 USDT at price 1 is 100 contracts: under one step of 1000
    fake.sym[SYM]["step"] = step


def test_floor_amount_and_too_small():
    fake = FakeCcxt()
    assert steps.floor_amount(fake, SYM, 1.99) == 1.9 and steps.floor_amount(fake, SYM, 0.05) == 0.0
    assert steps.floor_amount(fake, SYM, 0) == 0.0 and steps.floor_amount(fake, SYM, -1) == 0.0
    assert "one step" in steps.too_small(fake, SYM, 0.0, 1.0)
    assert "below exchange minimum 5" in steps.too_small(fake, SYM, 4.0, 1.0)   # MIN_NOTIONAL 5 in the fake
    assert steps.too_small(fake, SYM, 5.0, 1.0) is None


async def test_open_short_under_one_step_is_refused_and_sends_nothing():
    fake = FakeCcxt()
    coarse(fake)
    t = trader(fake)
    r = await t.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100)
    assert r["ok"] is False and "one step" in r["error"], r
    assert fake.orders == [] and fake.position == 0 and t.j.active_trade(SYM) is None


async def test_limit_entry_under_one_step_is_refused_and_sends_nothing():
    fake = FakeCcxt()
    coarse(fake)
    d = EntryDesk(trader(fake), regime_fn=favorable)
    r = await d.propose_group([{"symbol": SYM, "limit_price": 1.04, "stop_price": 1.10, "setup": "pump_fade",
                                "thesis": "sweep", "level_type": "prior_high", "level_reason": "1.05 high"}])
    assert r["ok"] is False and len(r["rejected"]) == 1, r
    assert r["rejected"][0]["error"].startswith("ValueError") and "one step" in r["rejected"][0]["error"]
    assert fake.orders == [] and fake.limits == {} and fake.prearms == {} and d.pending(all_orders=True) == []


async def test_add_under_one_step_is_refused_and_sends_nothing():
    fake = FakeCcxt()
    t = trader(fake)
    assert (await t.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100))["ok"]
    fake.price = 0.9
    coarse(fake)
    n, stops = len(fake.orders), dict(fake.stops)
    r = await PositionOps(t).add_to_winner(SYM, 9, 0.95, "trend continuing")
    assert r["ok"] is False and any("below one lot" in x for x in r["rejected_by_risk"]), r
    assert len(fake.orders) == n and fake.stops == stops and fake.position == 100   # not even the pre-add tighten
