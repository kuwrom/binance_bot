"""Kill switch: the persisted state, the gate on every opening path, and the human-only clear (shortbot-admin)."""

import io
import json
import re
import time
from types import SimpleNamespace

import pytest

from shortbot import admin
from shortbot import killswitch as ks
from shortbot.entries import EntryDesk
from shortbot.journal import Journal
from shortbot.killswitch import ACTIVE, HALTED, REDUCING
from shortbot.positioning import PositionOps
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks

SETTINGS = FakeEx(FakeCcxt()).settings


def trader(**kw):
    fake = FakeCcxt(**kw)
    t = Trader(FakeEx(fake), Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
    t.j.set(ks.HEARTBEAT_KEY, str(time.time()))   # a live guardian
    return t, fake


async def open_default(t, **kw):
    return await t.open_short(SYM, **{"stop_price": 1.05, "setup": "pump_fade", "thesis": "lower high",
                                      "notional": 100, **kw})


async def favorable():
    return "favorable"


def prop(limit, stop=1.10, **kw):
    return {"symbol": SYM, "limit_price": limit, "stop_price": stop, "setup": "pump_fade", "thesis": "sweep",
            "level_type": "prior_high", "level_reason": "1.05 high", "leverage": 3, **kw}


# ------------------------------------------------------------ state
async def test_default_active_and_a_trip_is_persisted_logged_and_alerted():
    j = Journal(":memory:")
    assert ks.read(j)["state"] == ACTIVE
    r = await ks.trip(j, SETTINGS, REDUCING, "guardian", "daily loss -3.2%")
    st = ks.read(j)
    assert r["changed"] and st["state"] == REDUCING and st["by"] == "guardian" and st["at"]
    assert j.decisions()[0]["action"] == "kill_switch" and j.alerts()[0]["kind"] == "kill_switch"


async def test_trips_only_escalate_and_repeats_are_silent():
    j = Journal(":memory:")
    await ks.trip(j, SETTINGS, HALTED, "agent (MCP)", "squeeze everywhere")
    r = await ks.trip(j, SETTINGS, REDUCING, "guardian", "untracked position")
    assert not r["changed"] and ks.read(j)["state"] == HALTED and ks.read(j)["reason"] == "squeeze everywhere"
    assert len(j.decisions()) == 1 and len(j.alerts()) == 1
    with pytest.raises(ValueError, match="human-only"):
        await ks.trip(j, SETTINGS, ACTIVE, "agent", "all good now")
    with pytest.raises(ValueError, match="reason"):
        await ks.trip(j, SETTINGS, HALTED, "agent", " ")


async def test_a_stale_writer_in_another_process_can_never_lower_it(tmp_path):
    a, b = Journal(tmp_path / "desk.db"), Journal(tmp_path / "desk.db")   # MCP server and guardian
    assert ks.read(b)["state"] == ACTIVE
    await ks.trip(a, SETTINGS, HALTED, "agent", "flatten")
    await ks.trip(b, SETTINGS, REDUCING, "guardian", "daily loss")       # decided on its stale ACTIVE view
    assert ks.read(a)["state"] == ks.read(b)["state"] == HALTED


async def test_an_unreadable_row_fails_closed_and_the_next_trip_repairs_it():
    j = Journal(":memory:")
    j.set(ks.KV_KEY, "garbage")
    assert ks.read(j)["state"] == REDUCING
    assert (await ks.trip(j, SETTINGS, REDUCING, "guardian", "x"))["changed"]
    assert ks.read(j)["reason"] == "x"


async def test_clear_loosens_and_keeps_what_it_cleared():
    j = Journal(":memory:")
    await ks.trip(j, SETTINGS, HALTED, "guardian", "drawdown 12.4%")
    shown = j.get(ks.KV_KEY)
    await ks.trip(j, SETTINGS, HALTED, "agent", "a second cause")   # no-op: the row is unchanged
    await ks.clear(j, SETTINGS, ACTIVE, "human:kibrom (tty)", "reviewed the week", shown)
    assert ks.read(j)["state"] == ACTIVE and ks.read(j)["prev"] == HALTED
    d = j.decisions()[0]
    assert d["action"] == "kill_switch_clear" and "drawdown 12.4%" in d["snapshot"]


# ------------------------------------------------------------ gates
async def test_reducing_refuses_open_short_before_any_order_or_journal_row():
    t, fake = trader()
    await ks.trip(t.j, SETTINGS, REDUCING, "guardian", "daily loss")
    r = await open_default(t)
    assert not r["ok"] and r["kill_switch"] == REDUCING and "shortbot-admin clear" in r["error"]
    assert fake.orders == [] and t.j.trades() == []


async def test_a_trip_during_the_slow_prechecks_still_stops_the_order():
    t, fake = trader()

    async def checks_then_trip(ex, s, tk):   # another process trips while we fetch tickers / vetoes
        await ks.trip(t.j, SETTINGS, HALTED, "agent (MCP)", "news shock")
        return await ok_prechecks(ex, s, tk)
    t.prechecks = checks_then_trip
    r = await open_default(t)
    assert not r["ok"] and r["kill_switch"] == HALTED and fake.orders == [] and t.j.trades() == []


async def test_exits_and_stop_tightening_still_work_when_halted():
    t, fake = trader()
    assert (await open_default(t))["ok"]
    await ks.trip(t.j, SETTINGS, HALTED, "agent", "stop everything")
    assert (await t.move_stop(SYM, 1.03, "tighten"))["ok"] and list(fake.stops.values()) == [1.03]
    assert (await t.close(SYM, "cut half", fraction=0.5))["ok"] and fake.position == 50
    assert (await t.close(SYM, "flat", "halted desk"))["ok"] and fake.position == 0


async def test_proposals_refused_and_a_trip_mid_split_rolls_the_placed_leg_back():
    t, fake = trader()
    d = EntryDesk(t, regime_fn=favorable)
    await ks.trip(t.j, SETTINGS, REDUCING, "guardian", "untracked position")
    r = await d.propose_group([prop(1.04)])
    assert not r["ok"] and r["kill_switch"] == REDUCING and fake.limits == {}

    t2, fake2 = trader()
    d2 = EntryDesk(t2, regime_fn=favorable)
    calls = 0

    async def budget_then_trip(*a, **k):   # the trip lands between the two legs
        nonlocal calls
        calls += 1
        if calls == 2:
            await ks.trip(t2.j, SETTINGS, HALTED, "agent", "halt")
        return await neutral_budget(*a, **k)
    t2.budget = budget_then_trip
    r = await d2.propose_group([prop(1.04, tranches=[{"limit_price": 1.04, "share": 0.5},
                                                     {"limit_price": 1.07, "share": 0.5}])])
    assert not r["ok"] and "kill switch HALTED" in r["rejected"][0]["error"]
    assert len(fake2.limits) == 1 and all(o["status"] == "canceled" for o in fake2.limits.values())
    assert fake2.prearms == {} and d2.pending() == []


async def test_amend_when_reducing_may_only_raise_the_limit():
    t, fake = trader()
    d = EntryDesk(t, regime_fn=favorable)
    eid = (await d.propose_group([prop(1.04)]))["placed"][0]["entry_id"]
    await ks.trip(t.j, SETTINGS, REDUCING, "guardian", "daily loss")
    r = await d.amend(eid, 1.03, "chase")
    assert not r["ok"] and r["kill_switch"] == REDUCING
    assert (await d.amend(eid, 1.06, "less risk: closer to the stop"))["ok"]


async def test_adds_and_widening_refused_without_touching_the_stop():
    t, fake = trader()
    assert (await open_default(t))["ok"]
    fake.price = 0.9
    await ks.trip(t.j, SETTINGS, REDUCING, "guardian", "daily loss")
    ops, n = PositionOps(t), len(fake.orders)
    assert (await ops.add_to_winner(SYM, 50, 0.95, "trend"))["kill_switch"] == REDUCING
    assert (await ops.widen_stop(SYM, 1.10, "noise"))["kill_switch"] == REDUCING
    assert list(fake.stops.values()) == [1.05] and fake.position == 100 and len(fake.orders) == n


async def test_a_trip_after_the_pre_add_tighten_stops_the_add_and_keeps_the_tighter_stop():
    t, fake = trader()
    assert (await open_default(t))["ok"]
    fake.price = 0.9
    move = t.move_stop

    async def move_then_trip(*a, **k):
        r = await move(*a, **k)
        await ks.trip(t.j, SETTINGS, HALTED, "agent", "halt")
        return r
    t.move_stop = move_then_trip
    r = await PositionOps(t).add_to_winner(SYM, 50, 0.95, "trend")
    assert r["kill_switch"] == HALTED and fake.position == 100 and list(fake.stops.values()) == [0.95]


async def test_ladder_opt_in_refused_and_its_add_becomes_a_skip(monkeypatch):
    from shortbot import ladder_apply as la
    t, fake = trader()
    assert (await open_default(t))["ok"]
    await ks.trip(t.j, SETTINGS, REDUCING, "guardian", "daily loss")
    assert (await la.enable(t, SYM))["kill_switch"] == REDUCING

    async def fake_ladder(ex, j, pos, log=True):
        return {"variants": {"base": {"action": "add", "x_virtual": 1.5, "score": 2},
                             "adaptive": {"relevel": None, "stop_v": 1.05}},
                "score": 2, "parts": {}, "x_actual": 1.0, "x_star": 1.5, "levels": []}
    monkeypatch.setattr(la, "build_ladder", fake_ladder)
    r = await la.apply(t, SYM, dry_run=True)
    assert [a["kind"] for a in r["actions"]] == ["skip_add"] and "REDUCING" in r["actions"][0]["why"]


# ------------------------------------------------------------ shortbot-admin (human-only clear)
class Tty(io.StringIO):
    """A terminal whose human types `answer`, or reads the code off the screen and types it."""
    def __init__(self, answer=None):
        super().__init__()
        self.answer = answer

    def readline(self, *a):
        return (self.answer or re.search(r"type (\w{4}) to confirm", self.getvalue()).group(1)) + "\n"


def clear_args(to=ACTIVE):
    return SimpleNamespace(cmd="clear", to=to, reason="reviewed the trip")


async def test_admin_clear_needs_a_human_at_a_real_terminal(monkeypatch):
    j = Journal(":memory:")
    await ks.trip(j, SETTINGS, HALTED, "guardian", "drawdown")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert "Claude Code" in (await admin.run(clear_args(), SETTINGS, j))["error"]
    monkeypatch.delenv("CLAUDECODE")

    def no_tty():
        raise OSError(6, "Device not configured")
    monkeypatch.setattr(admin, "_tty", no_tty)
    assert "interactive terminal" in (await admin.run(clear_args(), SETTINGS, j))["error"]
    monkeypatch.setattr(admin, "_tty", lambda: Tty("ABCD"))
    assert "did not match" in (await admin.run(clear_args(), SETTINGS, j))["error"]
    assert ks.read(j)["state"] == HALTED
    monkeypatch.setattr(admin, "_tty", lambda: Tty())
    r = await admin.run(clear_args(REDUCING), SETTINGS, j)
    assert r["ok"] and ks.read(j)["state"] == REDUCING and ks.read(j)["by"].startswith("human:")
    assert "not a loosening" in (await admin.run(clear_args(REDUCING), SETTINGS, j))["error"]


async def test_admin_clear_fails_if_a_trip_lands_while_the_human_types(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    j = Journal(":memory:")
    await ks.trip(j, SETTINGS, REDUCING, "guardian", "daily loss -3.1%", cause="daily_loss")

    class Busy(Tty):   # another process escalates between the prompt and the answer
        def readline(self, *a):
            j.set(ks.KV_KEY, json.dumps({"state": HALTED, "by": "agent (MCP)", "reason": "squeeze", "at": "t",
                                         "cause": "manual"}))
            return super().readline()
    monkeypatch.setattr(admin, "_tty", lambda: Busy())
    r = await admin.run(clear_args(), SETTINGS, j)
    assert not r["ok"] and "re-run" in r["error"] and ks.read(j)["reason"] == "squeeze"
    monkeypatch.setattr(admin, "_tty", lambda: Tty())   # the re-run shows (and clears) the new state
    assert (await admin.run(clear_args(), SETTINGS, j))["ok"] and ks.read(j)["state"] == ACTIVE
