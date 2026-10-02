"""#11: the veto override and the risk-budget override are two powers, each a non-empty research reason, each
recorded on the trade with what it bypassed and each alerted."""

from shortbot.entries import EntryDesk
from shortbot.journal import Journal
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks


async def favorable():
    return "favorable"


async def vetoed(ex, s, tk):
    return {"vetoes": ["negative funding"], "regime": "hostile", "features": {}}


def tight(risk_usd, blocked=False):
    async def budget(trader, symbol, regime, conviction="B", setup=None):
        return {"risk_usd": risk_usd, "blocked": blocked, "reasons": ["daily-cap room 2.00 USDT"] if blocked else [],
                "multipliers": {"m_gov": 0.5}}
    return budget


def trader(prechecks=ok_prechecks, budget=neutral_budget):
    fake, sent = FakeCcxt(), []

    async def alert(kind, msg, symbol=None, urgent=False):
        sent.append((kind, msg))
    return Trader(FakeEx(fake), Journal(":memory:"), alert, prechecks, budget), fake, sent


async def open_(t, **kw):
    return await t.open_short(SYM, **{"stop_price": 1.05, "setup": "pump_fade", "thesis": "lower high",
                                      "notional": 100, **kw})


async def test_a_veto_override_no_longer_lifts_the_risk_budget():
    t, fake, _ = trader(budget=tight(2.0))   # 100 USDT at a 5% stop risks 5 USDT > 2
    r = await open_(t, veto_override="research: does the regime filter cost winners")
    assert not r["ok"] and r["rejected_by_budget"] and fake.orders == []


async def test_a_budget_override_lifts_the_budget_records_what_it_bypassed_and_alerts():
    t, fake, sent = trader(budget=tight(2.0))
    r = await open_(t, budget_override="research: full size on an A+ sweep")
    assert r["ok"], r
    f = t.j.get_trade(r["trade_id"])["entry_features"]
    assert f["budget_override"] == "research: full size on an A+ sweep"
    b = f["budget_overridden"]
    assert b["risk_at_stop_usdt"] == 5.0 and b["budget_risk_usd"] == 1.0   # 2.0, halved: liquidity tier unknown
    assert b["multipliers"] == {"m_gov": 0.5, "liquidity_funding": 0.5}
    assert f["veto_override"] is None and f["vetoes_overridden"] is None
    assert [k for k, _ in sent if k.endswith("_override")] == ["budget_override"]


async def test_a_budget_override_never_lifts_a_blocked_budget():
    t, fake, _ = trader(budget=tight(0.0, blocked=True))
    r = await open_(t, budget_override="research", veto_override="research")
    assert not r["ok"] and r["rejected_by_budget"] == ["daily-cap room 2.00 USDT"] and fake.orders == []


async def test_a_budget_override_does_not_lift_vetoes():
    t, fake, _ = trader(prechecks=vetoed)
    r = await open_(t, budget_override="research: size")
    assert not r["ok"] and "negative funding" in r["vetoed"] and fake.orders == []


async def test_blank_overrides_are_no_overrides():
    t, fake, _ = trader(prechecks=vetoed)
    assert (await open_(t, veto_override="   "))["vetoed"]
    t, fake, _ = trader(budget=tight(2.0))
    r = await open_(t, budget_override=" \n ")   # used to pass the budget check: plain truthiness
    assert not r["ok"] and r["rejected_by_budget"] and fake.orders == []


async def test_a_veto_override_records_the_vetoes_and_alerts_once():
    t, fake, sent = trader(prechecks=vetoed)
    r = await open_(t, veto_override="  research: regime filter  ")
    assert r["ok"], r
    f = t.j.get_trade(r["trade_id"])["entry_features"]
    assert f["veto_override"] == "research: regime filter"
    assert f["vetoes_overridden"] == ["negative funding", "market regime is hostile"]
    assert f["budget_override"] is None and f["budget_overridden"] is None
    alerts = [m for k, m in sent if k == "veto_override"]
    assert len(alerts) == 1 and "negative funding" in alerts[0] and "research: regime filter" in alerts[0]


async def test_an_unused_override_is_not_recorded_as_one():
    t, fake, sent = trader()
    r = await open_(t, veto_override="just in case", budget_override="just in case")
    assert r["ok"]
    f = t.j.get_trade(r["trade_id"])["entry_features"]
    assert f["veto_override"] is None and f["budget_override"] is None
    assert not [k for k, _ in sent if k.endswith("_override")]


# ------------------------------------------------------------ limit entries
def desk(prechecks=vetoed):
    t, fake, sent = trader(prechecks=prechecks)
    return EntryDesk(t, regime_fn=favorable), t, fake, sent


def prop(limit=1.04, **kw):
    return {"symbol": SYM, "limit_price": limit, "stop_price": 1.10, "setup": "pump_fade", "thesis": "sweep",
            "level_type": "prior_high", "level_reason": "1.05 high", "leverage": 3, **kw}


async def test_a_limit_entry_veto_override_is_alerted_and_carried_onto_the_trade():
    d, t, fake, sent = desk()
    assert not (await d.propose_group([prop(veto_override="  ")]))["ok"]   # blank: still vetoed
    r = await d.propose_group([prop(veto_override="research: painted rejection")])
    assert r["ok"], r
    assert [k for k, _ in sent if k == "veto_override"] == ["veto_override"]
    oid = next(iter(fake.limits))
    fake.fill_limit(oid)
    ev = await d.poll()
    f = t.j.get_trade(ev[0]["trade_id"])["entry_features"]
    assert f["veto_override"] == "research: painted rejection" and f["vetoes_overridden"] == ["negative funding"]


async def test_a_limit_entry_has_no_budget_override():
    d, t, fake, sent = desk(prechecks=ok_prechecks)
    r = await d.propose_group([prop(budget_override="bigger")])
    assert not r["ok"] and "open_short" in r["rejected"][0]["error"] and fake.limits == {}
