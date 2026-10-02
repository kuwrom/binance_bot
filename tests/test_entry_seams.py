"""Seams of the entry order path (#60): sends of unknown outcome are cancelled with their OCO group and polled for
fills by client id; a fill's trade row is written inside the symbol lock, before a session's close can look for it;
a cancel never relabels a row another process resolved first."""

import asyncio

from shortbot import entry_intents
from shortbot.entries import EntryDesk
from shortbot.journal import Journal
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks
from tests.test_entries import favorable, prop
from tests.test_entry_intents import age, on_send, open_limits, row

SYM2 = "MOVR/USDT:USDT"
SPLIT = [{"limit_price": 1.04}, {"limit_price": 1.07}]


def desk():
    fake = FakeCcxt(price=1.035)             # a slow approach (the fast-approach rule stays quiet)
    fake.add_symbol(SYM2, 2.0, tick=0.001)
    t = Trader(FakeEx(fake), Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
    return EntryDesk(t, regime_fn=favorable), t, fake


def oid_at(fake, price):
    return next(i for i, o in fake.limits.items() if o["price"] == price)


# ------------------------------------------------------------ item 1: 'unknown' rows
async def test_a_fill_cancels_an_oco_sibling_whose_send_is_unknown():
    d, t, fake = desk()
    on_send(fake, "limit", 2, arm="ack_lost")                    # the 2nd order rests, its answer was lost
    r = await d.propose_group([prop(1.04), prop(1.06, stop=1.12)])
    sib = r["unknown"][0]["entry_id"]
    fake.price = 1.045                                           # the 1.04 order fills, the 1.06 one rests
    assert [e["event"] for e in await d.poll()] == ["filled"]
    assert open_limits(fake) == [] and fake.limits[oid_at(fake, 1.06)]["filled"] == 0
    assert row(d, sib)["status"] == "cancelled" and row(d, sib)["order_id"] == oid_at(fake, 1.06)
    fake.price = 1.07
    assert fake.position == fake.limits[oid_at(fake, 1.04)]["amount"]   # one order of the group filled, not two


async def test_cancelling_a_group_withdraws_its_unknown_send_for_good():
    d, t, fake = desk()
    fake.ack_lost = 1
    r = await d.propose_group([prop(1.04)])
    eid = r["unknown"][0]["entry_id"]
    out = await d.cancel_group(r["group_id"], "read changed")
    assert out["ok"] and out["cancelled"] == 1 and open_limits(fake) == []
    await entry_intents.reconcile(d)                             # not revived as a resting entry
    assert row(d, eid)["status"] == "cancelled"


async def test_cancelling_an_unknown_send_that_never_reached_the_book_rejects_it_after_the_grace():
    d, t, fake = desk()
    fake.ack_lost_unsent = 1
    r = await d.propose_group([prop(1.04)])
    eid = r["unknown"][0]["entry_id"]
    out = await d.cancel_group(r["group_id"], "read changed")    # may still reach the book: can't tell yet
    assert not out["ok"] and out["cancel_failed"] == [eid] and row(d, eid)["status"] == "unknown"
    age(d, eid)
    out = await d.cancel_group(r["group_id"], "read changed")
    assert out["ok"] and row(d, eid)["status"] == "rejected"     # as the reconciler labels it (explained_symbols)


async def test_the_poll_adopts_and_protects_a_filled_unknown_send_in_one_pass():
    d, t, fake = desk()
    fake.ack_lost = 1
    eid = (await d.propose_group([prop(1.04)]))["unknown"][0]["entry_id"]
    assert fake.prearms == {}                                    # nothing protects it until it is adopted
    fake.fill_limit(open_limits(fake)[0])
    assert [e["event"] for e in await d.poll()] == ["filled"]    # no reconciler tick needed
    assert list(fake.stops.values()) == [1.10] and row(d, eid)["status"] == "filled"
    assert t.j.active_trade(SYM)["status"] == "open"


async def test_the_poll_leaves_a_young_unfound_send_unknown_then_rejects_it():
    d, t, fake = desk()
    fake.ack_lost_unsent = 1
    eid = (await d.propose_group([prop(1.04)]))["unknown"][0]["entry_id"]
    assert await d.poll() == [] and row(d, eid)["status"] == "unknown"
    age(d, eid)
    await d.poll()
    assert row(d, eid)["status"] == "rejected"


async def test_a_busy_sibling_symbol_never_swallows_the_fill():
    d, t, fake = desk()
    r = await d.propose_group([prop(1.04), prop(2.08, stop=2.2, symbol=SYM2)])
    sib = r["placed"][1]["entry_id"]
    t.symlocks.wait_s = 0.05
    release = asyncio.Event()

    async def hold():
        async with t.symbol_lock(SYM2, "a session action"):
            await release.wait()
    h = asyncio.create_task(hold())
    await asyncio.sleep(0.01)
    fake.price = 1.045
    assert [e["event"] for e in await d.poll()] == ["filled"]
    assert any(f"#{sib} cancel failed" in a["message"] for a in t.j.alerts() if a["kind"] == "action_fill")
    release.set()
    await h
    await d.poll()                                               # the sibling rule retries it
    assert row(d, sib)["status"] == "cancelled" and open_limits(fake) == []


# ------------------------------------------------------------ item 2: the trade row inside the lock
def slow_history(fake):
    """The first trade-history read (the poll's) answers only once `go` is set; `asked` is set when it is made."""
    asked, go, real = asyncio.Event(), asyncio.Event(), fake.fetch_my_trades

    async def history(*a, **k):
        if not asked.is_set():
            asked.set()
            await go.wait()
        return await real(*a, **k)
    fake.fetch_my_trades = history
    return asked, go


async def close_while_the_poll_reads_history(d, t, fake):
    asked, go = slow_history(fake)

    async def session():
        await asked.wait()
        task = asyncio.create_task(t.close(SYM, "by hand"))
        await asyncio.sleep(0.05)                                # runs now unless the poll holds the symbol
        go.set()
        return await task
    _, res = await asyncio.gather(d.poll(), session())
    return res


async def test_a_session_close_during_a_fill_adoption_finds_and_finalizes_its_trade():
    d, t, fake = desk()
    await d.propose_group([prop(1.04)])
    fake.price = 1.06                                            # filled; the poll protects it, then journals it
    res = await close_while_the_poll_reads_history(d, t, fake)
    assert res["ok"] and res["result"] != {"note": "not in journal"} and fake.position == 0
    assert t.j.active_trade(SYM) is None and [x["status"] for x in t.j.trades()] == ["closed"]


async def test_a_session_close_during_a_leg_join_closes_one_trade_with_both_legs():
    d, t, fake = desk()
    await d.propose_group([prop(1.04, tranches=SPLIT)])
    fake.price = 1.045
    await d.poll()                                               # leg 1 opens the trade
    tid = t.j.active_trade(SYM)["id"]
    fake.price = 1.075                                           # leg 2 fills: an add to that trade
    res = await close_while_the_poll_reads_history(d, t, fake)
    assert res["ok"] and fake.position == 0 and t.j.active_trade(SYM) is None
    assert [x["id"] for x in t.j.trades()] == [tid] and t.j.get_trade(tid)["status"] == "closed"
    assert {x["trade_id"] for x in d._rows("1=1")} == {tid}


async def test_a_fill_resolved_elsewhere_before_the_lock_opens_no_trade():
    d, t, fake = desk()
    eid = (await d.propose_group([prop(1.04)]))["placed"][0]["entry_id"]
    fake.price = 1.06
    real = fake.fetch_order

    async def resolved_meanwhile(i, s, params=None):
        o = await real(i, s, params=params)
        if i == row(d, eid)["order_id"]:                         # another process journals it first
            d.j.db.execute("UPDATE entries SET status='error', reason='elsewhere' WHERE id=?", (eid,))
            d.j.db.commit()
        return o
    fake.fetch_order = resolved_meanwhile
    assert [e["event"] for e in await d.poll()] == ["resolved_elsewhere"]
    assert t.j.trades() == [] and row(d, eid)["reason"] == "elsewhere"


# ------------------------------------------------------------ item 4: compare-and-set
async def test_a_cancel_never_relabels_a_row_resolved_while_it_ran():
    d, t, fake = desk()
    r = await d.propose_group([prop(1.04)])
    eid = r["placed"][0]["entry_id"]
    real = fake.cancel_order

    async def expired_meanwhile(i, s, params=None):
        d.j.db.execute("UPDATE entries SET status='expired', reason='GTD (other process)' WHERE id=?", (eid,))
        d.j.db.commit()
        return await real(i, s, params=params)
    fake.cancel_order = expired_meanwhile
    await d.cancel_group(r["group_id"], "read changed")
    assert row(d, eid)["status"] == "expired" and row(d, eid)["reason"] == "GTD (other process)"
