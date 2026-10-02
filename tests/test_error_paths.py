"""Each error type the order path tells apart (errors.py, #37), raised by the fake as Binance/ccxt raise it, gets its
own handling: a definite refusal journals a refusal that names it, an ambiguous answer is resolved by the client id and
never re-sent, Gone is done, and nothing unexpected passes without a human being told."""

import ccxt

from shortbot import entry_intents, errors
from tests import fake_errors as err
from tests.fakes import SYM
from tests.test_entries import desk, prop
from tests.test_trading import open_default, trader

TRIG = {"trigger": True}


def kinds(t):
    return [a["kind"] for a in t.j.alerts(200)]


def sells(fake):
    return [o for o in fake.orders if o[0] == "market" and o[1] == "sell"]


# ---------------------------------------------------------------- definite refusals: named, journalled, never unknown
async def test_insufficient_margin_refuses_the_entry_and_says_so():
    t, fake = trader()
    fake.refuse = [err.margin_insufficient()]
    r = await open_default(t)
    assert not r["ok"] and "InsufficientMargin -2019" in r["error"] and not r.get("unknown")
    assert fake.position == 0 and len(sells(fake)) == 1 and "entry_unknown" not in kinds(t)
    assert not t.j.open_trades(include_opening=True)   # aborted, not left 'opening'


async def test_market_closed_refuses_the_entry_and_says_so():
    t, fake = trader()
    fake.refuse = [err.symbol_closed()]
    r = await open_default(t)
    assert not r["ok"] and "MarketClosed -4141" in r["error"] and not t.j.open_trades(include_opening=True)


async def test_any_other_refusal_is_refused_and_says_so():
    t, fake = trader()
    fake.refuse = [err.reduce_only_mode()]   # -4400: the account takes reduce-only orders only
    r = await open_default(t)
    assert not r["ok"] and "MarketClosed -4400" in r["error"]
    fake.refuse = [err.duplicate_client_id()]
    r = await open_default(t)
    assert not r["ok"] and "Refused -4116" in r["error"] and len(sells(fake)) == 2 and fake.position == 0


async def test_an_outcome_unknown_send_that_ran_is_found_by_its_id():
    t, fake = trader()
    fake.ack_lost = 1   # -1007 after the sell ran
    r = await open_default(t)
    assert r["ok"] and len(sells(fake)) == 1 and fake.position == 100


async def test_precision_on_a_limit_entry_is_a_rejected_row_not_an_unknown_one():
    d, t, fake = desk(price=1.0)
    fake.refuse = [err.bad_precision()]
    r = await d.propose_group([prop(1.04)])
    assert not r["placed"] and not r["unknown"] and "Precision -1111" in r["rejected"][0]["error"]
    assert [x["status"] for x in d._rows()] == ["rejected"]


# ---------------------------------------------------------------- ambiguous answers: looked up, never re-sent
async def test_rate_limited_entry_is_looked_up_by_its_id_and_never_resent():
    t, fake = trader()
    fake.refuse = [err.too_many_requests()]
    r = await open_default(t)
    assert r["unknown"] and len(sells(fake)) == 1 and "entry_unknown" in kinds(t)
    assert t.j.get_trade(r["trade_id"])["status"] == "opening"


async def test_rate_limited_limit_entry_rests_as_unknown():
    d, t, fake = desk(price=1.0)
    fake.refuse = [err.rate_limited()]
    r = await d.propose_group([prop(1.04)])
    assert r["unknown"] and [x["status"] for x in d._rows()] == ["unknown"]


async def test_clock_drift_on_a_send_resyncs_then_looks_it_up():
    t, fake = trader()
    fake.refuse = [err.clock_drift()]
    r = await open_default(t)
    assert r["unknown"] and t.ex.resyncs == 1 and len(sells(fake)) == 1


async def test_an_unmapped_code_is_unexpected_and_still_never_resent():
    t, fake = trader()
    fake.refuse = [err.unmapped()]
    r = await open_default(t)
    assert r["unknown"] and len(sells(fake)) == 1


# ---------------------------------------------------------------- StopExists / Gone
async def test_a_closeposition_stop_already_resting_counts_as_covered():
    t, fake = trader()
    assert (await open_default(t))["ok"]
    real = fake.fetch_open_orders

    async def blind(s, params=None):   # the remainder's cover can't list the stops: it places one, -4130 answers
        if params:
            raise err.status_unknown()
        return await real(s, params)
    fake.fetch_open_orders = blind
    async with t.symbol_lock(SYM):
        assert await t._cover(SYM, t.j.active_trade(SYM), 100) == "closePosition stop already in place"
    assert "NAKED_POSITION" not in kinds(t) and len(fake.stops) == 1


async def test_a_prearm_already_gone_needs_no_alert():
    d, t, fake = desk(price=1.0)
    eid = (await d.propose_group([prop(1.04)]))["placed"][0]["entry_id"]
    row = d._rows("id=?", (eid,))[0]
    fake._retire(row["prearm_id"], "CANCELED")   # gone already (-2011 on the cancel)
    assert await d._cancel_entry(row, "cancelled", "test") == "unfilled"
    assert "prearm_left" not in kinds(t)


async def test_a_prearm_whose_cancel_fails_is_told_and_left_for_the_orphan_sweep():
    d, t, fake = desk(price=1.0)
    eid = (await d.propose_group([prop(1.04)]))["placed"][0]["entry_id"]
    real = fake.cancel_order

    async def flaky(i, s, params=None):
        if params:
            raise err.status_unknown()
        return await real(i, s, params)
    fake.cancel_order = flaky
    assert await d._cancel_entry(d._rows("id=?", (eid,))[0], "cancelled", "test") == "unfilled"
    assert d._rows("id=?", (eid,))[0]["status"] == "cancelled" and len(fake.prearms) == 1
    assert "prearm_left" in kinds(t)


# ---------------------------------------------------------------- carrying on: transient quiet, unexpected told once
async def test_reconcile_tells_a_human_once_about_an_unexpected_failure(monkeypatch):
    d, t, fake = desk(price=1.0)
    monkeypatch.setattr(errors, "_reported", {})
    eid = (await d.propose_group([prop(1.04)]))["placed"][0]["entry_id"]
    d._update(eid, status="unknown")
    for e in (ccxt.RequestTimeout("slow"), err.rate_limited()):
        monkeypatch.setattr(entry_intents, "resolve", _raising(e))
        await entry_intents.reconcile(d)
    assert "unexpected_error" not in kinds(t)   # transient: retried next tick, quietly
    monkeypatch.setattr(entry_intents, "resolve", _raising(KeyError("order_id")))
    for _ in range(3):
        await entry_intents.reconcile(d)
    assert kinds(t).count("unexpected_error") == 1


def _raising(e):
    async def f(*a, **k):
        raise e
    return f
