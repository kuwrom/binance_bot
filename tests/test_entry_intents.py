"""Write-ahead intents for limit entries (entry_intents.py): the row and its client id exist before the order is
sent, ambiguous sends are resolved by client id and never resent, and the sweep finds orders the journal lacks."""

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from shortbot import clientid, entry_intents
from shortbot.config import RiskLimits
from shortbot.entries import SCHEMA, EntryDesk
from shortbot.journal import Journal
from shortbot.risk_budget import gather_inputs
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks
from tests.test_entries import favorable, prop
from tests.test_guardian import kinds

SPLIT = [{"limit_price": 1.04}, {"limit_price": 1.07}]


def on_send(fake, kind, nth, arm=None, die=False):
    """Interfere with the nth send of `kind` ("limit" / "STOP_MARKET") only: arm one of the fake's fault counters
    (ack_lost, ack_lost_unsent, ack_lost_503, unavailable_503) for it, or let it run and then cancel the caller
    (a crash or a cancelled MCP request mid-send)."""
    send, n = fake.create_order, {"n": 0}

    async def create_order(s, type_, side, amount, price=None, params=None):
        n["n"] += type_ == kind
        if type_ == kind and n["n"] == nth:
            if arm:
                setattr(fake, arm, getattr(fake, arm) + 1)
            if die:
                await send(s, type_, side, amount, price, params)
                raise asyncio.CancelledError()
        return await send(s, type_, side, amount, price, params)
    fake.create_order = create_order


def desk(limits=None):
    fake = FakeCcxt(price=1.035)             # a slow approach (the fast-approach rule stays quiet)
    t = Trader(FakeEx(fake, limits), Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
    return EntryDesk(t, regime_fn=favorable), t, fake


def age(d, eid, seconds=entry_intents.GRACE_S + 1):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    d.j.db.execute("UPDATE entries SET created_at=? WHERE id=?", (ts, eid))
    d.j.db.commit()


def row(d, eid):
    return d._rows("id=?", (eid,))[0]


def alerts(t, kind):
    return [a for a in t.j.alerts(200) if a["kind"] == kind]


def open_limits(fake):
    return [i for i, o in fake.limits.items() if o["status"] == "open"]


async def test_intent_row_is_journalled_before_the_order_is_sent():
    d, t, fake = desk()
    seen, send = [], fake.create_order

    async def spy(s, type_, side, amount, price=None, params=None):
        if type_ == "limit":
            seen.append((d._rows("1=1"), (params or {}).get("clientOrderId")))
        return await send(s, type_, side, amount, price, params)
    fake.create_order = spy
    r = await d.propose_group([prop(1.04)])
    ((rows, cid),) = seen
    assert [x["status"] for x in rows] == ["placing"] and rows[0]["client_id"] == cid and clientid.ours(cid)
    e = row(d, r["placed"][0]["entry_id"])
    assert r["ok"] and e["status"] == "pending" and e["prearm_id"] in fake.prearms
    assert fake.algo[e["prearm_id"]]["cid"] == e["prearm_cid"] and fake.limits[e["order_id"]]["cid"] == cid


async def test_a_failing_read_after_validation_leaves_nothing_on_the_exchange():
    d, t, fake = desk()

    async def down(*a, **k):
        raise TimeoutError("ohlcv")
    fake.fetch_ohlcv = down
    r = await d.propose_group([prop(1.04)])
    assert not r["ok"] and fake.limits == {} and fake.prearms == {}


async def test_a_definite_refusal_is_journalled_as_rejected():
    d, t, fake = desk()
    fake.clock_s = 120   # the exchange clock runs ahead: a 660 s GTD is under its 600 s minimum (-5040)
    r = await d.propose_group([prop(1.04)], ttl_hours=0)
    assert not r["ok"] and "-5040" in r["rejected"][0]["error"] and r["unknown"] == []
    assert [x["status"] for x in d._rows("1=1")] == ["rejected"] and fake.prearms == {}


@pytest.mark.parametrize("fault", ["ack_lost", "ack_lost_503"])
async def test_unknown_send_is_never_resent_and_is_adopted_when_it_rests(fault):
    d, t, fake = desk()
    setattr(fake, fault, 1)                                            # it ran, but the answer was lost
    r = await d.propose_group([prop(1.04)])
    assert not r["ok"] and r["rejected"] == [] and "Don't re-propose" in r["unknown"][0]["hint"]
    eid = r["unknown"][0]["entry_id"]
    assert row(d, eid)["status"] == "unknown" and fake.prearms == {}   # no pre-arm on an unconfirmed limit
    assert d.pending() == [] and [x["status"] for x in d.pending(all_orders=True)] == ["unknown"]
    await entry_intents.reconcile(d)
    assert row(d, eid)["status"] == "pending" and row(d, eid)["order_id"] in open_limits(fake)
    assert len([o for o in fake.orders if o[0] == "limit"]) == 1     # never resent
    fake.fill_limit(row(d, eid)["order_id"])
    assert [e["event"] for e in await d.poll()] == ["filled"] and list(fake.stops.values()) == [1.10]


@pytest.mark.parametrize("fault", ["ack_lost_unsent", "unavailable_503"])
async def test_unknown_send_that_never_reached_the_book_is_rejected_after_the_grace(fault):
    d, t, fake = desk()
    setattr(fake, fault, 1)
    eid = (await d.propose_group([prop(1.04)]))["unknown"][0]["entry_id"]
    await entry_intents.reconcile(d)
    assert row(d, eid)["status"] == "unknown"                          # may still be in flight
    age(d, eid)
    await entry_intents.reconcile(d)
    assert row(d, eid)["status"] == "rejected" and fake.limits == {} and alerts(t, "entry_intent")


async def test_split_leg_with_a_lost_answer_is_rolled_back_with_its_sibling():
    d, t, fake = desk()
    on_send(fake, "limit", 2, arm="ack_lost")      # the far leg acks; the near leg runs, its answer is lost
    r = await d.propose_group([prop(1.04, tranches=SPLIT)])
    assert not r["ok"] and "rolled back" in r["rejected"][0]["error"] and r["unknown"] == []
    far, near = d._rows("1=1")
    assert far["status"] == "cancelled" and near["status"] == "unknown" and near["abandon"] == 1
    assert len(open_limits(fake)) == 1                                  # the near leg rests, tracked
    await entry_intents.reconcile(d)
    assert row(d, near["id"])["status"] == "cancelled" and open_limits(fake) == [] and fake.prearms == {}


@pytest.mark.parametrize("filled", [False, True])
async def test_a_placing_call_that_died_mid_send_is_withdrawn_but_a_fill_is_kept(filled):
    d, t, fake = desk()
    on_send(fake, "limit", 1, die=True)
    with pytest.raises(asyncio.CancelledError):
        await d.propose_group([prop(1.04)])
    (e,) = d._rows("1=1")
    await entry_intents.reconcile(d)
    assert row(d, e["id"])["status"] == "placing"                       # its own call may still be waiting
    age(d, e["id"])
    (oid,) = open_limits(fake)
    if filled:
        fake.fill_limit(oid)
    await entry_intents.reconcile(d)
    if filled:                                                           # never strip a fill: adopt and protect it
        assert row(d, e["id"])["status"] == "pending" and alerts(t, "entry_intent")
        assert [x["event"] for x in await d.poll()] == ["filled"] and list(fake.stops.values()) == [1.10]
    else:
        assert row(d, e["id"])["status"] == "cancelled" and fake.limits[oid]["status"] == "canceled"


async def test_lost_prearm_answer_is_matched_by_client_id_and_dies_with_its_entry():
    d, t, fake = desk()
    on_send(fake, "STOP_MARKET", 1, arm="ack_lost")
    r = await d.propose_group([prop(1.04)])
    e = row(d, r["placed"][0]["entry_id"])
    assert r["ok"] and not r["placed"][0]["prearmed_stop"] and e["prearm_id"] is None and len(fake.prearms) == 1
    age(d, e["id"])
    await entry_intents.reconcile(d)
    assert row(d, e["id"])["prearm_id"] in fake.prearms
    await d.cancel_group(e["group_id"], "level gone")
    assert fake.prearms == {}


async def test_a_dead_entrys_unresolved_prearm_is_cancelled_and_an_unmatched_one_alerted_once():
    d, t, fake = desk()
    on_send(fake, "STOP_MARKET", 1, arm="ack_lost")          # it rests, its id unknown
    on_send(fake, "STOP_MARKET", 2, arm="ack_lost_unsent")   # it never ran
    r = await d.propose_group([prop(1.04), prop(1.06, stop=1.12)])
    a, b = (row(d, x["entry_id"]) for x in r["placed"])
    await d.cancel_group(a["group_id"], "level gone")         # prearm ids unknown: the resting stop survives
    assert len(fake.prearms) == 1
    age(d, a["id"]), age(d, b["id"])
    await entry_intents.reconcile(d)
    await entry_intents.reconcile(d)
    assert fake.prearms == {} and row(d, a["id"])["prearm_id"]
    assert row(d, b["id"])["prearm_id"] is None and len(alerts(t, "prearm_unresolved")) == 1   # never guessed


async def test_an_external_order_is_recorded_once_counted_as_exposure_and_dropped_when_it_leaves():
    d, t, fake = desk(RiskLimits(max_total_notional=150))
    o = await fake.create_order(SYM, "limit", "sell", 50, 1.2, {"timeInForce": "GTC"})   # placed outside shortbot
    await entry_intents.reconcile(d)
    d._swept_at = 0
    await entry_intents.reconcile(d)
    (x,) = d._rows("status='external'")
    assert x["symbol"] == SYM and x["amount"] == 50 and len(alerts(t, "external_order")) == 1 and d.pending() == []
    assert "NOT placed by shortbot" in d.pending(all_orders=True)[0]["hint"]
    inp = await gather_inputs(t, "favorable")
    assert abs(inp.open_risks[SYM] - 60 * t.ex.limits.max_stop_distance_pct / 100) < 1e-9   # no stop: worst case
    r = await d.propose_group([prop(1.05)])                   # 60 USDT external + ~98 new > 150
    assert not r["ok"] and "total notional" in str(r["rejected"])
    await fake.cancel_order(o["id"], SYM)
    d._swept_at = 0
    await entry_intents.reconcile(d)
    assert row(d, x["id"])["status"] == "external_gone" and SYM not in (await gather_inputs(t, "favorable")).open_risks


async def test_a_filled_external_order_explains_its_position_until_it_closes():
    d, t, fake = desk()
    o = await fake.create_order(SYM, "limit", "sell", 50, 1.2, {"timeInForce": "GTC"})   # placed outside shortbot
    await entry_intents.reconcile(d)
    (x,) = d._rows("status='external'")
    fake.fill_limit(o["id"])
    d._swept_at = 0
    await entry_intents.reconcile(d)
    assert row(d, x["id"])["status"] == "external_filled" and entry_intents.explained_symbols(d) == {SYM}
    assert d.pending(all_orders=True) == []                    # off the book: the position counts, not the order
    fake.trigger_stop()                                         # the position is gone
    d._swept_at = 0
    await entry_intents.reconcile(d)
    assert row(d, x["id"])["status"] == "external_gone" and entry_intents.explained_symbols(d) == set()


async def test_an_external_order_binance_has_forgotten_stops_counting():
    d, t, fake = desk()
    o = await fake.create_order(SYM, "limit", "sell", 50, 1.2, {"timeInForce": "GTC"})
    await entry_intents.reconcile(d)
    await fake.cancel_order(o["id"], SYM)
    del fake.limits[o["id"]]           # unfilled and cancelled > 3 days ago: Query Order answers -2013
    d._swept_at = 0
    await entry_intents.reconcile(d)
    assert [x["status"] for x in d._rows("1=1")] == ["external_gone"] and not entry_intents.external_orders(t.j)


@pytest.mark.parametrize("filled", [False, True])
async def test_sweep_cancels_a_late_order_of_a_rejected_intent_unless_it_filled(filled):
    d, t, fake = desk()
    fake.ack_lost = 1
    eid = (await d.propose_group([prop(1.04)]))["unknown"][0]["entry_id"]
    assert d._settle(eid, "unknown", status="rejected")             # the engine queued it past the grace
    (oid,) = open_limits(fake)
    if filled:
        fake.fill_limit(oid, qty=fake.limits[oid]["amount"] / 2)
    await entry_intents.reconcile(d)
    if filled:
        assert row(d, eid)["status"] == "pending" and row(d, eid)["order_id"] == oid
        assert [x["event"] for x in await d.poll()] == ["filled"] and list(fake.stops.values()) == [1.10]
    else:
        assert fake.limits[oid]["status"] == "canceled" and row(d, eid)["status"] == "rejected"


async def test_a_fresh_rejected_send_still_explains_a_position_for_a_while():
    d, t, fake = desk()
    fake.ack_lost = 1
    eid = (await d.propose_group([prop(1.04)]))["unknown"][0]["entry_id"]
    assert d._settle(eid, "unknown", status="rejected")
    assert entry_intents.explained_symbols(d) == {SYM}             # it may still have run late
    age(d, eid, entry_intents.LATE_S + 1)
    assert entry_intents.explained_symbols(d) == set()


async def test_withdraw_finds_a_row_without_an_order_id_by_its_client_id():
    from shortbot.entries_cancel import withdraw
    d, t, fake = desk()
    fake.ack_lost = 1                                               # it rests; the row has no order id
    a = row(d, (await d.propose_group([prop(1.04)]))["unknown"][0]["entry_id"])
    assert await withdraw(t, a) == "unfilled" and open_limits(fake) == []      # found by client id, cancelled
    fake.add_symbol("ZK/USDT:USDT", 1.035)
    fake.ack_lost_unsent = 1                                        # it never ran
    b = row(d, (await d.propose_group([prop(1.06, stop=1.12, symbol="ZK/USDT:USDT")]))["unknown"][0]["entry_id"])
    assert await withdraw(t, b) == "unknown"                     # may still be in flight
    age(d, b["id"])
    assert await withdraw(t, row(d, b["id"])) == "unfilled"      # never reached the book


async def test_placed_orders_are_reported_even_if_the_decision_log_or_alert_fails():
    d, t, fake = desk()

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    async def down(*a):
        raise RuntimeError("telegram down")
    t.j.log_decision = locked
    t.set_alert(down)
    r = await d.propose_group([prop(1.04)])
    assert r["ok"] and len(r["placed"]) == 1 and r["rejected"] == [] and len(r["warnings"]) == 2


async def test_a_possibly_live_order_counts_as_exposure_for_the_next_group():
    d, t, fake = desk(RiskLimits(max_total_notional=150))   # room for ONE ~98 USDT order
    fake.ack_lost = 1
    assert (await d.propose_group([prop(1.04)]))["unknown"]
    r = await d.propose_group([prop(1.05)])
    assert not r["ok"] and "total notional" in str(r["rejected"])


async def test_the_brief_lists_every_order_on_the_book_with_its_hint():
    from types import SimpleNamespace

    from shortbot.cli import brief
    d, t, fake = desk()
    fake.add_symbol("ZK/USDT:USDT", 1.035)
    await d.propose_group([prop(1.06, stop=1.12, symbol="ZK/USDT:USDT")])
    fake.ack_lost = 1
    await d.propose_group([prop(1.04)])
    text = await brief(SimpleNamespace(settings=t.ex.settings, journal=t.j, ex=t.ex, trader=t))
    assert "entry orders on the book (2)" in text and "pending" in text and "Don't re-propose" in text


async def test_guardian_runs_the_reconciler_as_its_own_protective_job():
    from tests.test_guardian import guardian_with
    g = guardian_with(FakeCcxt(price=1.035))
    g.entries._regime_fn = favorable
    assert "entry_reconcile" in [job.name for job in g._lanes()["protective"].jobs]
    g.ex.fake.ack_lost = 1
    eid = (await g.entries.propose_group([prop(1.04)]))["unknown"][0]["entry_id"]
    await g.reconcile_entries()
    assert row(g.entries, eid)["status"] == "pending" and alerts(g.app.trader, "entry_intent")


async def test_a_kill_switch_trip_mid_placement_leaves_no_row():
    from shortbot import killswitch as ks
    d, t, fake = desk()

    async def trip_then_pass(ex, symbol, ticker):   # tripped after propose_entries' own gate let the group in
        await ks.trip(t.j, t.ex.settings, ks.REDUCING, "guardian", "daily loss")
        return {"vetoes": [], "regime": "favorable", "features": {}}
    t.prechecks = trip_then_pass
    r = await d.propose_group([prop(1.04)])
    assert not r["ok"] and "REDUCING" in str(r["rejected"]) and d._rows("1=1") == [] and fake.limits == {}


async def test_halted_sweep_withdraws_our_unknown_send_but_never_an_external_order():
    import time

    from shortbot import killswitch as ks
    from tests.test_guardian import SETTINGS, guardian_with
    fake = FakeCcxt(price=1.035)
    g = guardian_with(fake)
    g.j.set(ks.HEARTBEAT_KEY, str(time.time()))
    g.entries._regime_fn = favorable
    fake.add_symbol("ZK/USDT:USDT", 1.0)
    ext = await fake.create_order("ZK/USDT:USDT", "limit", "sell", 50, 1.2, {"timeInForce": "GTC"})   # the owner's
    await entry_intents.reconcile(g.entries)
    fake.ack_lost = 1
    eid = (await g.entries.propose_group([prop(1.04)]))["unknown"][0]["entry_id"]   # rests, no order id yet
    await ks.trip(g.j, SETTINGS, ks.HALTED, "agent (MCP)", "halt")
    await g.check_entries()
    await g.check_entries()
    assert row(g.entries, eid)["status"] == "cancelled" and open_limits(fake) == [ext["id"]]
    assert [k for k in kinds(g) if k.startswith("external_while_halted")] == [f"external_while_halted:{ext['id']}"]


def test_an_existing_journal_gains_the_intent_columns_and_keeps_its_rows(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.db.executescript(SCHEMA)                                       # the original table, without later columns
    j.db.execute("INSERT INTO entries (group_id, symbol, status, created_at, expires_ms, order_id) "
                 "VALUES ('g', ?, 'pending', '2026-10-01T00:00:00+00:00', 0, '7')", (SYM,))
    j.db.commit()
    t = Trader(FakeEx(FakeCcxt()), j, prechecks=ok_prechecks, budget=neutral_budget)
    EntryDesk(t)
    (r,) = EntryDesk(t)._rows("1=1")                                 # migrating twice is a no-op
    assert r["order_id"] == "7" and r["client_id"] is None and r["prearm_cid"] is None and r["abandon"] == 0
    (sql,) = j.db.execute("SELECT sql FROM sqlite_master WHERE name='entries_client_id'").fetchone()
    assert "UNIQUE" in sql
