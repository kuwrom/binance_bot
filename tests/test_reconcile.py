"""Reconcile matches journal trades to the exchange by their own trade history (#15), not by symbol: a stop-out and a
re-entry before the 60 s pass close the old trade at its own exit, each position's income is counted once, an empty
positions answer closes nothing, and a reconcile failure never costs the guardian its stop checks. The desk's clock
follows the fake exchange clock here (`fake.advance` moves time between positions)."""

from datetime import UTC, datetime

import ccxt
import pytest

from shortbot import episodes, reconcile
from shortbot.journal import Journal
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks
from tests.test_entries_cancel import fail_cancel, oid_at
from tests.test_entry_races import desk, prop
from tests.test_guardian import guardian_with, kinds, open_trade

SYM2 = "MOVR/USDT:USDT"


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds")


def setup(monkeypatch):
    fake = FakeCcxt()
    fake.add_symbol(SYM2, 2.0, tick=0.001)
    monkeypatch.setattr("shortbot.journal.now_iso", lambda: iso(fake.now_ms()))
    return Trader(FakeEx(fake), Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget), fake


async def short(t, stop=1.05):
    r = await t.open_short(SYM, stop_price=stop, setup="pump_fade", thesis="t", notional=100)
    assert r["ok"], r
    return r["trade_id"]


def net(fake, until=None):
    return sum(float(i["income"]) for i in fake.income if until is None or i["time"] <= until)


def alert_kinds(t):
    return [a["kind"] for a in t.j.alerts(100)]


async def test_a_stop_out_and_reentry_before_the_pass_close_the_old_trade_at_its_own_exit(monkeypatch):
    t, fake = setup(monkeypatch)
    old = await short(t)
    fake.advance(60)
    fake.price = 1.05                                            # stopped out
    stop_fill = fake.fills[-1]
    fake.advance(60)
    new = await short(t, stop=1.10)                              # shorted again before the 60 s pass
    await reconcile.reconcile(t)
    o = t.j.get_trade(old)
    assert o["status"] == "closed" and o["exit_price"] == pytest.approx(1.05)
    assert o["closed_at"] == iso(stop_fill["timestamp"])         # its exit fill, not the pass
    assert o["pnl"] == pytest.approx(net(fake, stop_fill["timestamp"]))
    assert t.j.get_trade(new)["status"] == "open"
    fake.advance(60)
    fake.price = 1.10
    await reconcile.reconcile(t)
    n = t.j.get_trade(new)
    assert n["status"] == "closed" and n["exit_price"] == pytest.approx(1.10)
    assert o["pnl"] + n["pnl"] == pytest.approx(net(fake))       # every fill counted once


async def test_a_late_pass_bounds_each_trade_at_its_own_position(monkeypatch):
    t, fake = setup(monkeypatch)
    old = await short(t)
    fake.advance(60)
    fake.price = 1.05
    fake.advance(60)
    new = await short(t, stop=1.10)
    fake.advance(60)
    fake.price = 1.10                                            # the re-entry stopped out too
    fake.advance(600)                                            # and the pass comes late
    assert len(await reconcile.reconcile(t)) == 2
    first, second = (f for f in fake.fills if f["side"] == "buy")
    o, n = t.j.get_trade(old), t.j.get_trade(new)
    assert (o["closed_at"], n["closed_at"]) == (iso(first["timestamp"]), iso(second["timestamp"]))
    assert o["pnl"] == pytest.approx(net(fake, first["timestamp"]))
    assert o["pnl"] + n["pnl"] == pytest.approx(net(fake))


async def test_an_empty_positions_answer_closes_and_aborts_nothing_and_alerts(monkeypatch):
    t, fake = setup(monkeypatch)
    tid = await short(t)
    await fake.create_order(SYM2, "market", "sell", 50)          # an entry whose call died before the journal heard
    stuck = t.j.create_trade(SYM2, "pump_fade", "t", 2.2, 2)
    t.j.update_trade(stuck, opened_at=iso(fake.now_ms() - 600_000))
    real = fake.fetch_positions

    async def blank():
        return []
    fake.fetch_positions = blank
    for _ in range(2):
        assert await reconcile.reconcile(t) == []
    assert t.j.get_trade(tid)["status"] == "open" and t.j.get_trade(stuck)["status"] == "opening"
    assert alert_kinds(t).count("positions_empty") == 1          # once per streak
    fake.fetch_positions = real
    await reconcile.reconcile(t)
    assert t.j.get_trade(stuck)["status"] == "open" and t.j.get_trade(tid)["status"] == "open"


async def test_stuck_opening_rows_are_recovered_from_a_live_position_or_aborted(monkeypatch):
    t, fake = setup(monkeypatch)
    await fake.create_order(SYM, "market", "sell", 100)
    live, dead = (t.j.create_trade(s, "pump_fade", "t", 1.05, 2) for s in (SYM, SYM2))
    for x in (live, dead):
        t.j.update_trade(x, opened_at=iso(fake.now_ms() - 600_000))
    await reconcile.reconcile(t)
    assert t.j.get_trade(live)["status"] == "open" and t.j.get_trade(live)["amount"] == 100
    assert t.j.get_trade(dead)["status"] == "aborted"


async def test_an_entry_that_never_filled_is_aborted_when_no_trade_is_open(monkeypatch):
    t, fake = setup(monkeypatch)
    dead = t.j.create_trade(SYM2, "pump_fade", "t", 2.2, 2)
    t.j.update_trade(dead, opened_at=iso(fake.now_ms() - 600_000))
    await reconcile.reconcile(t)                                 # no position anywhere is no surprise then
    assert t.j.get_trade(dead)["status"] == "aborted" and "positions_empty" not in alert_kinds(t)


async def test_a_trade_is_closed_only_once_its_exit_fill_is_in_trade_history(monkeypatch):
    t, fake = setup(monkeypatch)
    await t.open_short(SYM2, stop_price=2.2, setup="pump_fade", thesis="t", notional=100)   # positions aren't empty
    tid = await short(t)
    fake.advance(60)
    fake.price = 1.05
    real = fake.fetch_my_trades

    async def lagging(s, since=None, limit=None, params=None):
        return [f for f in await real(s, since, limit, params) if f["side"] == "sell"]
    fake.fetch_my_trades = lagging
    assert await reconcile.reconcile(t) == []
    assert t.j.get_trade(tid)["status"] == "open" and "reconcile_waiting" in alert_kinds(t)
    fake.fetch_my_trades = real
    (res,) = await reconcile.reconcile(t)
    assert res["trade_id"] == tid and t.j.get_trade(tid)["exit_price"] == pytest.approx(1.05)


async def test_a_trade_held_over_a_week_is_closed_from_all_of_its_history(monkeypatch):
    t, fake = setup(monkeypatch)
    fake.clock_s = -9 * 86400
    tid = await short(t)
    fake.advance(9 * 86400)
    fake.price = 1.05                                            # ccxt's since-only query would stop at day 7
    await reconcile.reconcile(t)
    tr = t.j.get_trade(tid)
    assert tr["status"] == "closed" and tr["exit_price"] == pytest.approx(1.05)
    assert tr["pnl"] == pytest.approx(net(fake)) and tr["closed_at"] == iso(fake.fills[-1]["timestamp"])


async def test_a_fill_the_desk_joined_to_a_trade_after_its_stop_out_is_counted_in_it_once():
    d, t, fake = desk()
    fake.price = 1.035
    await d.propose_group([prop(1.04), prop(1.06, stop=1.12)])
    fail_cancel(fake, oid_at(fake, 1.06), times=99)              # its OCO cancel keeps failing
    fake.price = 1.045
    (first,) = await d.poll()
    assert (await t.move_stop(SYM, 1.05, "breakeven"))["ok"]
    fake.price = 1.07                                            # stopped at 1.05, then the sibling fills at 1.06
    assert [x["event"] for x in await d.poll()] == ["sibling_joined"]
    await reconcile.reconcile(t)
    assert t.j.get_trade(first["trade_id"])["status"] == "open"
    fake.price = 1.13
    await reconcile.reconcile(t)
    (tr,) = t.j.trades()
    assert tr["status"] == "closed" and tr["pnl"] == pytest.approx(sum(float(i["income"]) for i in fake.income))
    assert tr["closed_at"] == iso(fake.fills[-1]["timestamp"])


async def test_a_reconcile_failure_never_skips_the_stop_checks():
    fake = FakeCcxt()
    fake.add_symbol(SYM2, 2.0, tick=0.001)
    g = guardian_with(fake)
    await open_trade(g)
    r = await g.app.trader.open_short(SYM2, stop_price=2.1, setup="pump_fade", thesis="t", notional=100)
    fake.set_price(SYM2, 2.1)                                    # stopped out: reconcile reads its trade history
    fake.stops.clear()                                           # and the other position's stop went missing

    async def down(*a, **k):
        raise ccxt.ExchangeNotAvailable("binanceusdm GET https://demo-fapi.binance.com/fapi/v1/userTrades 503")
    fake.fetch_my_trades = down
    await g.check_positions()
    assert list(fake.stops.values()) == [1.05] and "stop_missing" in kinds(g)
    assert g.j.get_trade(r["trade_id"])["status"] == "open" and "reconcile_error" in kinds(g)


async def test_an_empty_answer_still_closes_a_trade_whose_own_exit_fill_proves_it(monkeypatch):
    t, fake = setup(monkeypatch)
    other = await short(t)
    r = await t.open_short(SYM2, stop_price=2.2, setup="pump_fade", thesis="t", notional=100)
    fake.advance(60)
    fake.set_price(SYM2, 2.2)                                    # stopped out: its exit fill is in its history

    async def blank():
        return []
    fake.fetch_positions = blank
    (res,) = await reconcile.reconcile(t)
    assert res["trade_id"] == r["trade_id"] and t.j.get_trade(other)["status"] == "open"
    (msg,) = [a["message"] for a in t.j.alerts(100) if a["kind"] == "positions_empty"]
    assert f"#{other} " in msg and f"#{r['trade_id']} " not in msg


async def test_a_desk_clock_ahead_of_binance_still_finds_each_entry_fill(monkeypatch):
    t, fake = setup(monkeypatch)
    monkeypatch.setattr("shortbot.journal.now_iso", lambda: iso(fake.now_ms() + 3000))   # opened_at after the fill
    old = await short(t)
    fake.advance(30)
    fake.price = 1.05
    fake.advance(10)                                             # shorted again 10 s after the stop-out
    new = await short(t, stop=1.10)
    fake.advance(30)
    fake.price = 1.10
    await reconcile.reconcile(t)
    first = next(f for f in fake.fills if f["side"] == "buy")
    o, n = t.j.get_trade(old), t.j.get_trade(new)
    assert o["pnl"] == pytest.approx(net(fake, first["timestamp"]))   # its entry commission included
    assert o["pnl"] + n["pnl"] == pytest.approx(net(fake)) and n["exit_price"] == pytest.approx(1.10)


async def test_trade_history_is_read_whole_when_a_window_holds_more_than_a_page():
    fake = FakeCcxt()
    await fake.create_order(SYM, "market", "sell", 100)
    f = fake.fills[0]
    fake.fills = [{**f, "id": str(i), "timestamp": f["timestamp"] + i * 100} for i in range(2500)]
    rows = await episodes.history(fake, SYM, f["timestamp"] - 1000)
    assert [x["id"] for x in rows] == [str(i) for i in range(2500)]
