"""Task #74: the caps count everything that can become exposure, not just the live positions. Another group's resting
limit entry and an entry of unknown outcome (it may still fill, at its full intended size) take a slot and their
notional in open_short's, a limit entry's and an add's checks; a voluntary widen must fit today's daily-loss room. And
the lock audit covers the account-config writes and every risk-adding send."""

from dataclasses import replace

import pytest

from shortbot.config import RiskLimits
from shortbot.entries import EntryDesk
from shortbot.journal import Journal
from shortbot.positioning import PositionOps
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks
from tests.test_entries import favorable, prop

SYM2 = "MOVR/USDT:USDT"
ONE_SLOT = {"positions": RiskLimits(max_positions=1), "notional": RiskLimits(max_total_notional=150)}


def desk(limits: RiskLimits | None = None, **kw):
    fake = FakeCcxt(**kw)
    fake.add_symbol(SYM2, 1.0)
    t = Trader(FakeEx(fake, limits), Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
    return EntryDesk(t, regime_fn=favorable), t, fake


async def open_sym(t, symbol=SYM, notional=100):
    return await t.open_short(symbol, stop_price=1.05, setup="pump_fade", thesis="t", notional=notional)


def refused_by_cap(r, cap):
    text = "; ".join(r.get("rejected_by_risk") or [r.get("error") or ""])
    return not r["ok"] and ("max 1 positions" if cap == "positions" else "total notional would be") in text


# ------------------------------------------------------------ resting limit entries (finding 2)
@pytest.mark.parametrize("cap", ONE_SLOT)
async def test_open_short_counts_another_groups_resting_entry_like_a_placement_does(cap):
    d, t, fake = desk(ONE_SLOT[cap])
    assert (await d.propose_group([prop(1.04, symbol=SYM2)]))["ok"]
    r = await open_sym(t)
    assert refused_by_cap(r, cap), r
    assert fake.position == 0


@pytest.mark.parametrize("cap", ONE_SLOT)
async def test_a_placement_counts_a_resting_entry_the_same_way(cap):
    d, t, fake = desk(ONE_SLOT[cap])
    assert (await d.propose_group([prop(1.04, symbol=SYM2)]))["ok"]
    r = await d.propose_group([prop(1.04)])
    assert not r["ok"] and refused_by_cap({"ok": False, "error": r["rejected"][0]["error"]}, cap), r


async def test_a_resting_leg_on_an_open_symbol_takes_no_second_slot():
    d, t, fake = desk(RiskLimits(max_positions=2))
    assert (await open_sym(t, SYM2))["ok"]
    t.j.db.execute("INSERT INTO entries (group_id, symbol, status, created_at, expires_ms, amount, limit_price, "
                   "stop_price, mode) VALUES ('g1', ?, 'pending', 'now', 0, 10, 1.04, 1.10, ?)",
                   (SYM2, t.j.mode))   # e.g. a split's far leg
    t.j.db.commit()
    assert (await open_sym(t))["ok"]   # SYM2 is one position, resting leg or not


async def test_an_add_counts_resting_entries_toward_total_notional():
    d, t, fake = desk()
    assert (await open_sym(t))["ok"]
    assert (await d.propose_group([prop(1.04, symbol=SYM2)]))["ok"]   # ~98 USDT that may fill
    t.ex.settings = replace(t.ex.settings, limits=replace(t.ex.settings.limits, max_total_notional=150))
    fake.price = 0.9   # 90 live + 9 added fits 150; with the resting ~98 it doesn't
    r = await PositionOps(t).add_to_winner(SYM, 9, 0.95, "trend continuing")
    assert not r["ok"] and "total notional would be" in r["error"], r


# ------------------------------------------------------------ entries of unknown outcome (finding 3)
@pytest.mark.parametrize("cap", ONE_SLOT)
async def test_a_market_entry_of_unknown_outcome_counts_at_its_intended_size(cap):
    d, t, fake = desk(ONE_SLOT[cap])
    fake.ack_lost_unsent = 1   # the answer is lost: the row stays 'opening', it may still fill
    u = await open_sym(t, SYM2)
    row = t.j.get_trade(u["trade_id"])
    assert u["unknown"] and row["status"] == "opening" and fake.sym[SYM2]["qty"] == 0
    assert row["entry_features"]["intended_notional"] == 100   # journalled before the send
    assert refused_by_cap(await open_sym(t), cap)
    r = await d.propose_group([prop(1.04)])
    assert refused_by_cap({"ok": False, "error": r["rejected"][0]["error"]}, cap), r
    assert fake.position == 0


@pytest.mark.parametrize("cap", ONE_SLOT)
async def test_a_limit_entry_of_unknown_outcome_counts_for_the_next_open(cap):
    d, t, fake = desk(ONE_SLOT[cap])
    fake.ack_lost = 1   # it rests, but the answer was lost: the row is 'unknown'
    r = await d.propose_group([prop(1.04, symbol=SYM2)])
    assert r["unknown"] and not r["placed"]
    assert refused_by_cap(await open_sym(t), cap)


async def test_an_unknown_entry_that_shows_as_a_position_is_counted_once():
    d, t, fake = desk(RiskLimits(max_total_notional=250))
    fake.ack_lost_unsent = 1
    u = await open_sym(t, SYM2)
    await fake.create_order(SYM2, "market", "sell", 100, None, {"clientOrderId": u["client_id"]})   # it fills late
    assert (await open_sym(t))["ok"]   # 100 live on SYM2 (not 100 more for its 'opening' row) + 100 <= 250


# ------------------------------------------------------------ a voluntary widen (finding 1)
async def widen_after_a_tighten(limits):
    d, t, fake = desk(limits)
    assert (await open_sym(t))["ok"]
    assert (await t.move_stop(SYM, 1.01, "tighten"))["ok"]   # 1 USDT at the stop now; 1.10 for 50 = 5 USDT
    return await PositionOps(t).widen_stop(SYM, 1.10, "stop inside noise"), t, fake


async def test_a_widen_that_does_not_fit_the_daily_loss_room_is_refused_with_the_numbers():
    r, t, fake = await widen_after_a_tighten(RiskLimits(daily_loss_limit_pct=0.02))   # cap ~1-2 USDT today
    assert not r["ok"] and "daily-loss room" in r["error"] and "adds 3.99 USDT" in r["error"], r
    assert list(fake.stops.values()) == [1.01] and fake.position == 100
    assert "widen_rejected" in [x["action"] for x in t.j.decisions(SYM)]


async def test_a_widen_that_fits_the_daily_loss_room_goes_through():
    r, t, fake = await widen_after_a_tighten(RiskLimits())   # 3% of 10k: room to spare
    assert r["ok"] and list(fake.stops.values()) == [1.10] and fake.position == pytest.approx(49.9)


# ------------------------------------------------------------ the lock audit (account config, risk-adding sends)
class NoLock:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return None


async def test_the_audit_flags_risk_and_account_config_sent_outside_the_account_lock(order_lock_audit, monkeypatch):
    monkeypatch.setattr(Trader, "account_lock", lambda self, *a, **k: NoLock())
    d, t, fake = desk(hedged=True)   # hedge mode: prepare switches the account to one-way first
    assert (await open_sym(t))["ok"]
    assert (await d.propose_group([prop(1.04, symbol=SYM2)]))["ok"]
    got = sorted({b.split(" via ")[0] for b in order_lock_audit})
    assert got == ["create_order(MOVR/USDT:USDT)", "create_order(PUMP/USDT:USDT)", "set_position_mode()"], got
    assert all("outside the account lock" in b for b in order_lock_audit)
    order_lock_audit.clear()


async def test_the_audit_flags_leverage_and_margin_mode_sent_outside_the_symbol_lock(order_lock_audit, monkeypatch):
    monkeypatch.setattr(Trader, "symbol_lock", lambda self, *a, **k: NoLock())
    d, t, fake = desk()
    assert (await open_sym(t))["ok"]
    flagged = {b.split("(")[0] for b in order_lock_audit if "outside its symbol lock" in b}
    assert {"set_margin_mode", "set_leverage"} <= flagged, order_lock_audit
    order_lock_audit.clear()
