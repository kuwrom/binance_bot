"""Market order ids on the trade (#76): every market entry, add and close journals its exchange order id with its client
id (#9), so a trade's position is found in trade history by id (episodes.match) and its income split by its own fills
(reconcile.trade_income); funding goes to the position held when it settled. Old trades without ids match by time."""

import asyncio

import pytest

from shortbot import episodes, fee_audit, fill_report, lab, order_ids, reconcile
from shortbot.journal import Journal
from tests.fakes import SYM, FakeEx
from tests.test_income_times import fees, income_of
from tests.test_probe_folds import pin
from tests.test_reconcile import net, setup, short


def funding(fake, ms: int, usdt: float) -> None:
    fake.income.append({"symbol": "PUMPUSDT", "incomeType": "FUNDING_FEE", "income": str(usdt), "asset": "USDT",
                        "time": ms, "tranId": 7000 + len(fake.income), "tradeId": ""})


def forget_ids(t, tid: int) -> None:
    """The trade as journalled before #76: no order ids."""
    t.j.db.execute("UPDATE trades SET fill_refs = json_remove(fill_refs, '$.orders') WHERE id=?", (tid,))
    t.j.db.commit()


async def desk_close_then_reentry(t, fake, fund: float | None = None) -> tuple[int, int, int]:
    """A opens at 0.1 s; the desk closes it at 30.2 s; B re-enters at 30.7 s: A's close and B's entry share the second
    30.000, where their income rows are stamped. `fund`: a funding row stamped 30.000 (A still held it then)."""
    base = pin(fake, 0)
    fake.clock_s = 0.1
    old = await short(t)
    fake.clock_s = 30.2
    if fund is not None:
        funding(fake, base + 30_000, fund)
    assert (await t.close(SYM, "manual", "lesson"))["ok"]
    fake.clock_s = 30.7
    new = await short(t, stop=1.10)
    return base, old, new


async def end(t, fake, how: str) -> None:
    fake.clock_s = 90.4
    if how == "desk":
        assert (await t.close(SYM, "manual", "lesson"))["ok"]
    else:
        fake.price = 1.10                                       # B's stop
        await reconcile.reconcile(t)


# ------------------------------------------------------------ the ids are journalled
async def test_entry_and_close_order_ids_are_journalled_with_their_client_ids(monkeypatch):
    t, fake = setup(monkeypatch)
    tid = await short(t)
    (entry,) = [k for k, o in fake.market_orders.items() if o["side"] == "sell"]
    assert (await t.close(SYM, "partial", "l", fraction=0.5))["ok"]
    assert (await t.close(SYM, "rest", "l"))["ok"]
    exits = sorted((k for k, o in fake.market_orders.items() if o["side"] == "buy"), key=int)
    ids = order_ids.read(t.j, tid)
    assert ids["entry"] == {entry: fake.market_orders[entry]["cid"]} and ids["entry"][entry].startswith("sb-o")
    assert ids["exit"] == {k: fake.market_orders[k]["cid"] for k in exits} and len(exits) == 2
    assert all(c.startswith("sb-c") for c in ids["exit"].values())


async def test_a_close_settled_by_its_client_id_journals_the_order_found(monkeypatch):
    t, fake = setup(monkeypatch)
    tid = await short(t)
    fake.ack_lost = 1                                            # the close ran, its answer was lost (-1007)
    assert (await t.close(SYM, "manual", "l"))["ok"]
    (close,) = [k for k, o in fake.market_orders.items() if o["side"] == "buy"]
    assert order_ids.read(t.j, tid)["exit"] == {close: fake.market_orders[close]["cid"]}


async def test_a_guardian_close_and_an_add_are_journalled_on_the_symbols_trade(monkeypatch):
    t, fake = setup(monkeypatch)
    tid = await short(t)
    add = await t.open_market(SYM, 10, "sb-a1-feedbeef")        # positioning.add_to_winner's send
    close = await t.close_market(SYM, fake.position)            # the guardian's forced close, stops._crossed...
    ids = order_ids.read(t.j, tid)
    assert ids["entry"][str(add["id"])] == "sb-a1-feedbeef" and str(close["id"]) in ids["exit"]


# ------------------------------------------------------------ matching: a close and a re-entry in one second
@pytest.mark.parametrize("how", ["desk", "stop"])
async def test_a_close_and_a_reentry_in_the_same_second_split_fees_and_income(monkeypatch, how):
    t, fake = setup(monkeypatch)
    _, old, new = await desk_close_then_reentry(t, fake)
    await end(t, fake, how)
    o, n = t.j.get_trade(old), t.j.get_trade(new)
    assert o["status"] == n["status"] == "closed"
    assert o["fees"] == pytest.approx(fees(*fake.fills[:2])) and o["pnl"] == pytest.approx(income_of(fake, *fake.fills[:2]))
    assert n["fees"] == pytest.approx(fees(*fake.fills[2:])) and n["pnl"] == pytest.approx(income_of(fake, *fake.fills[2:]))
    assert n["entry_price"] == pytest.approx(fake.fills[2]["price"])
    assert n["exit_price"] == pytest.approx(fake.fills[3]["price"]) and len(fake.fills) == 4
    assert o["pnl"] + n["pnl"] == pytest.approx(net(fake))       # every row counted once


async def test_the_reentry_is_matched_to_its_own_position_by_its_entry_id(monkeypatch):
    t, fake = setup(monkeypatch)
    _, old, new = await desk_close_then_reentry(t, fake)
    m = await episodes.match(t, t.j.get_trade(new))
    assert m["by"] == "ids" and m["orders"] == [str(fake.fills[2]["order"])] and m["state"] == "open"


# ------------------------------------------------------------ funding: the position held when it settled
@pytest.mark.parametrize("how", ["desk", "stop"])
async def test_funding_stamped_in_that_second_goes_to_the_trade_still_holding_it(monkeypatch, how):
    t, fake = setup(monkeypatch)
    _, old, new = await desk_close_then_reentry(t, fake, fund=-0.25)
    await end(t, fake, how)
    assert t.j.get_trade(old)["funding"] == pytest.approx(-0.25)
    assert t.j.get_trade(new)["funding"] == pytest.approx(0)
    assert t.j.get_trade(old)["pnl"] + t.j.get_trade(new)["pnl"] == pytest.approx(net(fake))


async def test_funding_after_a_stop_out_goes_to_the_reentry_not_to_both(monkeypatch):
    """A is stopped out at 29.6 s, B enters at 30.4 s, funding is stamped 30.000: nobody held at 30.000 itself, so it
    settled on B (opened within the settlement's lag, A long flat). Before #76 A counted it (to B's first fill) AND B
    (from its opening second)."""
    t, fake = setup(monkeypatch)
    base = pin(fake, 0)
    fake.clock_s = 0.1
    old = await short(t)
    fake.clock_s = 29.6
    fake.price = 1.05                                           # A's stop
    fake.clock_s = 30.4
    new = await short(t, stop=1.10)
    funding(fake, base + 30_000, -0.4)
    await reconcile.reconcile(t)
    await end(t, fake, "stop")
    o, n = t.j.get_trade(old), t.j.get_trade(new)
    assert o["funding"] == pytest.approx(0) and n["funding"] == pytest.approx(-0.4)
    assert o["pnl"] + n["pnl"] == pytest.approx(net(fake))


def test_holds_is_exclusive_between_neighbours():
    a = {"opened_ms": 100, "closed_ms": 30_200, "prior_ms": None}
    b = {"opened_ms": 30_700, "closed_ms": 90_400, "prior_ms": 30_200}
    for ms in (0, 30_000, 31_000, 45_000, 90_000, 91_000):
        assert episodes.holds(a, ms) + episodes.holds(b, ms) <= 1, ms
    assert episodes.holds(a, 30_000) and episodes.holds(b, 31_000) and not episodes.holds(b, 91_000)
    late = {"opened_ms": 30_400, "closed_ms": 90_400, "prior_ms": 29_600}     # opened after the stamp, A flat by it
    assert episodes.holds(late, 30_000) and not episodes.holds(late, 30_000 - episodes.FUNDING_LAG_MS)


# ------------------------------------------------------------ old trades: by time
async def test_an_old_trade_without_ids_still_matches_by_time(monkeypatch):
    t, fake = setup(monkeypatch)
    pin(fake, 500)
    tid = await short(t)
    forget_ids(t, tid)
    assert order_ids.read(t.j, tid) == {"entry": {}, "exit": {}}
    fake.advance(60)
    fake.price = 1.05
    m = await episodes.match(t, t.j.get_trade(tid))
    assert m["state"] == "closed" and m["by"] == "time"
    await reconcile.reconcile(t)
    tr = t.j.get_trade(tid)
    assert tr["status"] == "closed" and tr["pnl"] == pytest.approx(net(fake))


async def test_an_old_trade_closed_by_the_desk_is_finalized_as_before(monkeypatch):
    t, fake = setup(monkeypatch)
    pin(fake, 875)
    tid = await short(t)
    forget_ids(t, tid)
    fake.advance(10)
    assert (await t.close(SYM, "manual", "lesson"))["ok"]
    tr = t.j.get_trade(tid)
    assert tr["fees"] == pytest.approx(fees(*fake.fills)) and tr["pnl"] == pytest.approx(net(fake))


# ------------------------------------------------------------ the fee audit
async def same_second_journalled_the_old_way(t, fake) -> tuple[int, int]:
    """Both trades closed, then B's row as pre-#76 matching left it: A's close commission counted in B too."""
    _, old, new = await desk_close_then_reentry(t, fake)
    await end(t, fake, "desk")
    n, wrong = t.j.get_trade(new), fake.fills[1]["fee"]["cost"]
    t.j.update_trade(new, fees=n["fees"] - wrong, pnl=n["pnl"] - wrong)
    return old, new


async def test_the_fee_audit_matches_by_ids_where_the_trade_has_them(monkeypatch):
    t, fake = setup(monkeypatch)
    old, new = await same_second_journalled_the_old_way(t, fake)
    fake.clock_s = 120.0
    gone = await short(t, stop=1.20)                             # a third trade, journalled before #76
    forget_ids(t, gone)
    fake.clock_s = 150.3
    fake.price = 1.20
    await reconcile.reconcile(t)
    r = await fee_audit.audit(t)
    rows = {x["id"]: x for x in r["trades"]}
    assert (rows[old]["matched"], rows[new]["matched"], rows[gone]["matched"]) == ("ids", "ids", "time")
    assert rows[old]["diff"]["net"] == pytest.approx(0)
    assert rows[new]["after"]["fees"] == pytest.approx(fees(*fake.fills[2:4]))
    assert rows[new]["diff"]["fees"] == pytest.approx(fake.fills[1]["fee"]["cost"])
    assert r["totals"]["net"] == pytest.approx(fake.fills[1]["fee"]["cost"]) and r["skipped"] == 0


def test_lab_fee_audit_uses_the_journalled_ids(monkeypatch):
    t, fake = setup(monkeypatch)
    old, new = asyncio.run(same_second_journalled_the_old_way(t, fake))
    monkeypatch.setattr(lab, "journal", lambda: t.j)
    monkeypatch.setattr(lab, "_with_ex", lambda fn: asyncio.run(fn(FakeEx(fake))))
    r = lab.fee_audit()
    rows = {x["id"]: x for x in r["trades"]}
    assert rows[new]["matched"] == "ids" and rows[new]["diff"]["fees"] == pytest.approx(fake.fills[1]["fee"]["cost"])


def test_the_fill_report_does_not_count_order_ids_as_references():
    j = Journal(":memory:")
    tid = j.create_trade(SYM, "pump_fade", "t", 1.05, 2)
    order_ids.note(j, SYM, "entry", {"id": 41, "clientOrderId": "sb-o1-00aa11bb"})
    order_ids.note(j, SYM, "exit", {"id": None, "clientOrderId": "sb-c-flat"})   # a flat answer: no id, nothing
    assert order_ids.read(j, tid) == {"entry": {"41": "sb-o1-00aa11bb"}, "exit": {}}
    assert fill_report.report(j)["coverage"]["trades_with_refs"] == 0
