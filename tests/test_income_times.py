"""Income rows are stamped to the whole second, 100-944 ms BEFORE their trade (probe_result 395, #75): every reader that
bounds income by a fill, an open, a close or a mark. A trade's own COMMISSION / REALIZED_PNL rows are matched by tradeId
against its fills; rows without one (funding, insurance) by time, from the second it opened."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from shortbot import equity_marks as em
from shortbot import reconcile
from shortbot.journal import Journal
from tests.fakes import SYM
from tests.test_entry_fills import later
from tests.test_entry_races import desk, prop
from tests.test_probe_folds import pin
from tests.test_reconcile import net, setup, short


def fees(*fills) -> float:
    return -sum(f["fee"]["cost"] for f in fills)


def income_of(fake, *fills) -> float:
    ids = {f["id"] for f in fills}
    return sum(float(r["income"]) for r in fake.income if r.get("tradeId") in ids)


# ------------------------------------------------------------ reconcile.finalize (episodes)
async def test_reconcile_counts_the_entry_commission_stamped_before_its_fill(monkeypatch):
    t, fake = setup(monkeypatch)
    pin(fake, 875)
    tid = await short(t)
    fake.advance(60)
    fake.price = 1.05                                            # stopped out on the exchange
    await reconcile.reconcile(t)
    tr = t.j.get_trade(tid)
    assert tr["status"] == "closed" and tr["fees"] == pytest.approx(fees(*fake.fills))
    assert tr["pnl"] == pytest.approx(net(fake))


async def test_a_reentry_a_moment_after_the_stop_out_keeps_its_income_out_of_the_old_trade(monkeypatch):
    """The old trade ends at the fill before the re-entry's (until_ms = 61.099 s), and the re-entry's COMMISSION is
    stamped 61.000 s: inside that bound by time, outside it by tradeId."""
    t, fake = setup(monkeypatch)
    pin(fake, 0)
    fake.clock_s = 0.3
    old = await short(t)
    fake.clock_s = 60.9
    fake.price = 1.05
    fake.clock_s = 61.1
    new = await short(t, stop=1.10)
    await reconcile.reconcile(t)
    o = t.j.get_trade(old)
    assert o["status"] == "closed" and o["fees"] == pytest.approx(fees(*fake.fills[:2]))
    assert o["pnl"] == pytest.approx(income_of(fake, *fake.fills[:2]))
    fake.clock_s = 120.4
    fake.price = 1.10
    await reconcile.reconcile(t)
    n = t.j.get_trade(new)
    assert n["status"] == "closed" and n["fees"] == pytest.approx(fees(*fake.fills[2:]))
    assert o["pnl"] + n["pnl"] == pytest.approx(net(fake))       # every row counted once


async def test_a_round_trip_is_journalled_with_both_commissions(monkeypatch):
    d, t, fake = desk()
    pin(fake, 875)
    await d.propose_group([prop(1.04)])
    fake.price = 1.12                                            # filled and stopped out in the same ms
    later(monkeypatch)
    (ev,) = await d.poll()
    assert ev["event"] == "round_trip"
    tr = t.j.get_trade(ev["trade_id"])
    assert tr["fees"] == pytest.approx(fees(*fake.fills)) and tr["pnl"] == pytest.approx(net(fake))


async def test_funding_inside_the_trade_and_from_its_opening_second_counts(monkeypatch):
    """Rows without a tradeId are bounded by time: from the second the trade opened to its until_ms."""
    t, fake = setup(monkeypatch)
    base = pin(fake, 500)
    tid = await short(t)
    fake.income.append({"symbol": "PUMPUSDT", "incomeType": "FUNDING_FEE", "income": "-0.25", "asset": "USDT",
                        "time": base - 500, "tranId": 1, "tradeId": ""})
    fake.advance(30)
    fake.price = 1.05
    fake.income.append({"symbol": "PUMPUSDT", "incomeType": "FUNDING_FEE", "income": "-9", "asset": "USDT",
                        "time": fake.now_ms() + 3_600_000, "tranId": 2, "tradeId": ""})   # long after: not its
    await reconcile.reconcile(t)
    assert t.j.get_trade(tid)["funding"] == pytest.approx(-0.25)


# ------------------------------------------------------------ finalize without an episode (the desk's own close)
async def test_the_desks_close_counts_the_entry_commission(monkeypatch):
    t, fake = setup(monkeypatch)
    pin(fake, 875)
    tid = await short(t)
    fake.advance(10)
    await t.close(SYM, "manual", "lesson")
    tr = t.j.get_trade(tid)
    assert tr["fees"] == pytest.approx(fees(*fake.fills)) and tr["pnl"] == pytest.approx(net(fake))


# ------------------------------------------------------------ income_since: pages inside one second
async def test_income_pages_that_end_inside_a_second_lose_no_row(monkeypatch):
    t, fake = setup(monkeypatch)
    sec = pin(fake, 0) - 10_000
    rows = [{"symbol": "PUMPUSDT", "incomeType": "COMMISSION", "income": "-0.01", "asset": "USDT", "time": sec,
             "tranId": i, "tradeId": str(i)} for i in range(999)]
    rows += [{**rows[0], "time": sec + 1000, "tranId": 1000 + i, "tradeId": str(1000 + i)} for i in range(3)]
    fake.income = rows
    got = await reconcile.income_since(t.t, sec)
    assert sorted(r["tranId"] for r in got) == sorted(r["tranId"] for r in rows)


# ------------------------------------------------------------ day / week P&L (trading.account, equity_marks)
async def test_day_pnl_keeps_a_fill_just_after_midnight_in_today(monkeypatch):
    t, fake = setup(monkeypatch)
    day0 = em.period_starts(datetime.now(UTC))["day"][1]
    fake.income += [{"symbol": "PUMPUSDT", "incomeType": "REALIZED_PNL", "income": "-7", "asset": "USDT",
                     "time": day0, "tranId": 1, "tradeId": "1"},          # a fill at 00:00:00.400
                    {"symbol": "PUMPUSDT", "incomeType": "COMMISSION", "income": "-3", "asset": "USDT",
                     "time": day0 - 1000, "tranId": 2, "tradeId": "2"}]   # a fill at 23:59:59.700 yesterday
    assert (await t.account())["realized_today"] == pytest.approx(-7)


T0 = datetime(2026, 10, 1, 0, 5, tzinfo=UTC)


def _row(kind, usdt, ms):
    return {"incomeType": kind, "income": str(usdt), "time": ms}


def test_a_close_just_before_the_mark_in_its_second_is_not_counted_twice():
    """The guardian closes, then marks: the mark's equity holds the close already."""
    j = Journal(":memory:")
    mark = T0 + timedelta(milliseconds=800)
    em.period_pnl(j, 1000.0, [], 0, 0, now=mark, unrealized=0.0)
    sec = int(T0.timestamp() * 1000)                             # stamped .000: the close was at .300
    rows = [_row("REALIZED_PNL", -50, sec), _row("COMMISSION", -1, sec)]
    r = em.period_pnl(j, 1000.0, rows, 0, 0, now=T0 + timedelta(hours=1), unrealized=0.0)
    assert r["day_pnl"] == 0 and not r["pending"]["day"]


def test_a_close_just_after_the_mark_in_its_second_is_counted():
    j = Journal(":memory:")
    mark = T0 + timedelta(milliseconds=200)
    em.period_pnl(j, 1000.0, [], 0, 0, now=mark, unrealized=-50.0)   # an open short 50 under water
    sec = int(T0.timestamp() * 1000)                             # stamped .000: the close was at .600
    rows = [_row("REALIZED_PNL", -50, sec), _row("COMMISSION", -1, sec)]
    r = em.period_pnl(j, 999.0, rows, 0, 0, now=T0 + timedelta(hours=1), unrealized=0.0)
    assert r["day_pnl"] == -1 and not r["pending"]["day"]
    assert json.loads(j.get("eq_mark:day:2026-10-01"))["ts"] == int(mark.timestamp() * 1000)
