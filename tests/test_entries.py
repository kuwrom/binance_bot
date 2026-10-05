"""Limit-entry groups against the strict fake exchange."""

import time

from shortbot.entries import EntryDesk
from shortbot.journal import Journal
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks


async def favorable():
    return "favorable"


def desk(**kw):
    fake = FakeCcxt(**kw)
    t = Trader(FakeEx(fake), Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
    return EntryDesk(t, regime_fn=favorable), t, fake


def prop(limit, stop=1.10, **kw):
    p = {"symbol": SYM, "limit_price": limit, "stop_price": stop, "setup": "pump_fade", "thesis": "sweep of 1.05 high",
         "level_type": "prior_high", "level_reason": "1.05 swing high + round number", "leverage": 3}
    p.update(kw)
    return p


async def test_limit_must_be_above_market_and_stop_above_limit():
    d, t, fake = desk()
    r = await d.propose_group([prop(0.99), prop(1.04, stop=1.03)])
    assert not r["ok"] and len(r["rejected"]) == 2 and fake.limits == {}


async def test_group_places_gtd_limits_with_prearmed_stops_and_sizes_from_budget():
    d, t, fake = desk()
    r = await d.propose_group([prop(1.04), prop(1.06, stop=1.12)], ttl_hours=6)
    assert r["ok"] and len(r["placed"]) == 2 and len(fake.limits) == 2 and len(fake.prearms) == 2
    gtd = [o for o in fake.orders if o[0] == "limit"][0][3]
    assert gtd["timeInForce"] == "GTD" and gtd["goodTillDate"] > time.time() * 1000
    # neutral budget = 1% of 10k = 100 USDT risk; capped by max_position_notional (100 USDT)
    assert all(p["risk_usdt"] <= 100 for p in r["placed"])


async def test_first_fill_protects_journals_and_cancels_siblings():
    d, t, fake = desk()
    r = await d.propose_group([prop(1.04), prop(1.06, stop=1.12)])
    first, second = r["placed"]
    oid = next(i for i, o in fake.limits.items() if o["price"] == 1.04)
    fake.fill_limit(oid)
    ev = await d.poll()
    filled = [e for e in ev if e["event"] == "filled"][0]
    assert list(fake.stops.values()) == [1.10]                       # closePosition stop placed on fill
    assert fake.prearms == {}                                        # pre-armed stops cleaned up
    assert all(o["status"] != "open" for o in fake.limits.values())   # sibling cancelled
    tr = t.j.get_trade(filled["trade_id"])
    assert tr["status"] == "open" and tr["entry_features"]["entry_type"] == "limit"
    assert d.pending() == []
    sib = d._rows("id=?", (second["entry_id"],))[0]                 # not overwritten as 'expired' later in the pass
    assert sib["status"] == "cancelled" and "filled" in sib["reason"]


async def test_partial_fill_keeps_its_remainder_resting_and_protects():   # owner decision 2026-10-05
    d, t, fake = desk()
    await d.propose_group([prop(1.04)])
    oid = next(iter(fake.limits))
    fake.fill_limit(oid, qty=fake.limits[oid]["amount"] / 2)
    await d.poll()
    assert fake.limits[oid]["status"] == "open" and fake.stops and fake.position > 0   # tests/test_entry_partial.py
    assert t.j.open_trades()[0]["amount"] == fake.position


async def test_cancel_when_move_happens_without_us_and_on_fast_approach():
    d, t, fake = desk()
    await d.propose_group([prop(1.04, cancel_below=0.95)])
    fake.price = 0.94
    ev = await d.poll()
    assert ev[0]["event"] == "cancelled" and "without us" in ev[0]["why"]
    d2, t2, fake2 = desk()
    await d2.propose_group([prop(1.08, stop=1.15)])
    fake2.price = 1.045   # +4.5% within 15 min while ATR(1h) = 4% of price -> cascade, not a tap
    ev = await d2.poll()
    assert ev[0]["event"] == "cancelled" and "cascade" in ev[0]["why"]


async def test_amend_only_once():
    d, t, fake = desk()
    r = await d.propose_group([prop(1.04)])
    eid = r["placed"][0]["entry_id"]
    assert (await d.amend(eid, 1.05, "level moved"))["ok"]
    assert not (await d.amend(eid, 1.06, "again"))["ok"]


async def test_size_mult_only_shrinks_the_order():
    d, t, fake = desk()
    from shortbot.config import RiskLimits

    def roomy():                    # no notional cap in the way, so risk (not the cap) sets the size
        fk = FakeCcxt()
        lim = RiskLimits(max_position_notional=100_000, max_total_notional=400_000, max_leverage=10)
        tr = Trader(FakeEx(fk, lim), Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
        return EntryDesk(tr, regime_fn=favorable)
    full = (await roomy().propose_group([prop(1.04)]))["placed"][0]
    half = (await roomy().propose_group([prop(1.04, size_mult=0.5)]))["placed"][0]
    big = (await roomy().propose_group([prop(1.04, size_mult=5)]))["placed"][0]
    assert abs(half["risk_usdt"] - full["risk_usdt"] / 2) < 1 and big["amount"] == full["amount"]


async def test_split_entry_two_legs_share_one_budget_and_join_one_trade():
    d, t, fake = desk()
    fake.price = 1.035                                               # a slow approach (the fast-approach rule stays)
    other = prop(1.07, stop=1.12)                                   # an ordinary OCO sibling in the same group
    r = await d.propose_group([prop(1.04, tranches=[{"limit_price": 1.04, "share": 0.5},
                                                    {"limit_price": 1.07, "share": 0.5}]), other])
    assert r["ok"] and len(r["placed"]) == 3
    legs = [x for x in r["placed"] if "full_size" in x]
    assert len(legs) == 2 and abs(sum(x["risk_usdt"] for x in legs) - legs[0]["r1_usdt"]) < 1e-6
    assert sum(x["risk_usdt"] for x in legs) <= 100 + 1e-6            # both legs together = one budget
    oid1 = next(i for i, o in fake.limits.items() if o["price"] == 1.04)
    fake.fill_limit(oid1)
    ev = await d.poll()
    tid = [e for e in ev if e["event"] == "filled"][0]["trade_id"]
    tr = t.j.get_trade(tid)
    assert tr["plan"]["ladder_live"] == "v1" and tr["plan"]["full_size"] == legs[0]["full_size"]
    pend = d.pending()
    assert [p["leg"] for p in pend] == [2]                           # the OCO sibling died, leg 2 lives
    oid2 = next(i for i, o in fake.limits.items() if o["price"] == 1.07)
    fake.fill_limit(oid2)
    ev = await d.poll()
    j = [e for e in ev if e["event"] == "tranche_joined"][0]
    assert j["trade_id"] == tid and 1.04 < j["avg"] < 1.07
    tr = t.j.get_trade(tid)
    assert abs(tr["amount"] - fake.position) < 1e-9 and list(fake.stops.values()) == [1.10]
    assert len(t.j._rows("SELECT id FROM trades WHERE status='open'")) == 1


async def test_split_entry_second_leg_dies_when_the_trade_closes():
    d, t, fake = desk()
    fake.price = 1.035
    await d.propose_group([prop(1.04, tranches=[{"limit_price": 1.04}, {"limit_price": 1.07}])])
    oid1 = next(i for i, o in fake.limits.items() if o["price"] == 1.04)
    fake.fill_limit(oid1)
    await d.poll()
    fake.price = 1.0
    await t.close(SYM, "target", "lesson")                         # close() cancels the resting leg itself
    assert d.pending() == [] and all(o["status"] != "open" for o in fake.limits.values())
    assert d._rows("leg=2")[0]["reason"] == "trade closed"


def test_split_entry_rejects_bad_shares():
    import pytest

    from shortbot.tranches import expand
    with pytest.raises(ValueError):
        expand({"tranches": [{"limit_price": 1, "share": 0.7}, {"limit_price": 2, "share": 0.5}]})
    with pytest.raises(ValueError):
        expand({"tranches": [{"limit_price": 1}]})


async def test_fill_always_gets_a_whole_position_stop_even_when_a_sibling_prearm_exists():
    # same-symbol group: the sibling's pre-armed reduce-only stop must NOT count as protection (it is cancelled
    # with the sibling a moment later) - the fill needs its own closePosition stop
    d, t, fake = desk()
    fake.price = 1.035
    await d.propose_group([prop(1.04), prop(1.06, stop=1.12)])
    assert len(fake.prearms) == 2 and fake.stops == {}
    oid = next(i for i, o in fake.limits.items() if o["price"] == 1.04)
    fake.fill_limit(oid)
    await d.poll()
    assert list(fake.stops.values()) == [1.10] and fake.prearms == {}


async def test_fill_tightens_a_looser_fallback_stop_to_the_plan_stop():
    d, t, fake = desk()
    fake.price = 1.035
    await d.propose_group([prop(1.04)])
    oid = next(iter(fake.limits))
    fake.fill_limit(oid)
    fake.stops["99"] = 1.144          # guardian fallback (+10%) landed before the fill was processed
    await d.poll()
    assert list(fake.stops.values()) == [1.10]


async def test_split_entry_legs_count_together_as_exposure_for_other_groups():
    from shortbot.config import RiskLimits
    from shortbot.journal import Journal
    from shortbot.trading import Trader
    from tests.fakes import FakeEx
    fake = FakeCcxt()
    fake.price = 1.035
    lim = RiskLimits(max_total_notional=150)   # room for ONE ~95 USDT order, not for two split legs + another
    t = Trader(FakeEx(fake, lim), Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
    d = EntryDesk(t, regime_fn=favorable)
    r1 = await d.propose_group([prop(1.04, tranches=[{"limit_price": 1.04}, {"limit_price": 1.07}])])
    assert r1["ok"]
    assert sum(x["amount"] * x["limit_price"] for x in r1["placed"]) > 90       # ~49 + ~49 USDT legs
    r2 = await d.propose_group([prop(1.05)])   # both legs of the other group count, so this can't squeeze under the cap
    assert not r2["ok"] and "total notional" in str(r2["rejected"])


async def test_split_entry_is_all_or_nothing_when_a_later_leg_is_vetoed():
    calls = {"n": 0}

    async def veto_second(ex, symbol, ticker):     # the read changes between legs (0G 2026-10-01)
        calls["n"] += 1
        return {"vetoes": ["breakout accepted above prior_high"] if calls["n"] > 1 else [],
                "regime": "favorable", "features": {}}
    d, t, fake = desk()
    fake.price = 1.035
    t.prechecks = veto_second
    r = await d.propose_group([prop(1.04, tranches=[{"limit_price": 1.04}, {"limit_price": 1.07}])])
    assert not r["ok"] and "vetoed at placement" in r["rejected"][0]["error"]
    assert d.pending() == [] and all(o["status"] != "open" for o in fake.limits.values())
    assert fake.stops == {}                                          # leg 1's pre-armed stop is gone too
    assert [x["status"] for x in d._rows("1=1")] == ["cancelled"]


async def test_split_rollback_never_strips_the_stop_of_a_leg_that_already_filled():
    d, t, fake = desk()
    fake.price = 1.035
    calls = {"n": 0}

    async def fill_then_veto(ex, symbol, ticker):  # leg 1 fills before leg 2 is validated (MOVR 2026-10-01)
        calls["n"] += 1
        if calls["n"] == 2:
            fake.fill_limit(next(iter(fake.limits)))
            return {"vetoes": ["already short"], "regime": "favorable", "features": {}}
        return {"vetoes": [], "regime": "favorable", "features": {}}
    t.prechecks = fill_then_veto
    r = await d.propose_group([prop(1.04, tranches=[{"limit_price": 1.04}, {"limit_price": 1.07}])])
    assert not r["ok"] and fake.position > 0
    assert len(fake.prearms) == 1 and [x["status"] for x in d._rows("1=1")] == ["pending"]
    ev = await d.poll()                                              # the poll adopts and protects it
    assert [e["event"] for e in ev] == ["filled"] and list(fake.stops.values()) == [1.10]


async def test_split_places_the_far_leg_first_so_the_near_leg_cannot_fill_mid_placement():
    d, t, fake = desk()
    fake.price = 1.035
    await d.propose_group([prop(1.04, tranches=[{"limit_price": 1.04}, {"limit_price": 1.07}])])
    assert [o["price"] for o in fake.limits.values()] == [1.07, 1.04]


async def test_limit_entry_refuses_a_vetoed_read_unless_overridden():
    async def vetoed(ex, symbol, ticker):
        return {"vetoes": ["sweep closed back below, but time/volume stayed above: a painted rejection"],
                "regime": "favorable", "features": {}}
    d, t, fake = desk()
    t.prechecks = vetoed
    r = await d.propose_group([prop(1.04)])
    assert not r["ok"] and "vetoed at placement" in r["rejected"][0]["error"] and fake.limits == {}
    r = await d.propose_group([prop(1.04, veto_override="research: testing painted-close rejections")])
    assert r["ok"]
