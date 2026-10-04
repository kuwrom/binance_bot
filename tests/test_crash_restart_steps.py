"""#86, the crash/restart suite's multi-step points (the rig and the first five: test_crash_restart.py): a process
killed outright between two steps of one intent, then a new guardian's start-up jobs. Besides that suite's
invariants (one closePosition stop per position, nothing sent twice, the heartbeat only on a verified pass), the dead
intent resolves `interrupted -> partial` with the steps it took, never `done`.

Points: (6) a plan step killed after its T1 close filled, before its breakeven move."""

import pytest

from shortbot import audit, intents
from tests.fake_faults import Crash
from tests.test_crash_restart import STOP, Desk, alerts, opened, startup, verified
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

