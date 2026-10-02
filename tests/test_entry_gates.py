"""#12: resting limit entries are withdrawn while the account governor halts new entries or a market pause is on
(the guardian's 5 s pass), never on a stale verdict or map; the TTL is capped."""

import json
import math
import time

from pytest import approx

from shortbot import entry_gates
from shortbot import killswitch as ks
from shortbot.entries import EntryDesk
from shortbot.entries_place import MAX_TTL_H
from shortbot.market.market_map import KV_LAST
from shortbot.params import ResearchParams
from tests.fakes import FakeCcxt
from tests.test_guardian import guardian_with, kinds
from tests.test_killswitch import favorable, prop

PAUSE = "BTC +0.95% in 15 min: alts catch up, wait"


def guardian():
    fake = FakeCcxt()
    g = guardian_with(fake)
    g.j.set(ks.HEARTBEAT_KEY, str(time.time()))
    return g, fake


async def rest(g, *props):
    r = await EntryDesk(g.app.trader, regime_fn=favorable).propose_group(list(props) or [prop(1.04), prop(1.06)])
    assert r["ok"], r
    return [p["entry_id"] for p in r["placed"]]


def governor(g, halt=True, until=None, age=0.0):
    entry_gates.publish_governor(g.j, {"halt": halt, "until": until, "reasons": ["daily loss cap hit (-310 USDT)"]},
                                 ".env baseline", now=time.time() - age)


def market_map(g, pause, age=0.0):
    g.j.set(KV_LAST, json.dumps({"ts": time.time() - age, "header": {}, "coins": {},
                                 "vetoes": {"pause": pause, "sectors": {}}}))


def status(g, eid):
    return g.entries._rows("id=?", (eid,))[0]


async def test_a_governor_halt_withdraws_every_resting_entry_with_its_reason():
    g, fake = guardian()
    ids = await rest(g)
    governor(g)
    await g.check_entries()
    assert all(o["status"] == "canceled" for o in fake.limits.values()) and fake.prearms == {}
    for eid in ids:
        assert status(g, eid)["status"] == "cancelled"
        assert status(g, eid)["reason"] == "governor halt: daily loss cap hit (-310 USDT)"
    assert "entries_withdrawn" in kinds(g)


async def test_a_stale_lifted_or_expired_governor_verdict_withdraws_nothing():
    g, fake = guardian()
    await rest(g)
    governor(g, age=entry_gates.MAX_AGE_S + 1)                 # the monitor lane stopped publishing
    await g.check_entries()
    governor(g, halt=False)
    await g.check_entries()
    governor(g, until="2020-01-01T00:00+00:00")                # its halt ran out before the next check
    await g.check_entries()
    assert len(g.entries.pending()) == 2 and "entries_withdrawn" not in kinds(g)


async def test_a_market_pause_withdraws_resting_entries_but_a_stale_map_never_does():
    g, fake = guardian()
    ids = await rest(g)
    market_map(g, [PAUSE], age=entry_gates.MAX_AGE_S + 1)     # a stopped guardian's last map
    await g.check_entries()
    assert len(g.entries.pending()) == 2
    market_map(g, [])
    await g.check_entries()
    assert len(g.entries.pending()) == 2
    market_map(g, [PAUSE])
    await g.check_entries()
    assert g.entries.pending() == [] and status(g, ids[0])["reason"] == f"market pause: {PAUSE}"


async def test_an_entry_placed_over_the_pause_rests_until_the_governor_halts():
    g, fake = guardian()
    (before,) = await rest(g, prop(1.04))

    async def paused(ex, s, tk):
        return {"vetoes": [PAUSE], "regime": "favorable", "features": {}}
    market_map(g, [PAUSE])
    g.app.trader.prechecks = paused
    fake.add_symbol("ALT/USDT:USDT", 2.0, tick=0.001)
    (over,) = await rest(g, prop(2.08, stop=2.2, symbol="ALT/USDT:USDT",
                                 veto_override="research: does the catch-up reach this level"))
    await g.check_entries()
    assert status(g, before)["status"] == "cancelled" and status(g, over)["status"] == "pending"
    governor(g)                                                 # the governor has no override
    await g.check_entries()
    assert status(g, over)["status"] == "cancelled" and status(g, over)["reason"].startswith("governor halt: ")


async def test_check_governor_publishes_the_verdict_under_the_experiments_limits(monkeypatch):
    g, fake = guardian()
    ResearchParams(g.app.settings, g.j).set({"daily_loss_limit_pct": 5}, "wider day for fades", "dl5")
    seen = {}

    async def regime(ex):
        return {"verdict_for_shorts": "caution"}

    async def gov(tr, reg, limits=None):
        seen["limits"] = limits
        return {"halt": True, "until": None, "reasons": ["daily loss cap hit"], "drawdown_pct": 1.0}
    monkeypatch.setattr("shortbot.guardian_jobs.market_regime", regime)
    monkeypatch.setattr("shortbot.guardian_jobs.governor_status", gov)
    await g.check_governor()
    assert seen["limits"].daily_loss_limit_pct == 5          # the limits the session sizes and places under
    v = entry_gates.governor_halt(g.j)
    assert v == ["daily loss cap hit"]
    assert "dl5" in json.loads(g.j.get(g.j.mode_key(entry_gates.GOV_KEY)))["limits"]


async def test_the_ttl_is_capped():
    g, fake = guardian()
    d = EntryDesk(g.app.trader, regime_fn=favorable)
    for bad in (MAX_TTL_H + 0.01, 72, math.nan):
        r = await d.propose_group([prop(1.04)], ttl_hours=bad)
        assert not r["ok"] and "ttl_hours" in r["error"] and fake.limits == {}
    assert (await d.propose_group([prop(1.04)], ttl_hours=MAX_TTL_H))["ok"]


async def test_gather_inputs_judges_and_measures_under_the_limits_it_is_given():
    from shortbot.config import RiskLimits
    from shortbot.risk_budget import gather_inputs
    from tests.fakes import SYM
    g, fake = guardian()
    t, other = g.app.trader, "ETH/USDT:USDT"
    EntryDesk(t)
    t.j._insert("INSERT INTO entries (group_id, symbol, status, created_at, expires_ms, limit_price, amount)"
                " VALUES (?,?,?,?,?,?,?)", ("ext", other, "external", "2026-10-02T00:00:00+00:00", 0, 2.0, 10.0))
    pos = [{"symbol": SYM, "exchange_stop": None, "mark": 1.0, "contracts": 100.0}]   # no stop on the exchange
    base = await gather_inputs(t, "favorable", positions=pos)
    tight = await gather_inputs(t, "favorable", positions=pos,
                                limits=RiskLimits(max_stop_distance_pct=10.0, daily_loss_limit_pct=1.0))
    assert base.open_risks[SYM] == approx(25.0) and tight.open_risks[SYM] == approx(10.0)   # no stop: worst case
    assert base.open_risks[other] == approx(5.0) and tight.open_risks[other] == approx(2.0)  # an external order too
    assert tight.gov["daily"]["cap_usd"] == approx(base.gov["daily"]["cap_usd"] / 3, abs=0.01)   # the governor
