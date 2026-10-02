"""#66: a resting entry is withdrawn when the size it was placed at no longer fits: the governor halved size since
(m_gov below the stamp it was sized under), today's loss-cap room minus open risk no longer covers its risk, or its
setup's drawdown ladder (P2) went to HALF (stamp above) or SHADOW. A NULL stamp (an older server placed it) skips the
multiplier comparison but not the room. Only a verdict newer than the row judges it."""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from shortbot import entry_gates
from shortbot import killswitch as ks
from shortbot.entries import EntryDesk
from tests.fakes import FakeCcxt, neutral_budget
from tests.test_guardian import guardian_with, kinds
from tests.test_killswitch import favorable, prop

HALVED = "drawdown 8.3% >= 8%: half size"


def guardian():
    fake = FakeCcxt()
    g = guardian_with(fake)
    g.j.set(ks.HEARTBEAT_KEY, str(time.time()))
    return g, fake


def sized(m_gov=None, m_ladder=None):
    async def budget(trader, symbol, regime, conviction="B", setup=None):
        b = await neutral_budget(trader, symbol, regime, conviction, setup)
        return {**b, "multipliers": {"m_gov": m_gov, "m_ladder": m_ladder}}
    return budget


async def rest(g, *props, **stamps):
    """Rest `props` as one group, stamped like `stamps` (None = an older server's NULL), placed a minute ago."""
    r = await EntryDesk(g.app.trader, regime_fn=favorable).propose_group(list(props))
    assert r["ok"], r
    ids = [p["entry_id"] for p in r["placed"]]
    ago = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    for eid in ids:
        g.j.db.execute(f"UPDATE entries SET created_at=?{''.join(f', {k}=?' for k in stamps)} WHERE id=?",
                       (ago, *stamps.values(), eid))
    g.j.db.commit()
    return ids


def verdict(g, m_gov=1.0, room=1000.0, open_risk=0.0, reasons=(), age=0.0, **extra):
    entry_gates.publish_governor(g.j, {"halt": False, "until": None, "reasons": list(reasons), "m_gov": m_gov,
                                       "daily": {"room_usd": room}, "open_risk_usd": open_risk, **extra},
                                 ".env baseline", now=time.time() - age)


def row(g, eid):
    return g.entries._rows("id=?", (eid,))[0]


def risk(e):
    return e["amount"] * (e["stop_price"] - e["limit_price"])


def ladder(monkeypatch, mult=None, **by_setup):
    def lad(j, setup):
        state = by_setup.get(setup, "NORMAL")
        return {"state": state, "mult": {"NORMAL": 1.0, "HALF": 0.5, "SHADOW": 0.0}.get(state, mult),
                "reasons": [f"setup ladder: {setup} drawdown 9R: {state}"] if state != "NORMAL" else []}
    monkeypatch.setattr("shortbot.risk_budget._ladder", lad)


# ------------------------------------------------------------ the stamp
async def test_an_entry_is_stamped_with_the_multipliers_it_was_sized_under_and_an_amend_restamps_it():
    g, fake = guardian()
    g.app.trader.budget = sized(m_gov=0.5, m_ladder=1.0)
    d = EntryDesk(g.app.trader, regime_fn=favorable)
    eid = (await d.propose_group([prop(1.06)]))["placed"][0]["entry_id"]
    assert (row(g, eid)["m_gov"], row(g, eid)["m_ladder"]) == (0.5, 1.0)
    g.app.trader.budget = sized(m_gov=0.25, m_ladder=0.5)
    assert (await d.amend(eid, 1.05, "level moved down"))["ok"]       # lowering re-checks the current budget
    assert (row(g, eid)["m_gov"], row(g, eid)["m_ladder"]) == (0.25, 0.5)


# ------------------------------------------------------------ governor
async def test_a_governor_halving_withdraws_entries_sized_above_it():
    g, fake = guardian()
    (big,) = await rest(g, prop(1.04), m_gov=1.0)
    verdict(g, m_gov=0.5, reasons=[HALVED])
    await g.check_entries()
    assert row(g, big)["status"] == "cancelled"
    assert row(g, big)["reason"] == f"governor resized: size x0.5 now, placed at x1 ({HALVED})"
    assert "entries_withdrawn" in kinds(g)
    msg = [a["message"] for a in g.j.alerts() if a["kind"] == "entries_withdrawn"][0]
    assert "Re-propose at the current budget" in msg


async def test_an_entry_already_sized_at_the_halving_or_unstamped_rests_on():
    g, fake = guardian()
    ids = await rest(g, prop(1.04), prop(1.06, stop=1.12))               # a budget without the multipliers
    assert [row(g, i)["m_gov"] for i in ids] == [None, None]
    g.j.db.execute("UPDATE entries SET m_gov=0.5 WHERE id=?", (ids[0],))    # the other: as a pre-#66 server left it
    g.j.db.commit()
    verdict(g, m_gov=0.5, reasons=[HALVED])
    await g.check_entries()
    assert [row(g, i)["status"] for i in ids] == ["pending", "pending"] and "entries_withdrawn" not in kinds(g)


async def test_an_unstamped_entry_is_still_withdrawn_when_its_risk_exceeds_the_room():
    g, fake = guardian()
    (eid,) = await rest(g, prop(1.04))                                     # NULL stamps
    r = risk(row(g, eid))
    verdict(g, m_gov=0.5, room=r + 2.0, open_risk=2.01, reasons=[HALVED])  # room left: r - 0.01
    await g.check_entries()
    assert row(g, eid)["status"] == "cancelled"
    assert row(g, eid)["reason"].startswith("governor resized: risk over the daily-cap room left (room ")


async def test_shrinking_room_withdraws_only_the_entries_it_no_longer_covers():
    g, fake = guardian()
    small, big = await rest(g, prop(1.04), prop(1.06, stop=1.20), m_gov=1.0)
    assert risk(row(g, small)) < 10 < risk(row(g, big))
    verdict(g, room=15.0, open_risk=5.0)
    await g.check_entries()
    assert row(g, small)["status"] == "pending" and row(g, big)["status"] == "cancelled"


async def test_a_split_entry_is_judged_and_withdrawn_as_one_unit():
    g, fake = guardian()
    legs = await rest(g, prop(1.04, tranches=[{"limit_price": 1.04}, {"limit_price": 1.07}]), m_gov=1.0)
    rs = [risk(row(g, i)) for i in legs]
    verdict(g, room=max(rs) + 0.01)                                         # each leg fits, both don't
    await g.check_entries()
    assert [row(g, i)["status"] for i in legs] == ["cancelled", "cancelled"]


@pytest.mark.parametrize("case", ["stale", "newer_row", "no_fields"])
async def test_a_stale_or_older_verdict_or_one_without_the_fields_withdraws_nothing(case):
    g, fake = guardian()
    (eid,) = await rest(g, prop(1.04), m_gov=1.0)
    if case == "stale":
        verdict(g, m_gov=0.5, room=0.0, age=entry_gates.MAX_AGE_S + 1)
    elif case == "newer_row":                                               # sized after this verdict was taken
        verdict(g, m_gov=0.5, room=0.0, age=120)
    else:
        entry_gates.publish_governor(g.j, {"halt": False, "reasons": []}, ".env baseline")
    await g.check_entries()
    assert row(g, eid)["status"] == "pending" and "entries_withdrawn" not in kinds(g)


async def test_a_halt_still_withdraws_everything_with_its_own_reason():
    g, fake = guardian()
    ids = await rest(g, prop(1.04), prop(1.06, stop=1.12), m_gov=0.5)
    verdict(g, m_gov=0.5, room=0.0, halt=True, reasons=["daily loss cap hit"])
    await g.check_entries()
    assert {row(g, i)["reason"] for i in ids} == {"governor halt: daily loss cap hit"}


async def test_check_governor_publishes_the_multiplier_and_the_room(monkeypatch):
    g, fake = guardian()

    async def regime(ex):
        return {"verdict_for_shorts": "caution"}

    async def gov(tr, reg, limits=None):
        return {"halt": False, "until": None, "reasons": [HALVED], "m_gov": 0.5, "drawdown_pct": 8.3,
                "daily": {"room_usd": 42.0}, "open_risk_usd": 7.5}
    monkeypatch.setattr("shortbot.guardian_jobs.market_regime", regime)
    monkeypatch.setattr("shortbot.guardian_jobs.governor_status", gov)
    await g.check_governor()
    v = json.loads(g.j.get(g.j.mode_key(entry_gates.GOV_KEY)))
    assert (v["m_gov"], v["room_usd"], v["open_risk_usd"]) == (0.5, 42.0, 7.5)


async def test_governor_status_reports_the_open_risk_the_budget_subtracts(monkeypatch):
    from shortbot import risk_budget
    inp = risk_budget.BudgetInputs(1000.0, 1000.0, open_risks={"A/USDT:USDT": 2.0, "B/USDT:USDT": 3.5},
                                   gov={"halt": False, "m_gov": 1.0, "reasons": [], "daily": {"room_usd": 30.0}})

    async def gather(trader, regime, **kw):
        return inp
    monkeypatch.setattr(risk_budget, "gather_inputs", gather)
    tr = type("T", (), {"j": type("J", (), {"closed_results": lambda self: []})()})()
    assert (await risk_budget.governor_status(tr, "favorable"))["open_risk_usd"] == 5.5


# ------------------------------------------------------------ setup ladder (P2)
async def test_a_shadowed_setup_withdraws_all_its_resting_entries(monkeypatch):
    g, fake = guardian()
    ids = await rest(g, prop(1.04), prop(1.06, stop=1.12))                 # NULL stamps: SHADOW takes them too
    ladder(monkeypatch, pump_fade="SHADOW")
    await g.check_entries()
    assert [row(g, i)["status"] for i in ids] == ["cancelled", "cancelled"]
    assert row(g, ids[0])["reason"].startswith("setup ladder: SHADOW (setup ladder: pump_fade drawdown 9R: SHADOW)")


async def test_a_halved_setup_withdraws_entries_stamped_above_half_only(monkeypatch):
    g, fake = guardian()
    full, half, unstamped = await rest(g, prop(1.04), prop(1.05, stop=1.11), prop(1.06, stop=1.12))
    for eid, m in ((full, 1.0), (half, 0.5)):
        g.j.db.execute("UPDATE entries SET m_ladder=? WHERE id=?", (m, eid))
    g.j.db.commit()
    ladder(monkeypatch, pump_fade="HALF")
    await g.check_entries()
    assert [row(g, i)["status"] for i in (full, half, unstamped)] == ["cancelled", "pending", "pending"]
    assert row(g, full)["reason"] == "setup ladder: HALF (setup ladder: pump_fade drawdown 9R: HALF): re-propose"


@pytest.mark.parametrize("state, mult", [("UNKNOWN", 0.0), ("UNKNOWN", 0.5), ("NORMAL", 1.0)])
async def test_an_unknown_or_normal_ladder_withdraws_nothing(monkeypatch, state, mult):
    # an unreadable ladder fails closed for NEW entries (mult 0 or 0.5); a transient failure must not churn the book
    g, fake = guardian()
    (eid,) = await rest(g, prop(1.04), m_ladder=1.0)
    ladder(monkeypatch, mult, pump_fade=state)
    await g.check_entries()
    assert row(g, eid)["status"] == "pending" and "entries_withdrawn" not in kinds(g)
