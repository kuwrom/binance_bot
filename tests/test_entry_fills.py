"""Fills the 5 s poll learns about late (#8): a limit that filled and was closed again before the poll is rebuilt from
trade history as one closed trade; every limit trade opens at its fill (exchange clock), not at the poll; protection
is make-before-break and a partial fill's remainder is pulled without losing track of it. Moving `fake.price` fills
and triggers on its own (fake_engine.py); the journal is compared with the fake's fills and income."""

from datetime import datetime, timedelta, timezone

import pytest

from shortbot import reconcile
from shortbot.entries_fills import ROUND_TRIP_TRIES, StopNotPlaced
from tests.fakes import SYM
from tests.test_entries_cancel import fail_cancel, oid_at, recorder
from tests.test_entry_races import SYM2, desk, prop


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def later(monkeypatch, seconds=5):
    """The poll runs a tick after the fills: anything stamped 'now' lands after them (income startTime is a hard
    bound, so a trade opened at the poll misses its own entry commission)."""
    ts = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    monkeypatch.setattr("shortbot.journal.now_iso", lambda: ts)
    monkeypatch.setattr("shortbot.tranches._now", lambda: ts)


def net_income(fake):
    return sum(float(i["income"]) for i in fake.income)


async def test_a_round_trip_is_journalled_with_exchange_truth_pnl_at_its_own_times(monkeypatch):
    d, t, fake = desk()
    sent = recorder(t)
    eid = (await d.propose_group([prop(1.04)]))["placed"][0]["entry_id"]
    fake.price = 1.12                                            # fills at 1.04, its pre-armed stop buys at 1.10
    sell, buy = fake.fills
    later(monkeypatch)
    ev = await d.poll()
    assert [x["event"] for x in ev] == ["round_trip"]
    tr = t.j.get_trade(ev[0]["trade_id"])
    assert tr["status"] == "closed" and (tr["entry_price"], tr["exit_price"]) == pytest.approx((1.04, 1.10))
    assert (tr["opened_at"], tr["closed_at"]) == (iso(sell["timestamp"]), iso(buy["timestamp"]))
    assert tr["pnl"] == pytest.approx(net_income(fake))                     # realized + BOTH commissions
    assert tr["fees"] == pytest.approx(-(sell["fee"]["cost"] + buy["fee"]["cost"]))
    assert "pre-armed stop" in tr["exit_reason"] and tr["mae_pct"] > 5
    (res,) = t.j.closed_results()
    assert -1.1 < res["r_multiple"] < -1.0                                  # a full 1R loss reaches the R stats
    row = d._rows("id=?", (eid,))[0]
    assert (row["status"], row["trade_id"], row["filled_at"]) == ("filled", tr["id"], iso(sell["timestamp"]))
    assert fake.prearms == {} and d.pending() == []
    ((msg, urgent),) = [(m, u) for k, m, s, u in sent if k == "action_fill"]
    assert urgent and f"trade #{tr['id']}" in msg and "lesson" in msg


async def test_a_limit_trade_opens_at_its_fill_so_its_close_counts_the_entry_commission(monkeypatch):
    d, t, fake = desk()
    await d.propose_group([prop(1.04)])
    fake.price = 1.05
    (sell,) = fake.fills
    later(monkeypatch)
    (ev,) = await d.poll()
    tr = t.j.get_trade(ev["trade_id"])
    assert tr["opened_at"] == iso(sell["timestamp"]) == d._rows("1=1")[0]["filled_at"]
    fake.price = 1.0
    await t.close(SYM, "target", "lesson")
    assert t.j.get_trade(tr["id"])["fees"] == pytest.approx(-sum(f["fee"]["cost"] for f in fake.fills))


async def test_a_partial_fill_that_expired_then_stopped_out_is_journalled_at_its_filled_size(monkeypatch):
    d, t, fake = desk()
    await d.propose_group([prop(1.04)], ttl_hours=1)
    oid = next(iter(fake.limits))
    half = fake.limits[oid]["amount"] / 2
    fake.fill_limit(oid, qty=half)
    fake.advance(3700)                                          # its GTD lapses with half filled (no lastTrade time)
    fake.price = 1.12                                           # the pre-armed stop buys back that half
    later(monkeypatch)
    (ev,) = await d.poll()
    tr = t.j.get_trade(ev["trade_id"])
    assert ev["event"] == "round_trip" and tr["amount"] == pytest.approx(half)
    assert tr["opened_at"] == iso(fake.fills[0]["timestamp"]) and tr["pnl"] == pytest.approx(net_income(fake))


async def test_same_symbol_siblings_filled_and_stopped_out_between_polls_are_one_round_trip(monkeypatch):
    d, t, fake = desk()
    fake.price = 1.035
    await d.propose_group([prop(1.04), prop(1.06, stop=1.12)])
    fake.price = 1.13                                           # both fill, both pre-armed stops buy back
    assert fake.position == 0 and [f["side"] for f in fake.fills] == ["sell", "sell", "buy", "buy"]
    later(monkeypatch)
    assert [x["event"] for x in await d.poll()] == ["round_trip"]
    (tr,) = t.j.trades()
    assert tr["amount"] == pytest.approx(sum(f["amount"] for f in fake.fills if f["side"] == "sell"))
    assert tr["status"] == "closed" and tr["pnl"] == pytest.approx(net_income(fake))
    assert {(x["status"], x["trade_id"]) for x in d._rows("1=1")} == {("filled", tr["id"])}


async def test_a_fill_into_a_journalled_position_that_closed_before_the_poll_is_counted_once():
    d, t, fake = desk()
    fake.price = 1.035
    sib = (await d.propose_group([prop(1.04), prop(1.06, stop=1.12)]))["placed"][1]["entry_id"]
    fail_cancel(fake, oid_at(fake, 1.06), times=2)              # its OCO cancel fails: the sibling stays live
    fake.price = 1.05
    (first,) = await d.poll()                                   # trade open, whole-position stop at 1.10
    sent = recorder(t)
    fake.price = 1.13                                           # the sibling fills; the 1.10 stop closes both
    assert fake.position == 0
    ev = await d.poll()
    assert [(x["event"], x["trade_id"]) for x in ev] == [("fill_linked", first["trade_id"])]
    assert d._rows("id=?", (sib,))[0]["trade_id"] == first["trade_id"] and fake.prearms == {}
    assert [u for k, m, s, u in sent if k == "action_fill"] == [True]
    await reconcile.reconcile(t)                                # the 60 s pass closes it, once, with all of it
    (tr,) = t.j.trades()
    assert tr["status"] == "closed" and tr["pnl"] == pytest.approx(net_income(fake))


async def test_a_round_trip_missing_from_trade_history_waits_then_errors_loudly():
    d, t, fake = desk()
    sent = recorder(t)
    r = await d.propose_group([prop(1.04), prop(2.08, stop=2.2, symbol=SYM2)])
    eid, sib = (x["entry_id"] for x in r["placed"])
    fake.price = 1.12

    async def lagging(s, since=None, limit=None):
        return []
    fake.fetch_my_trades = lagging
    for _ in range(ROUND_TRIP_TRIES - 1):
        assert [x["event"] for x in await d.poll()] == ["fill_unresolved"]
        assert [x["id"] for x in d.pending()] == [eid] and t.j.trades() == []
    assert d._rows("id=?", (sib,))[0]["status"] == "cancelled"   # the fill is certain: its OCO sibling went at once
    assert [x["event"] for x in await d.poll()] == ["error"]
    assert d._rows("id=?", (eid,))[0]["status"] == "error" and t.j.trades() == []
    assert [u for k, m, s, u in sent if k == "fill_unresolved"] == [True]


async def test_an_order_adopted_after_its_round_trip_was_journalled_links_to_that_trade():
    d, t, fake = desk()
    fake.price = 1.035
    a, b = (x["entry_id"] for x in (await d.propose_group([prop(1.04), prop(1.06, stop=1.12)]))["placed"])
    d._update(b, status="unknown")                              # its send's outcome unknown (#13 resolves it later)
    fake.price = 1.13
    (ev,) = await d.poll()
    assert ev["event"] == "round_trip" and d._rows("id=?", (b,))[0]["status"] == "unknown"
    assert d._settle(b, "unknown", status="pending")            # entry_intents adopts it: found, filled
    assert [(x["event"], x["trade_id"]) for x in await d.poll()] == [("fill_linked", ev["trade_id"])]
    (tr,) = t.j.trades()
    assert tr["pnl"] == pytest.approx(net_income(fake)) and d._rows("id=?", (b,))[0]["trade_id"] == tr["id"]


async def test_a_fill_keeps_its_prearmed_stop_until_the_whole_position_stop_rests():
    d, t, fake = desk()
    sent = recorder(t)
    fake.price = 1.035
    await d.propose_group([prop(1.04)])
    fake.price = 1.05
    fake.fail_stop = 1                                          # the closePosition stop is refused (not retried)
    with pytest.raises(StopNotPlaced):
        await d.poll()                                          # its only entry failed: the pass fails too
    assert list(fake.prearms.values()) == [1.10] and fake.stops == {} and t.j.trades() == []
    assert len(d.pending()) == 1 and [u for k, m, s, u in sent if k == "entry_poll_failing"] == [True]
    assert [x["event"] for x in await d.poll()] == ["filled"]
    assert list(fake.stops.values()) == [1.10] and fake.prearms == {}


async def test_a_partial_fill_whose_remainder_wont_cancel_is_protected_and_journalled_once_settled():
    d, t, fake = desk()
    fake.price = 1.035
    await d.propose_group([prop(1.04)])
    oid = next(iter(fake.limits))
    fake.fill_limit(oid, qty=fake.limits[oid]["amount"] / 2)
    fail_cancel(fake, oid, times=1)
    assert [x["event"] for x in await d.poll()] == ["fill_unsettled"]
    assert list(fake.stops.values()) == [1.10] and fake.prearms == {}   # protected before anything else
    assert t.j.trades() == [] and len(d.pending()) == 1                 # still tracked: more may fill
    fake.fill_limit(oid, qty=fake.limits[oid]["amount"] / 4)            # and more does, before the retry
    assert [x["event"] for x in await d.poll()] == ["filled"]
    (tr,) = t.j.trades()
    assert tr["amount"] == pytest.approx(fake.position) and fake.limits[oid]["status"] == "canceled"


async def test_a_split_entry_leg_that_joins_is_stamped_at_its_own_fill(monkeypatch):
    d, t, fake = desk()
    fake.price = 1.035
    await d.propose_group([prop(1.04, tranches=[{"limit_price": 1.04}, {"limit_price": 1.07}])])
    fake.price = 1.05
    await d.poll()
    fake.price = 1.08
    leg2 = fake.fills[-1]
    later(monkeypatch)
    assert [x["event"] for x in await d.poll()] == ["tranche_joined"]
    assert d._rows("leg=2")[0]["filled_at"] == iso(leg2["timestamp"])
