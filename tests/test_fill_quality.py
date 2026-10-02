"""#35: every entry and exit fill is benchmarked against the MAINNET price at its fill (`fake.prod` is the keyless prod
client): the live book and mark for a fill the desk sees as it happens, the last prod print at or before the fill for
one it learns about late. Never awaited by the order path, never fails it: a capture that fails stores nothing and
is counted; one order is one reference, whichever path records it first."""

import asyncio
import logging

import ccxt
import pytest

from shortbot import fill_quality as fq
from shortbot import reconcile
from shortbot.fill_report import bps
from tests.fakes import SYM
from tests.test_entry_fills import later
from tests.test_entry_races import desk, prop
from tests.test_reconcile import setup, short
from tests.test_trading import open_default, trader


async def refs(t, tid):
    await fq.drain()
    return t.j.get_trade(tid)["fill_refs"]


def sold(fake, side="sell"):
    return [i for i, o in fake.market_orders.items() if o["side"] == side]


async def hang(*a, **k):
    await asyncio.Event().wait()


async def down(*a, **k):
    raise ccxt.NetworkError("prod unreachable")


async def test_a_market_entry_is_benchmarked_against_the_mainnet_book_and_mark_at_its_fill():
    t, fake = trader()
    fake.prod.price, fake.prod.spread = 1.01, 0.0002            # mainnet ~1% above the demo book that filled us
    r = await open_default(t)
    assert r["ok"], r
    fr = await refs(t, r["trade_id"])
    (f,) = fr["fills"]
    assert (f["leg"], f["type"], f["src"], f["order"]) == ("entry", "market", "book", sold(fake)[0])
    assert (f["px"], f["qty"]) == (pytest.approx(1.0), pytest.approx(r["amount"]))
    assert f["mark"] == pytest.approx(1.01) and f["bid"] < 1.01 < f["ask"]
    assert f["ms"] == fake.fills[0]["timestamp"] and f["ref_ms"] >= f["ms"] - 1000
    assert fr["vol24_usd"] == 100e6                             # the backtest's slippage input (mainnet 24h volume)
    assert bps(f) == pytest.approx(99.0, abs=0.5)               # sold ~99 bp below mainnet: worse for the short


async def test_a_failed_reference_never_fails_the_order_and_is_counted(monkeypatch):
    t, fake = trader()
    monkeypatch.setattr(fake.prod, "fapiPublicGetTickerBookTicker", down)
    monkeypatch.setattr(fake.prod, "fapiPublicGetPremiumIndex", down)
    r = await open_default(t)
    assert r["ok"] and fake.stops, r
    fr = await refs(t, r["trade_id"])
    assert fr["fills"] == [] and fr["missed"] == {sold(fake)[0]: "error"}


async def test_a_hanging_mainnet_never_delays_the_stop_and_times_out_into_a_count(monkeypatch):
    t, fake = trader()
    monkeypatch.setattr(fq, "TIMEOUT_S", 0.05)
    monkeypatch.setattr(fake.prod, "fapiPublicGetTickerBookTicker", hang)
    monkeypatch.setattr(fake.prod, "fapiPublicGetPremiumIndex", hang)
    r = await open_default(t)
    assert r["ok"] and fake.stops and fq.pending()               # returned, stop on, the capture still running
    fr = await refs(t, r["trade_id"])
    assert fr["fills"] == [] and fr["missed"] == {sold(fake)[0]: "timeout"}


async def test_one_mainnet_quote_failing_keeps_the_other():
    t, fake = trader()
    fake.prod.fapiPublicGetTickerBookTicker = down
    r = await open_default(t)
    (f,) = (await refs(t, r["trade_id"]))["fills"]
    assert f["mark"] == pytest.approx(1.0) and "bid" not in f and bps(f) == pytest.approx(0.0)


async def test_closes_and_partial_closes_are_exit_references_worse_when_bought_above_mainnet():
    t, fake = trader()
    r = await open_default(t)
    fake.price, fake.prod.price = 0.95, 0.94                    # the demo buys back 0.95, mainnet trades 0.94
    assert (await t.close(SYM, "half", fraction=0.5))["ok"]
    assert (await t.close(SYM, "rest", "lesson"))["ok"]
    fr = await refs(t, r["trade_id"])
    exits = [f for f in fr["fills"] if f["leg"] == "exit"]
    assert [f["type"] for f in exits] == ["market", "market"] and [f["order"] for f in exits] == sold(fake, "buy")
    assert sum(f["qty"] for f in exits) == pytest.approx(r["amount"])
    assert all(bps(f) == pytest.approx(106.4, abs=0.5) for f in exits)


async def test_a_limit_fill_the_poll_sees_late_is_benchmarked_at_its_own_fill_time(monkeypatch):
    d, t, fake = desk()
    fake.clock_s = -10                                         # it filled 10 s before the poll looked
    await d.propose_group([prop(1.04)])
    fake.price = 1.05
    ms = fake.fills[0]["timestamp"]
    fake.prod.print_at(1.031, ms - 900, buyer_maker=False)
    fake.prod.print_at(1.030, ms - 400)                       # the last mainnet print at the fill: 1.030 (bid)
    fake.prod.print_at(1.060, ms + 300)                       # after it: not the reference
    later(monkeypatch)
    (ev,) = await d.poll()
    (f,) = (await refs(t, ev["trade_id"]))["fills"]
    assert (f["leg"], f["type"], f["src"], f["ms"]) == ("entry", "limit", "trade", ms)
    assert (f["px"], f["last"], f["at"], f["ref_ms"]) == (pytest.approx(1.04), 1.03, "bid", ms - 400)
    assert bps(f) == pytest.approx(-97.1, abs=0.5)             # filled above mainnet: the demo flattered us


async def test_a_round_trip_gets_its_entry_and_its_stop_out_from_trade_history(monkeypatch):
    d, t, fake = desk()
    fake.clock_s = -20                                         # both fills 20 s before the poll
    await d.propose_group([prop(1.04)])
    fake.price = 1.12                                            # fills at 1.04, its pre-armed stop buys at 1.10
    sell, buy = fake.fills
    later(monkeypatch)
    (ev,) = await d.poll()
    assert ev["event"] == "round_trip"
    entry, out = (await refs(t, ev["trade_id"]))["fills"]
    assert (entry["leg"], entry["type"], entry["order"], entry["px"]) == ("entry", "limit", sell["order"],
                                                                         pytest.approx(1.04))
    assert (out["leg"], out["type"], out["order"], out["px"]) == ("exit", "exchange", buy["order"],
                                                                 pytest.approx(1.10))
    assert out["trigger"] == pytest.approx(1.10) and out["src"] == "trade" and out["ms"] == buy["timestamp"]


async def test_a_reconciled_stop_out_adds_only_its_exit_to_the_live_entry_reference(monkeypatch):
    t, fake = setup(monkeypatch)
    tid = await short(t)
    await fq.drain()
    fake.price = 1.10                                            # the closePosition stop at 1.05 fires
    assert [c["trade_id"] for c in await reconcile.reconcile(t)] == [tid]
    fr = await refs(t, tid)
    assert [(f["leg"], f["type"]) for f in fr["fills"]] == [("entry", "market"), ("exit", "exchange")]
    assert fr["fills"][1]["trigger"] == pytest.approx(1.05)


async def test_a_missed_entry_is_recovered_from_history_when_the_trade_closes(monkeypatch):
    t, fake = setup(monkeypatch)
    fake.prod.fapiPublicGetTickerBookTicker = fake.prod.fapiPublicGetPremiumIndex = down
    tid = await short(t)
    assert (await refs(t, tid))["missed"]
    del fake.prod.fapiPublicGetTickerBookTicker, fake.prod.fapiPublicGetPremiumIndex   # mainnet answers again
    fake.price = 1.10
    await reconcile.reconcile(t)
    fr = await refs(t, tid)
    assert [(f["leg"], f["src"]) for f in fr["fills"]] == [("entry", "book"), ("exit", "book")]
    assert list(fr["missed"]) == [fr["fills"][0]["order"]]       # still counted as missed live, but recovered


async def test_one_order_is_one_reference_whoever_records_it():
    t, fake = trader()
    r = await open_default(t)
    await fq.drain()
    oid = sold(fake)[0]
    for _ in range(2):
        fq.record(t, r["trade_id"], "entry", "market", oid, px=1.0, qty=1.0)
    fq.record(t, r["trade_id"], "exit", "target", "999", px=0.9, qty=1.0)   # any leg / type (#65's resting targets)
    fr = await refs(t, r["trade_id"])
    assert [(f["order"], f["type"]) for f in fr["fills"]] == [(oid, "market"), ("999", "target")]


async def test_no_trade_or_no_order_id_records_nothing():
    t, fake = trader()
    r = await open_default(t)
    before = await refs(t, r["trade_id"])
    fq.record(t, None, "exit", "market", {"id": "77", "average": 1.0, "filled": 1.0})       # not in the journal
    fq.record(t, r["trade_id"], "exit", "market", {"flat": True, "id": None, "average": None, "filled": None})
    fq.record_episode(t, {"id": r["trade_id"]}, {"sells": [{"order": "9"}]})                # malformed: no amount
    fq.record_fills(t, r["trade_id"], "entry", "limit", [{"order": "9", "price": "x", "amount": 1}])
    assert await refs(t, r["trade_id"]) == before and not fq.pending()


async def test_the_last_print_reads_through_full_pages_and_widens_for_a_thin_coin():
    t, fake = trader()
    ms = fake.now_ms() - 10_000
    for k in range(1500):                                      # 1500 prints in the 2 s before the fill: 2 pages
        fake.prod.print_at(1.0 + k * 1e-5, ms - 1500 + k)
    ref = await fq.capture(t.ex, SYM, ms)
    assert (ref["src"], ref["ref_ms"], ref["last"]) == ("trade", ms - 1, pytest.approx(1.01499))
    fake.prod.tape[SYM] = []
    fake.prod.print_at(0.97, ms - 30_000, buyer_maker=False)   # a thin coin: its last print 30 s before the fill
    assert await fq.capture(t.ex, SYM, ms) == {"src": "trade", "ref_ms": ms - 30_000, "last": 0.97, "at": "ask"}
    fake.prod.tape[SYM] = []
    assert await fq.capture(t.ex, SYM, ms) == {"why": "no_print"}
    assert await fq.capture(t.ex, SYM, fake.now_ms() - 49 * 3_600_000) == {"why": "too_old"}


@pytest.mark.parametrize("journal_closed", [False, True])
def test_a_capture_pending_at_shutdown_is_cancelled_quietly(caplog, journal_closed):
    """The guardian's and the CLI's loop (asyncio.run, close_app's bounded drain first) cancels what still runs at
    exit: no traceback, nothing for the loop's exception handler ('exception was never retrieved'), nothing pending.
    A journal closed underneath a pending capture: it is cancelled before it writes."""
    seen = []

    async def main():
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: seen.append(ctx))
        t, fake = trader()
        fake.prod.fapiPublicGetTickerBookTicker = hang
        fake.prod.fapiPublicGetPremiumIndex = hang
        r = await open_default(t)
        assert r["ok"] and fq.pending()
        await fq.drain(0.01)                                    # close_app: bounded, never raises
        assert fq.pending()
        if journal_closed:
            t.j.db.close()

    with caplog.at_level(logging.DEBUG):
        asyncio.run(main())
    assert seen == [] and not fq.pending()
    assert not [x for x in caplog.records if x.levelno >= logging.WARNING or x.exc_info], caplog.records
