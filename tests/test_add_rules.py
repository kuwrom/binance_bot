"""add_to_winner's caps and pyramiding rules (#10, docs/RISK_GOVERNANCE.md): the notional, stop-distance and
liquidation checks open_short applies, plus min_add_r / max_add_frac / max_adds as research parameters."""

from dataclasses import replace

import pytest

from shortbot.config import RiskLimits
from shortbot.journal import Journal
from shortbot.params import ParamError, ResearchParams
from shortbot.positioning import PositionOps, adds_done
from shortbot.risk import AccountState, AddRequest, RiskViolation, check_add
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks

ROOMY = {"max_position_notional": 1000, "max_total_notional": 4000}   # isolates the pyramiding rules from the caps

# short 100 @ 1.00, stop 1.05 (1R = 5 USDT), now 0.90 (+2R); add 10 contracts with the stop at 0.95: every rule passes
BASE = AddRequest(SYM, add_qty=10, mark=0.9, stop_price=0.95, qty=100, entry=1.0, leverage=2, initial_size=100,
                  r1=5.0, adds_done=0)
ACCT = AccountState(equity=10_000, open_symbols=(SYM,), total_open_notional=90, day_pnl=0)


def reasons(req=BASE, acct=ACCT, lim=None):
    try:
        check_add(req, acct, lim or RiskLimits())
    except RiskViolation as e:
        return e.reasons
    return []


@pytest.mark.parametrize("change, acct, why", [
    ({"add_qty": 0}, ACCT, "positive"),
    ({"add_qty": 20}, ACCT, "per-coin max 100"),                                  # (100 + 20) x 0.9 = 108
    ({}, replace(ACCT, total_open_notional=395), "total notional would be 404"),
    ({"stop_price": 0.902}, ACCT, "min_stop_distance_pct"),                        # 0.22% above price
    ({"leverage": 5}, ACCT, "leverage 5 outside [1, 3]"),
    ({"leverage": 0}, ACCT, "leverage unknown"),
    ({"leverage": 3, "stop_price": 1.30}, ACCT, "liquidation"),                    # 31% above the new average
    ({"mark": 0.97, "add_qty": 3, "stop_price": 0.99}, ACCT, "min_add_r"),         # +0.6R
    ({"initial_size": 15}, ACCT, "max_add_frac"),
    ({"adds_done": 2}, ACCT, "max_adds"),
])
def test_each_add_rule_alone(change, acct, why):
    assert reasons() == []
    got = reasons(replace(BASE, **change), acct)
    assert len(got) == 1 and why in got[0], got


def test_every_broken_rule_is_listed():
    got = reasons(replace(BASE, add_qty=2000 / 0.9, adds_done=2))
    assert any("per-coin" in r for r in got) and any("total" in r for r in got)
    assert any("max_add_frac" in r for r in got) and any("max_adds" in r for r in got)


def test_the_pyramiding_rules_follow_the_limits():
    lim = RiskLimits(min_add_r=0.5, max_add_frac=1.0, max_adds=3)
    assert reasons(replace(BASE, mark=0.97, add_qty=3, stop_price=0.99), lim=lim) == []
    assert reasons(replace(BASE, initial_size=15), lim=lim) == []
    assert reasons(replace(BASE, adds_done=2), lim=lim) == []
    assert reasons(replace(BASE, add_qty=10), lim=RiskLimits(max_adds=0)) == [
        "0 adds already on this trade: max_adds is 0 (research param)"]


def test_new_params_are_tunable_within_bounds_only():
    p = ResearchParams(FakeEx(FakeCcxt()).settings, Journal(":memory:"))
    eff = p.effective()
    assert (eff.min_add_r, eff.max_add_frac, eff.max_adds) == (1.0, 0.5, 2)
    p.set({"min_add_r": 0, "max_add_frac": 1.0, "max_adds": 4}, "ladder adds from +0R", "ladder_pyr")
    assert (p.effective().min_add_r, p.effective().max_add_frac, p.effective().max_adds) == (0, 1.0, 4)
    with pytest.raises(ParamError, match="whole number"):
        p.set({"max_adds": 2.5}, "why", "x")
    for bad in ({"max_add_frac": 5}, {"max_adds": 50}, {"min_add_r": -1}):
        with pytest.raises(ParamError, match="outside research bounds"):
            p.set(bad, "why", "x")


# ------------------------------------------------------------------ through add_to_winner
class ParamEx(FakeEx):
    """FakeEx whose limits are the research params' effective ones, as Exchange.limits computes them."""
    limits = property(lambda self: self.params.effective())


async def desk(**limits):
    fake = FakeCcxt()
    ex = ParamEx(fake, RiskLimits(**limits))
    t = Trader(ex, Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
    ex.params = ResearchParams(ex.settings, t.j)
    assert (await t.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100))["ok"]
    return PositionOps(t), t, fake


def untouched(t, fake):
    return fake.position == 100 and list(fake.stops.values()) == [1.05] and \
        t.j.decisions(SYM)[0]["action"] == "add_rejected"


async def test_oversized_add_is_refused_at_baseline_and_allowed_under_an_experiment_tag():
    ops, t, fake = await desk()
    fake.price = 0.9
    r = await ops.add_to_winner(SYM, 2000, 0.95, "press it")             # 2,000 USDT onto a 100 USDT short
    assert not r["ok"] and any("per-coin max 100" in x for x in r["rejected_by_risk"])
    assert "set_risk_params" in r["hint"]
    assert any("total notional" in x for x in r["rejected_by_risk"]) and untouched(t, fake)
    r = await ops.add_to_winner(SYM, 90, 0.95, "double")                  # 100 more: 2x the position, 180 USDT
    assert not r["ok"] and "per-coin max" in r["error"] and "max_add_frac" in r["error"] and untouched(t, fake)
    t.ex.params.set({"max_position_notional": 300, "max_add_frac": 1.0}, "double into strength", "pyr_double")
    r = await ops.add_to_winner(SYM, 90, 0.95, "double")
    assert r["ok"] and fake.position == 200 and list(fake.stops.values()) == [0.95]


async def test_add_below_min_add_r_is_refused_until_the_param_is_lowered():
    ops, t, fake = await desk(**ROOMY)
    fake.price = 0.97                                                    # +3 USDT = +0.6R
    r = await ops.add_to_winner(SYM, 20, 0.99, "early add")
    assert not r["ok"] and "0.60R" in r["error"] and "min_add_r" in r["error"] and untouched(t, fake)
    t.ex.params.set({"min_add_r": 0.5}, "add from +0.5R", "pyr_early")
    assert (await ops.add_to_winner(SYM, 20, 0.99, "early add"))["ok"]


async def test_max_add_frac_is_of_the_initial_size_not_the_grown_position():
    ops, t, fake = await desk(**ROOMY)
    fake.price = 0.9
    r = await ops.add_to_winner(SYM, 54, 0.95, "60 contracts")
    assert not r["ok"] and "max_add_frac" in r["error"] and untouched(t, fake)
    assert (await ops.add_to_winner(SYM, 44, 0.95, "48.8 contracts"))["ok"] and fake.position == 148.8
    r = await ops.add_to_winner(SYM, 54, 0.95, "60: under half the 148.8 now, not of the initial 100")
    assert not r["ok"] and "initial size 100" in r["error"] and fake.position == 148.8


async def test_max_adds_counts_this_trades_adds_from_the_journal():
    ops, t, fake = await desk(**ROOMY)
    fake.price = 0.9
    assert (await ops.add_to_winner(SYM, 9, 0.95, "one"))["ok"]
    assert (await ops.add_to_winner(SYM, 9, 0.95, "two"))["ok"]
    tid = t.j.active_trade(SYM)["id"]
    assert adds_done(t.j, t.j.active_trade(SYM)) == 2
    assert t.j.decisions(SYM)[0]["snapshot"].count(f'"trade_id": {tid}') == 1
    r = await ops.add_to_winner(SYM, 9, 0.95, "three")
    assert not r["ok"] and "2 adds already" in r["error"] and fake.position == 120
    t.ex.params.set({"max_adds": 3}, "a third add on trend days", "pyr_three")
    assert (await ops.add_to_winner(SYM, 9, 0.95, "three"))["ok"] and fake.position == 130


def test_adds_done_scopes_by_trade_and_counts_unknown_and_legacy_rows():
    j = Journal(":memory:")
    old = j.create_trade(SYM, "pump_fade", "t", 1.05, 2, {}, {}, None)
    j.db.execute("UPDATE trades SET status='closed', opened_at='2020-01-01T00:00:00+00:00' WHERE id=?", (old,))
    j.db.execute("INSERT INTO decisions (ts, symbol, action, reasoning, snapshot) VALUES "
                 "('2020-01-02T00:00:00+00:00', ?, 'add', 'an earlier trade', NULL)", (SYM,))
    tid = j.create_trade(SYM, "pump_fade", "t", 1.05, 2, {}, {}, None)
    j.log_decision("add", "legacy row: no trade id", SYM, snapshot={"add_qty": 5})
    j.log_decision("add", "UNKNOWN: may have filled", SYM, snapshot={"trade_id": tid, "unknown": True})
    j.log_decision("add", "another trade (other mode)", SYM, snapshot={"trade_id": tid + 1})
    j.log_decision("add_rejected", "refused", SYM, snapshot={"trade_id": tid})
    j.log_decision("add", "another symbol", "ETH/USDT:USDT", snapshot={"trade_id": tid})
    assert adds_done(j, j.get_trade(tid)) == 2


async def test_a_stop_inside_the_min_distance_is_refused_without_moving_the_stop():
    ops, t, fake = await desk(**ROOMY)
    fake.price = 0.9
    r = await ops.add_to_winner(SYM, 9, 0.902, "hug the price")
    assert not r["ok"] and "min_stop_distance_pct" in r["error"] and untouched(t, fake)
    assert (await t.move_stop(SYM, 0.902, "tight trail"))["ok"]
    r = await ops.add_to_winner(SYM, 9, 0.95, "a looser new_stop leaves the 0.902 stop resting")   # judged on 0.902
    assert not r["ok"] and "stop 0.902 is" in r["error"] and list(fake.stops.values()) == [0.902]


def test_the_liquidation_check_reads_the_symbols_bracket():
    # 3x, stop 29% above the new average 0.9909: past (liquidation - 2%) at the 2.5% fallback rate (liquidation ~30.1%),
    # inside it at a 1% bracket (~32.0%)
    req = replace(BASE, leverage=3, stop_price=round(1090 / 1100 * 1.29, 4))
    got = reasons(req)
    assert len(got) == 1 and "liquidation" in got[0], got
    assert reasons(replace(req, maint=(0.01, 0.0))) == []
