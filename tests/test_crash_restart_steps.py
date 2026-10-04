"""#86, the crash/restart suite's multi-step points (the rig and the first five: test_crash_restart.py): a process
killed outright between two steps of one intent, then a new guardian's start-up jobs. Besides that suite's
invariants (one closePosition stop per position, nothing sent twice, the heartbeat only on a verified pass), the dead
intent resolves `interrupted -> partial` with the steps it took, never `done`.

Points: (6) a plan step killed after its T1 close filled, before its breakeven move; (7) a split entry's propose
killed after one leg, or both, rested with their pre-arms, before the legs were pinned as one unit."""

import pytest

from shortbot import audit, intents
from shortbot.entries import EntryDesk
from tests.fake_faults import Crash
from tests.test_crash_restart import STOP, Desk, alerts, dies, opened, startup, verified
from tests.test_entries import favorable, prop
from tests.test_entry_intents import age, open_limits
from tests.test_fault_matrix import bridges, cids, close_stops, resolutions

T1 = {"targets": [{"price": 0.95, "fraction": 0.5}], "auto": {"targets": True, "breakeven": True}}


@pytest.fixture(autouse=True)
def dead_pids(monkeypatch):
    """The killed process shares this test's pid: the sweep must judge it gone."""
    monkeypatch.setattr(intents, "_alive", lambda pid: False)


# ------------------------------------------------------------ (6) a plan step, its T1 filled, before its breakeven
async def test_killed_mid_plan_step_after_t1_before_breakeven(tmp_path):
    d = Desk(tmp_path)
    s = await opened(d)
    (tr,) = s.j.open_trades()
    s.j.update_trade(tr["id"], plan={**tr["plan"], **T1, "auto": {**tr["plan"]["auto"], **T1["auto"]}})
    old = d.guardian("old guardian")
    d.fake.price = 0.93                                                       # past T1: its market fallback
    old.ex.fake.crash_after("create_order", "close")
    with pytest.raises(Crash), audit.acting("guardian:positions"):
        await old.check_positions()
    assert d.fake.position == 50 and close_stops(d.fake) == [STOP]           # T1 filled; the stop never moved
    (step,) = old.j._rows("SELECT * FROM intents WHERE kind='plan_step'")
    g = d.guardian()
    await startup(g)
    assert d.fake.position == 50 and len(cids(d.fake, "close")) == 1       # T1 is never closed twice
    (be,) = close_stops(d.fake)                                               # moved to breakeven by the new pass
    assert tr["entry_price"] <= be < STOP and bridges(d.fake) == [] and d.fake.prearms == {}
    assert g.j.get_trade(tr["id"])["targets_hit"] == [0.95]                  # the plan knows T1 was taken
    assert intents.get(g.j, step["id"])["status"] == "partial" and resolutions(g.j) == ["interrupted -> partial"]
    (told,) = alerts(g, "intent_resolved")
    assert " close " in told and "filled (it stopped there)" in told
    verified(g, "PUMP/USDT:USDT")


# ------------------------------------------------------------ (7) a split entry, its legs resting, never pinned
@pytest.mark.parametrize("acked", [1, 2])
async def test_killed_mid_split_entry_placement_rolls_the_unpinned_legs_back(acked, tmp_path, monkeypatch):
    d = Desk(tmp_path, price=1.035)
    s = d.trader("session")
    split = prop(1.04, tranches=[{"limit_price": 1.04}, {"limit_price": 1.07}])   # far leg (1.07) first
    s.ex.fake.crash_after("create_order", "prearm", nth=acked)               # leg `acked`'s pre-arm rests, then dead
    iid = await dies(s, monkeypatch, lambda: EntryDesk(s, regime_fn=favorable).propose_group([split]))
    g = d.guardian()
    legs = g.entries._rows("tranche IS NOT NULL")
    assert [x["status"] for x in legs] == ["pending"] * acked and len(open_limits(d.fake)) == acked
    assert not any("ladder_live" in x["plan"] for x in legs)                  # never pinned: no full_size, no 1R
    for x in legs:
        age(g.entries, x["id"])
    await startup(g)
    # one unit: a dead call's unpinned legs go, with their pre-arms, as a dead 'placing' leg's split does
    assert open_limits(d.fake) == [] and d.fake.prearms == {} and d.fake.position == 0 and d.fake.stops == {}
    assert {x["status"] for x in g.entries._rows("tranche IS NOT NULL")} == {"cancelled"}
    assert all("its call died" in x["reason"] for x in g.entries._rows("tranche IS NOT NULL"))
    assert len(cids(d.fake, "entry")) == acked and intents.get(g.j, iid)["status"] == "partial"
    assert "interrupted -> partial" in resolutions(g.j)
    verified(g)


async def test_a_dead_calls_leg_that_filled_is_kept_and_protected_its_unfilled_sibling_goes(tmp_path, monkeypatch):
    d = Desk(tmp_path, price=1.035)
    s = d.trader("session")
    split = prop(1.04, tranches=[{"limit_price": 1.04}, {"limit_price": 1.07}])
    s.ex.fake.crash_after("create_order", "prearm", nth=2)
    await dies(s, monkeypatch, lambda: EntryDesk(s, regime_fn=favorable).propose_group([split]))
    g = d.guardian()
    near = g.entries._rows("tranche IS NOT NULL AND limit_price=1.04")[0]
    d.fake.fill_limit(near["order_id"])                                        # filled before the restart
    for x in g.entries._rows("tranche IS NOT NULL"):
        age(g.entries, x["id"])
    await startup(g)
    (tr,) = g.j.open_trades()
    assert tr["amount"] == near["amount"] and close_stops(d.fake) == [1.10] and open_limits(d.fake) == []
    assert d.fake.prearms == {} and g.entries._rows("id=?", (near["id"],))[0]["status"] == "filled"
    verified(g, "PUMP/USDT:USDT")
