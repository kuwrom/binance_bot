"""Client order ids on the market entry, add, close and stop paths (#9): an ambiguous answer (timeout, 5xx, -1007) is
resolved by the client id and the position, never by a blind resend. Review repros: r1 (partial close executed twice,
entry stop resent into -4130) and r2 (full close raised although flat)."""

import ccxt
import pytest

from shortbot import clientid
from tests import fake_errors as err
from tests.fakes import SYM
from tests.test_entry_intents import on_send
from tests.test_positioning import ops_with_open_trade
from tests.test_trading import open_default, trader

RAN = ["ack_lost", "ack_lost_503"]                  # it ran, then the answer was lost
UNSENT = ["ack_lost_unsent", "unavailable_503"]     # "unknown", and it did NOT run


def sends(fake, type_, side=None):
    return [o for o in fake.orders if o[0] == type_ and (side is None or o[1] == side)]


def lookups(fake):
    seen, real = [], fake.fetch_order

    async def spy(i, s, params=None):
        seen.append(dict(params or {}))
        return await real(i, s, params)
    fake.fetch_order = spy
    return seen


def kinds(t):
    return [a["kind"] for a in t.j.alerts(200)]


def test_codes_stop_refusals_and_refs():
    assert clientid.code(err.one_close_position_stop()) == -4130 and clientid.code(err.unavailable("order", 1)) is None
    for e in (err.one_close_position_stop(), err.switch_algo()):   # bare ExchangeError: unknown, unless a stop refusal
        assert clientid.send_unknown(e) and not clientid.send_unknown(e, clientid.STOP_REFUSALS)
    assert not clientid.send_unknown(err.would_trigger(), clientid.STOP_REFUSALS)
    assert clientid.send_unknown(err.status_unknown(), clientid.STOP_REFUSALS)
    assert clientid.tag("1000PEPE/USDT:USDT") == "1000PEPE" and clientid.tag("币安人生/USDT:USDT") == ""
    assert clientid.VALID.match(clientid.new("stop", clientid.tag("币安人生/USDT:USDT")))


# ---------------------------------------------------------------- (a) market entry
@pytest.mark.parametrize("fault", RAN)
async def test_entry_whose_answer_was_lost_is_found_by_its_client_id_and_protected(fault):
    t, fake = trader()
    seen, send = [], fake.create_order

    async def spy(s, type_, side, amount, price=None, params=None):
        if type_ == "market":   # the id is journalled before the send
            seen.append(((params or {}).get("clientOrderId"), t.j.trades()[0]["entry_features"].get("client_id")))
        return await send(s, type_, side, amount, price, params)
    fake.create_order = spy
    on_send(fake, "market", 1, arm=fault)
    r = await open_default(t)
    ((cid, journalled),) = seen
    assert r["ok"] and cid == journalled and cid.startswith(f"sb-o{r['trade_id']}-")
    assert fake.position == 100 and list(fake.stops.values()) == [1.05] and len(sends(fake, "market")) == 1
    assert t.j.get_trade(r["trade_id"])["status"] == "open"


@pytest.mark.parametrize("fault", UNSENT)
async def test_entry_of_unknown_outcome_is_never_aborted_or_resent_and_a_late_fill_gets_its_stop(fault):
    t, fake = trader()
    on_send(fake, "market", 1, arm=fault)
    r = await open_default(t)
    assert not r["ok"] and r["unknown"] and fake.position == 0 and fake.stops == {}
    assert t.j.get_trade(r["trade_id"])["status"] == "opening" and "entry_unknown" in kinds(t)
    again = await open_default(t)                                   # never re-sent while it may still fill
    assert not again["ok"] and "unresolved" in again["error"] and len(sends(fake, "market")) == 1
    await fake.create_order(SYM, "market", "sell", 100, None, {"clientOrderId": r["client_id"]})   # it fills late
    res = await t.ensure_stop(SYM, 10)                              # the guardian: no 'entry in progress' skip
    assert res["action"] == "stop_placed" and list(fake.stops.values()) == [1.05]


async def test_a_refused_entry_is_aborted_without_a_lookup():
    t, fake = trader()
    seen, send = lookups(fake), fake.create_order

    async def refuse(s, type_, side, amount, price=None, params=None):
        if type_ == "market":
            raise err.margin_insufficient()
        return await send(s, type_, side, amount, price, params)
    fake.create_order = refuse
    r = await open_default(t)
    assert not r["ok"] and "-2019" in r["error"] and seen == [] and t.j.trades()[0]["status"] == "aborted"


async def test_a_failing_position_read_after_the_fill_still_places_the_stop():
    t, fake = trader()
    real = fake.fetch_positions

    async def down():
        if fake.position:
            raise ccxt.RequestTimeout("binanceusdm GET https://demo-fapi.binance.com/fapi/v3/positionRisk timed out")
        return await real()
    fake.fetch_positions = down
    r = await open_default(t)
    assert r["ok"] and r["warnings"] and list(fake.stops.values()) == [1.05] and fake.position == 100
    tr = t.j.get_trade(r["trade_id"])
    assert tr["status"] == "open" and tr["amount"] == 100 and tr["entry_price"] == 1.0   # from the order's own fill


# ---------------------------------------------------------------- (c) stops
@pytest.mark.parametrize("fault", RAN)
async def test_entry_stop_whose_answer_was_lost_keeps_the_position(fault):   # review r1
    t, fake = trader()
    on_send(fake, "STOP_MARKET", 1, arm=fault)
    r = await open_default(t)
    assert r["ok"] and fake.position == 100 and list(fake.stops.values()) == [1.05]
    assert len(sends(fake, "STOP_MARKET")) == 1 and r["stop_order_id"] in fake.stops


@pytest.mark.parametrize("fault", UNSENT)
async def test_a_stop_that_never_landed_is_sent_again_under_the_same_client_id(fault):
    t, fake = trader()
    on_send(fake, "STOP_MARKET", 1, arm=fault)
    r = await open_default(t)
    a, b = (o[3]["clientOrderId"] for o in sends(fake, "STOP_MARKET"))
    assert r["ok"] and a == b and a.startswith("sb-sPUMP-") and list(fake.stops.values()) == [1.05]


async def test_a_stop_that_lands_after_its_lookup_is_found_when_the_resend_is_refused():
    t, fake = trader()
    send, n = fake.create_order, {"n": 0}

    async def late(s, type_, side, amount, price=None, params=None):
        if type_ == "STOP_MARKET":
            n["n"] += 1
            if n["n"] == 1:
                raise err.status_unknown()                          # in flight: not there at the first lookup
            if n["n"] == 2:
                await send(s, type_, side, amount, price, params)  # ...and it lands just before the resend
        return await send(s, type_, side, amount, price, params)
    fake.create_order = late
    r = await open_default(t)
    assert r["ok"] and fake.position == 100 and list(fake.stops.values()) == [1.05]


@pytest.mark.parametrize("code", ["-4130", "-2021", "-4045"])
async def test_definite_stop_refusals_raise_at_once_without_a_lookup(code):
    t, fake = trader()
    assert (await open_default(t))["ok"]
    seen, n, px = lookups(fake), len(sends(fake, "STOP_MARKET")), 1.04   # -4130: the trade's stop rests
    if code == "-2021":
        fake.stops.clear()
        px = 0.99
    fake.fail_stop = int(code == "-4045")
    with pytest.raises(ccxt.BaseError, match=code):
        await t._place_stop(SYM, px, tries=3)
    assert seen == [] and len(sends(fake, "STOP_MARKET")) == n + 1


# ---------------------------------------------------------------- (b) closes, (d) what filled
async def test_partial_close_whose_answer_was_lost_is_not_sent_twice():   # review r1
    t, fake = trader()
    await open_default(t)
    fake.ack_lost = 1
    r = await t.close(SYM, "take half", fraction=0.5, expect_contracts=100)
    assert r["ok"] and r["closed_amount"] == 50 and r["remaining"] == 50 and fake.position == 50
    assert len(sends(fake, "market", "buy")) == 1


@pytest.mark.parametrize("fault", RAN)
async def test_full_close_whose_answer_was_lost_is_finalized(fault):   # review r2
    t, fake = trader()
    tid = (await open_default(t))["trade_id"]
    setattr(fake, fault, 1)
    r = await t.close(SYM, "exit", "lesson")
    assert r["ok"] and fake.position == 0 and fake.stops == {} and len(sends(fake, "market", "buy")) == 1
    assert t.j.get_trade(tid)["status"] == "closed"


@pytest.mark.parametrize("fault", UNSENT)
async def test_full_close_that_never_ran_is_sent_again_under_a_new_id(fault):
    t, fake = trader()
    tid = (await open_default(t))["trade_id"]
    setattr(fake, fault, 1)
    r = await t.close(SYM, "exit", "lesson")
    a, b = (o[3]["clientOrderId"] for o in sends(fake, "market", "buy"))
    assert r["ok"] and a != b and fake.position == 0 and t.j.get_trade(tid)["status"] == "closed"


@pytest.mark.parametrize("fault", UNSENT)
async def test_partial_close_of_unknown_outcome_is_never_resent(fault):
    t, fake = trader()
    await open_default(t)
    setattr(fake, fault, 1)
    r = await t.close(SYM, "half", fraction=0.5, expect_contracts=100)
    assert not r["ok"] and r["unknown"] and "re-read" in r["error"] and fake.position == 100
    assert len(sends(fake, "market", "buy")) == 1 and list(fake.stops.values()) == [1.05]


async def test_partial_close_reports_what_filled_not_what_was_asked():
    t, fake = trader()
    await open_default(t)
    send = fake.create_order

    async def short_fill(s, type_, side, amount, price=None, params=None):
        if type_ == "market" and side == "buy":   # the book ran out: half fills, the rest expires
            o = await send(s, type_, side, amount / 2, price, params)
            return {**o, "amount": amount, "remaining": amount - o["filled"], "status": "expired"}
        return await send(s, type_, side, amount, price, params)
    fake.create_order = short_fill
    r = await t.close(SYM, "half", fraction=0.5, expect_contracts=100)
    assert r["ok"] and r["closed_amount"] == 25 and r["remaining"] == 75 == fake.position
    assert "25 of 50" in r["warning"]


# ---------------------------------------------------------------- add_to_winner's market add
@pytest.mark.parametrize("fault", RAN)
async def test_an_add_whose_answer_was_lost_is_found_and_journalled(fault):
    ops, t, fake = await ops_with_open_trade()
    fake.price, n = 0.9, len(sends(fake, "market"))
    on_send(fake, "market", 1, arm=fault)
    r = await ops.add_to_winner(SYM, 9, 0.95, "trend continuing")    # 10 more: within the 100 USDT cap
    assert r["ok"] and r["added"] == 10 and fake.position == 110 and len(sends(fake, "market")) == n + 1
    assert t.j.active_trade(SYM)["amount"] == 110


@pytest.mark.parametrize("fault", UNSENT)
async def test_an_add_of_unknown_outcome_is_never_resent(fault):
    ops, t, fake = await ops_with_open_trade()
    fake.price, n = 0.9, len(sends(fake, "market"))
    on_send(fake, "market", 1, arm=fault)
    r = await ops.add_to_winner(SYM, 9, 0.95, "trend continuing")
    assert not r["ok"] and r["unknown"] and fake.position == 100 and len(sends(fake, "market")) == n + 1
    assert "add_unknown" in kinds(t) and t.j.decisions(SYM)[0]["action"] == "add"
    assert list(fake.stops.values()) == [0.95]                       # the tightened stop stays
