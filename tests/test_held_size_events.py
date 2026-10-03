"""Size changes are journalled before their symbol lock is released, also while another process holds the journal's
write lock (#82). max_adds (positioning.adds_done) and close_intent.recent_change read size-changing decisions under
the symbol lock, and the next holder may be another process: a decision only posted (#79) waited in the writer's
queue there, so a third add could pass max_adds, or a session's partial close miss the guardian's target close. Each
writer awaits its unit inside the lock now (tests/conftest.py checks every writer of close_intent.SIZE_EVENTS, in
every test); here, another process reads it the moment the flow returns."""

from shortbot import close_intent
from shortbot.config import RiskLimits
from shortbot.journal import Journal
from shortbot.positioning import PositionOps, adds_done
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks
from tests.held_lock import STALL_REPORT, HeldLock

ROOMY = RiskLimits(max_position_notional=1000, max_total_notional=4000, max_adds=1)


async def short(path, fake: FakeCcxt, plan: dict | None = None) -> Trader:
    """A short of 100 @ 1.00, stop 1.05, journalled by this process (a session or the guardian)."""
    t = Trader(FakeEx(fake, ROOMY), Journal(path), prechecks=ok_prechecks, budget=neutral_budget)
    r = await t.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100, plan=plan)
    assert r["ok"], r
    return t


def other(path) -> Journal:
    """Another process's view of the journal: its own connection, nothing queued of ours."""
    return Journal(path)


@STALL_REPORT
async def test_an_add_made_during_a_hold_counts_toward_max_adds_in_another_process(tmp_path):
    fake = FakeCcxt()
    t = await short(tmp_path / "j.db", fake)
    fake.price = 0.9
    with HeldLock(tmp_path / "j.db", 1.0):
        assert (await PositionOps(t).add_to_winner(SYM, 9, 0.95, "one"))["ok"]
        o = other(tmp_path / "j.db")
        assert adds_done(o, o.active_trade(SYM)) == 1            # before #82: 0 while the add waited in the queue
    s = Trader(t.ex, o, prechecks=ok_prechecks, budget=neutral_budget)   # a session in that process
    r = await PositionOps(s).add_to_winner(SYM, 9, 0.94, "two")
    assert not r["ok"] and "1 adds already" in r["error"] and fake.position == 110


@STALL_REPORT
async def test_a_target_close_during_a_hold_refuses_another_process_s_blind_partial(tmp_path):
    fake = FakeCcxt()
    t = await short(tmp_path / "j.db", fake)
    fake.price = 0.95
    with HeldLock(tmp_path / "j.db", 1.0):
        r = await t.close(SYM, "plan/target 1", fraction=0.5, of_original=True)   # the guardian takes target 1
        assert r["ok"] and fake.position == 50
        o = other(tmp_path / "j.db")
        ev = close_intent.recent_change(o, SYM, o.active_trade(SYM))
        assert ev and ev["action"] == "partial_close"            # before #82: None while it waited in the queue
    s = Trader(t.ex, o, prechecks=ok_prechecks, budget=neutral_budget)
    r = await s.close(SYM, "halve it", fraction=0.5)             # a session that looked before the target
    assert not r["ok"] and "target already taken by the guardian" in r["error"] and fake.position == 50
