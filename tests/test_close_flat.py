"""A close that meets a position already flat (#37): the stop (or a target) fired between our read and our reduce-only
order, and Binance answers -2022 ReduceOnly rejected. Done, not failed: each of the seven callers of close_market
treats it as the flat path does - no retry loop, no "market close failed", no NAKED alert - and the trade is journalled
closed (at once, or by reconcile like any exchange-side close)."""

from shortbot import errors, reconcile, stops
from shortbot.guardian_positions import MAX_STOP_RESTORE_FAILS
from shortbot.positioning import PositionOps
from tests.fakes import SYM
from tests.test_guardian import guardian_with
from tests.test_trading import open_default, trader

FAILED = {"NAKED_POSITION", "close_incomplete", "widen_undone", "position_check_error"}
TRIG = {"trigger": True}


def kinds(t):
    return [a["kind"] for a in t.j.alerts(200)]


def stop_fires_first(fake):
    """The exchange closes the short just before our first reduce-only market buy arrives: Binance answers -2022."""
    real, fired = fake.create_order, []

    async def racing(s, type_, side, amount, price=None, params=None):
        if type_ == "market" and side == "buy" and (params or {}).get("reduceOnly") and not fired:
            fired.append(s)
            fake.trigger_stop(s)
        return await real(s, type_, side, amount, price, params)
    fake.create_order = racing
    return fired


def closes(fake):
    return [o for o in fake.orders if o[0] == "market" and o[1] == "buy" and o[3].get("reduceOnly")]


async def opened():
    t, fake = trader()
    assert (await open_default(t))["ok"]
    return t, fake


async def finalized(t):
    """The trade is journalled closed now, or by the guardian's reconcile (its exit is on the exchange)."""
    await reconcile.reconcile(t)
    return not t.j.open_trades(include_opening=True)


async def test_market_close_returns_flat_on_reduce_only_rejected_without_retrying():
    t, fake = await opened()
    stop_fires_first(fake)
    async with t.symbol_lock(SYM):
        o = await t.close_market(SYM, 100)
    assert o["flat"] and o["filled"] == 0 and o["id"] is None and "ReduceOnlyRejected -2022" in o["rejected"]
    assert len(closes(fake)) == 1 and fake.position == 0


async def test_full_close():
    t, fake = await opened()
    stop_fires_first(fake)
    r = await t.close(SYM, "thesis broken", "lesson")
    assert r["ok"] and r["closed_amount"] == 0 and "flat before" in r["note"] and len(closes(fake)) == 1
    assert not t.j.open_trades(include_opening=True) and not FAILED & set(kinds(t))
    assert not fake.stops and not fake.prearms


async def test_partial_close():
    t, fake = await opened()
    stop_fires_first(fake)
    r = await t.close(SYM, "take half", "lesson", fraction=0.5)
    assert r["ok"] and r["flat"] and r["closed_amount"] == 0 and r["remaining"] == 0 and len(closes(fake)) == 1
    assert not FAILED & set(kinds(t)) and "partial_close" not in [d["action"] for d in t.j.decisions(SYM)]
    assert await finalized(t)


async def test_widen_cut():
    t, fake = await opened()
    stop_fires_first(fake)
    r = await PositionOps(t).widen_stop(SYM, 1.10, "stop inside noise")
    assert r["ok"] and r["closed"] and r["size_cut"] == 0 and len(closes(fake)) == 1
    assert not FAILED & set(kinds(t)) and not fake.stops   # the new stop on a flat symbol is stripped
    assert "widen_stop" not in [d["action"] for d in t.j.decisions(SYM)]
    assert await finalized(t)


async def test_stop_crossed_during_a_move():
    """Price moved past the new level after move_stop's check: the bridge answers -2021, stops._crossed closes."""
    t, fake = await opened()
    fake.price = 1.03
    stop_fires_first(fake)
    async with t.symbol_lock(SYM):
        r = await stops.set_stop(t, SYM, 1.02, 100, await t.stop_orders(SYM, close_position_only=True))
    assert r["ok"] and r["closed"] and len(closes(fake)) == 1 and not FAILED & set(kinds(t))
    assert await finalized(t)


async def test_guardian_forced_close():
    t, fake = await opened()
    g = guardian_with(fake)
    g.app.trader = t
    sent = []

    async def send(kind, msg, symbol=None, **kw):
        sent.append((kind, msg))
    g.n.send = send

    async def broken(*a, **k):
        raise RuntimeError("ensure_stop failed")
    t.ensure_stop = broken
    for i in list(fake.stops):
        await fake.cancel_order(i, SYM, params=TRIG)
    g.stop_fails[SYM] = MAX_STOP_RESTORE_FAILS - 1
    stop_fires_first(fake)
    assert await g._restore_stop(SYM)
    assert len(closes(fake)) == 1 and fake.position == 0
    msg = dict(sent)["forced_close"]
    assert "already gone" in msg and "CLOSED." not in msg
    assert await finalized(t)


async def test_close_past_the_stop():
    t, fake = await opened()
    for i in list(fake.stops):
        await fake.cancel_order(i, SYM, params=TRIG)
    fake.price = 1.06
    stop_fires_first(fake)
    r = await t.ensure_stop(SYM, 5.0)
    assert r["action"] == "closed" and len(closes(fake)) == 1 and not FAILED & set(kinds(t))
    assert not t.j.open_trades(include_opening=True)


async def test_open_short_abort_when_the_stop_cannot_be_placed():
    t, fake = trader()
    fake.fail_stop = 5
    stop_fires_first(fake)
    r = await open_default(t)
    assert not r["ok"] and "position closed" in r["error"] and len(closes(fake)) == 1
    assert "NAKED_POSITION" not in kinds(t) and "stop_failed" in kinds(t)
    assert not t.j.open_trades(include_opening=True)


async def test_not_flat_after_minus_2022_is_still_a_refusal():
    """-2022 with the position still there (a reduce-only cap, an odd answer): the refusal stands, retried, then
    raised as today."""
    t, fake = await opened()
    fake.refuse = [errors_reduce_only() for _ in range(3)]
    async with t.symbol_lock(SYM):
        try:
            await t.close_market(SYM, 100)
            raise AssertionError("expected the refusal")
        except RuntimeError as e:
            assert "market close failed after 3 tries" in str(e)
    assert fake.position == 100


def errors_reduce_only():
    from tests import fake_errors as err
    e = err.reduce_only_rejected()
    assert errors.classify(e) is errors.ReduceOnlyRejected
    return e


# ---------------------------------------------------------------- -4509: a closePosition stop placed while flat
def flat_before_the_stop(fake):
    """The position closes (a fill, a target) just before our closePosition stop arrives: Binance answers -4509 (demo
    probe #55; the fake still places one while flat, so the answer is injected)."""
    from tests import fake_errors as err
    real = fake.create_order

    async def racing(s, type_, side, amount, price=None, params=None):
        if (params or {}).get("closePosition") and fake.sym[s]["qty"]:
            fake.trigger_stop(s)
            raise err.close_position_flat()
        return await real(s, type_, side, amount, price, params)
    fake.create_order = racing


async def test_a_guardian_stop_restore_racing_a_fill_to_flat_is_done_not_failed():
    t, fake = await opened()
    for i in list(fake.stops):
        await fake.cancel_order(i, SYM, params=TRIG)
    flat_before_the_stop(fake)
    assert await t.ensure_stop(SYM, 5.0) is None   # nothing left to protect: no stop_missing, no failed restore
    assert fake.position == 0 and not fake.stops and not FAILED & set(kinds(t))


async def test_a_remainders_cover_racing_to_flat_raises_no_naked_alert():
    t, fake = await opened()
    for i in list(fake.stops):
        await fake.cancel_order(i, SYM, params=TRIG)
    flat_before_the_stop(fake)
    async with t.symbol_lock(SYM):
        got = await t._cover(SYM, t.j.active_trade(SYM), 100)
    assert got.startswith("flat") and "NAKED_POSITION" not in kinds(t)


async def test_a_fills_stop_meeting_a_flat_position_is_no_instant_naked_alert():
    from tests.test_entries import desk, prop
    from tests import fake_errors as err
    d, t, fake = desk(price=1.0)
    eid = (await d.propose_group([prop(1.04)]))["placed"][0]["entry_id"]
    real = fake.create_order

    async def racing(s, type_, side, amount, price=None, params=None):   # its pre-arm fires first, then -4509
        if (params or {}).get("closePosition") and fake.prearms:
            fake._trigger(next(iter(fake.prearms)))
            raise err.close_position_flat()
        return await real(s, type_, side, amount, price, params)
    fake.create_order = racing
    fake.fill_limit(d._rows("id=?", (eid,))[0]["order_id"])
    await d.poll()   # the stop meets -4509: filled and closed again, journalled as a round trip in this same poll
    assert "entry_poll_failing" not in kinds(t) and fake.position == 0
    assert d._rows("id=?", (eid,))[0]["status"] == "filled" and not t.j.open_trades(include_opening=True)


async def test_minus_2022_after_a_lost_answer_is_the_unknown_flat_answer():
    """Our first close ran but its answer was lost and could not be settled yet; the re-send meets -2022: flat, maybe
    by our own send, so the unknown-outcome flat answer (not "nothing of ours filled")."""
    t, fake = await opened()
    fake.ack_lost = 1

    async def still_in_doubt(symbol, doubt):
        return None
    t._close_outcome = still_in_doubt
    async with t.symbol_lock(SYM):
        o = await t.close_market(SYM, 100)
    assert o["flat"] and o["filled"] is None and "rejected" not in o and len(closes(fake)) == 2
