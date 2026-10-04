"""#46: every fill and every income row counts once, however often the desk is told about it.

- REST: a userTrades or income page answered twice (each row repeated). Get-Income-History doc: "tranId is unique in
  the same incomeType for a user", so a repeated (incomeType, tranId) is one row; a userTrades row is one fill by its
  id (episodes.history already merges its reads by it). Trade PnL, fees, the round trip's size and day P&L count each
  once. Paged reads that overlap inside a second: test_income_times (pages ending inside a second).
- user-data stream: PARTIALLY_FILLED then FILLED events, each delivered twice and every batch reversed, adopt the
  entry once at its whole size; the stream trigger and the scheduled 5 s poll meeting the same fill make one
  action_fill. Duplicated and reversed events of a FULL fill and its stop-out: test_user_stream_guardian
  (duplicate_and_reordered_events_are_harmless)."""

import asyncio
from datetime import datetime

import pytest

from shortbot import guardian as guardian_mod
from shortbot import reconcile
from shortbot import user_stream as us
from shortbot.entries import EntryDesk
from shortbot.journal import Journal
from shortbot.trading import Trader
from tests.fake_faults import FaultyCcxt
from tests.fake_stream import Feed, Hub, order_event
from tests.fakes import SYM, FakeEx, neutral_budget, ok_prechecks
from tests.test_entries import favorable, prop
from tests.test_entry_fills import later
from tests.test_guardian import guardian_with, kinds, open_trade
from tests.test_probe_folds import pin
from tests.test_reconcile import iso
from tests.test_user_stream import until
from tests.test_user_stream_guardian import live


def trader(monkeypatch, **kw) -> tuple[Trader, FaultyCcxt]:
    fake = FaultyCcxt(**kw)
    pin(fake, 875)   # rows stamped to the second, 875 ms before their fills (probe_result 395)
    monkeypatch.setattr("shortbot.journal.now_iso", lambda: iso(fake.now_ms()))
    return Trader(FakeEx(fake), Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget), fake


def truth(fake) -> tuple[float, float]:
    """(net income, fees) as the exchange booked them, each row once."""
    return (sum(float(i["income"]) for i in fake.income), -sum(f["fee"]["cost"] for f in fake.fills))


def twice(fake) -> None:
    fake.dup_trades = fake.dup_income = True


# ------------------------------------------------------------ REST pages answered twice
async def test_a_stop_out_reconciled_from_pages_served_twice_counts_pnl_and_fees_once(monkeypatch):
    t, fake = trader(monkeypatch)
    tid = (await t.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100))["trade_id"]
    fake.advance(60)
    fake.price = 1.06                                       # stopped out on the exchange
    twice(fake)
    (done,) = await reconcile.reconcile(t)
    tr = t.j.get_trade(tid)
    net, fees = truth(fake)
    assert tr["status"] == "closed" and tr["exit_price"] == 1.05 and done["trade_id"] == tid
    assert tr["pnl"] == pytest.approx(net) and tr["fees"] == pytest.approx(fees)
    assert await reconcile.reconcile(t) == [] and t.j.get_trade(tid)["pnl"] == pytest.approx(net)   # once


async def test_the_desks_own_close_from_pages_served_twice_counts_pnl_and_fees_once(monkeypatch):
    t, fake = trader(monkeypatch)
    tid = (await t.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100))["trade_id"]
    fake.advance(10)
    twice(fake)
    assert (await t.close(SYM, "manual", "lesson"))["ok"]
    tr = t.j.get_trade(tid)
    net, fees = truth(fake)
    assert tr["pnl"] == pytest.approx(net) and tr["fees"] == pytest.approx(fees)


async def test_a_round_trip_rebuilt_from_pages_served_twice_is_journalled_at_its_own_size(monkeypatch):
    fake = FaultyCcxt(price=1.035)
    pin(fake, 875)
    t = Trader(FakeEx(fake), Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
    d = EntryDesk(t, regime_fn=favorable)
    (p,) = (await d.propose_group([prop(1.04)]))["placed"]
    fake.price = 1.12                                       # filled and stopped out (its pre-arm) before the poll
    later(monkeypatch)
    twice(fake)
    (ev,) = await d.poll()
    tr = t.j.get_trade(ev["trade_id"])
    net, fees = truth(fake)
    assert ev["event"] == "round_trip" and ev["qty"] == p["amount"] == tr["amount"]
    assert tr["pnl"] == pytest.approx(net) and tr["fees"] == pytest.approx(fees)


async def test_day_pnl_from_income_served_twice_counts_each_row_once(monkeypatch):
    t, fake = trader(monkeypatch)
    await t.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100)
    fake.advance(10)
    assert (await t.close(SYM, "manual", "lesson"))["ok"]
    once = (await t.account())["realized_today"]
    twice(fake)
    assert (await t.account())["realized_today"] == pytest.approx(once) == pytest.approx(truth(fake)[0])


async def test_funding_tracked_from_income_served_twice_counts_each_row_once():
    fake = FaultyCcxt()
    g = guardian_with(fake)
    tid = await open_trade(g)
    since = int(datetime.fromisoformat(g.j.get_trade(tid)["opened_at"]).timestamp() * 1000)
    fake.income += [{"symbol": "PUMPUSDT", "incomeType": "FUNDING_FEE", "income": "-0.25", "asset": "USDT",
                     "time": since + 1000 * n, "tranId": 7700 + n, "tradeId": ""} for n in (1, 2)]
    fake.dup_income = True
    await g._track_funding(SYM)
    await g.j.flush()
    assert g.j.get_trade(tid)["funding"] == pytest.approx(-0.5)


# ------------------------------------------------------------ the user-data stream
@pytest.fixture
def streams(monkeypatch):
    """test_user_stream_guardian's timings: the stream reconnects in ms, the lane ticks every 10 ms."""
    for k, v in {"WAKE_S": 0.005, "CONNECT_S": 0.3, "REST_S": 0.3, "BACKOFF_MIN_S": 0.001, "BACKOFF_MAX_S": 0.004,
                 "CLOSE_S": 0.05, "UP_MARGIN_MS": -1000}.items():
        monkeypatch.setattr(us, k, v)
    monkeypatch.setattr(guardian_mod, "TRIGGER_GAP_S", {"entries": 0.0, "positions": 0.0})
    monkeypatch.setattr(guardian_mod, "TICK_S", 0.01)

    def schedule(entries_s: float) -> None:
        monkeypatch.setattr(guardian_mod, "PROTECTIVE", {"entries": (entries_s, 240), "positions": (3000, 240),
                                                         "entry_reconcile": (3000, 240)})
    return schedule


async def resting_entry():
    fake = FaultyCcxt(price=1.035)
    g = guardian_with(fake)
    g.entries._regime_fn = favorable
    (p,) = (await g.entries.propose_group([prop(1.04)]))["placed"]
    return g, fake, next(iter(fake.limits)), p["amount"]


async def test_partial_then_full_fill_events_twice_and_reversed_adopt_the_entry_once_at_its_whole_size(streams):
    streams(3000)                                            # only the stream runs the fill poll
    g, fake, oid, qty = await resting_entry()
    hub = Hub(fake)
    async with live(g, hub) as lane:
        (sock,) = hub.live()                                 # the guardian's connection
        fake.fill_limit(oid, qty=30)                         # two fills before the stream's next read: Binance pushes
        part = order_event(fake, oid, "TRADE", "PARTIALLY_FILLED", 30)   # one TRADE event each
        fake.fill_limit(oid)
        full = order_event(fake, oid, "TRADE", "FILLED", qty - 30)
        sock.feed = Feed(fake)                               # its baseline: both are in this hand-made batch
        sock.queue += [full, full, part, part]               # each twice, the batch reversed
        await until(lambda: g.j.open_trades(), limit_s=1.0)
        await until(lambda: not sock.queue, limit_s=1.0)
        await asyncio.sleep(0.05)                            # the late PARTIALLY_FILLED runs the poll again
    (tr,) = g.j.open_trades()
    assert tr["amount"] == qty == fake.position and lane.trigger_stats["entries"]["runs"] >= 1
    assert [e["status"] for e in g.entries._rows()] == ["filled"] and kinds(g).count("action_fill") == 1
    assert list(fake.stops.values()) == [1.10] and fake.prearms == {}
    assert len([o for o in fake.orders if (o[3] or {}).get("closePosition")]) == 1


async def test_the_stream_trigger_and_the_scheduled_poll_on_one_fill_make_one_action_fill(streams):
    streams(0.02)                                            # the 5 s poll, every 20 ms here
    g, fake, oid, qty = await resting_entry()
    async with live(g, Hub(fake)) as lane:
        runs = lane.trigger_stats.get("entries", {}).get("runs", 0)
        fake.fill_limit(oid)
        await until(lambda: g.j.open_trades(), limit_s=1.0)
        await asyncio.sleep(0.15)                            # several scheduled polls meet the same fill after it
    assert lane.trigger_stats["entries"]["runs"] > runs     # the stream woke the poll at least once
    (tr,) = g.j.open_trades()
    assert tr["amount"] == qty and kinds(g).count("action_fill") == 1 and len(g.j._rows("SELECT id FROM trades")) == 1
    assert list(fake.stops.values()) == [1.10] and fake.prearms == {}
    assert len([o for o in fake.orders if (o[3] or {}).get("closePosition")]) == 1
