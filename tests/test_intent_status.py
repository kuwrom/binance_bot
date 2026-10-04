"""An intent's status is exact by what it sent (#36 gateway step 3, GATEWAY.md §4.5), on the real order flows against
the strict fake: refused (nothing sent), failed (a send, then nothing left changed), partial (part done), unknown (a
send of unknown outcome), each order's outcome linked by its client id (intent_orders)."""

import pytest

from shortbot import audit, gateway, intents
from shortbot.gateway import LocalGateway
from shortbot.intents import Intent
from tests import fake_errors as err
from tests.fakes import SYM, FakeCcxt
from tests.test_add_rules import ROOMY, desk
from tests.test_close_scope import close_fills_short
from tests.test_guardian import guardian_with, open_trade
from tests.test_trading import open_default, trader


async def submit(t, monkeypatch, flow, kind="t_flow"):
    """`flow` (a coroutine function of no arguments) as one intent: (its answer or the error it raised, its row)."""
    async def run(ctx):
        return await flow()
    monkeypatch.setitem(gateway.HANDLERS, kind, run)
    it = Intent(kind, {}, SYM)
    try:
        res = await LocalGateway(lambda: t.j).submit(it)
    except Exception as e:  # noqa: BLE001 - the row says what it was
        res = e
    await t.j.flush()
    return res, intents.get(t.j, it.id)


def outcomes(j, row):
    return [(o["kind"], o["outcome"]) for o in intents.orders(j, row["id"])]


def blind_lookups(fake):
    """Query Order times out (the order itself is untouched) until `on` is cleared: a send stays in doubt."""
    real, state = fake.fetch_order, {"on": True}

    async def fetch_order(i, s, params=None):
        if state["on"]:
            raise err.status_unknown()
        return await real(i, s, params)
    fake.fetch_order = fetch_order
    return state


def unsent_sell(fake):
    """The next market SELL answers -1007 without running ("unknown" cuts both ways)."""
    real, state = fake.create_order, {"on": True}

    async def create(s, type_, side, amount, price=None, params=None):
        if state["on"] and type_ == "market" and side == "sell":
            state["on"] = False
            fake.orders.append((type_, side, amount, params))
            raise err.status_unknown()
        return await real(s, type_, side, amount, price, params)
    fake.create_order = create


# ------------------------------------------------------------ refused: nothing was sent
async def test_a_refusal_before_any_send_is_refused(monkeypatch):
    t, fake = trader()
    assert (await open_default(t))["ok"]
    sent = len(fake.orders)
    res, row = await submit(t, monkeypatch, lambda: t.close(SYM, "x", fraction=2))
    assert not res["ok"] and (row["status"], row["sent"], row["error"]) == ("refused", 0, None)
    res, row = await submit(t, monkeypatch, lambda: t.close("NOPE/USDT:USDT", "x"))
    assert row["status"] == "refused" and len(fake.orders) == sent and outcomes(t.j, row) == []


# ------------------------------------------------------------ failed: sent, nothing left changed
async def test_a_stop_swap_failing_after_its_bridge_is_failed_with_each_orders_outcome(monkeypatch):
    t, fake = trader()
    assert (await open_default(t))["ok"]
    fake.price, fake.fail_stop = 0.95, 1          # the new closePosition stop is refused; the old one is restored
    res, row = await submit(t, monkeypatch, lambda: t.move_stop(SYM, 1.02, "tighten"))
    assert not res["ok"] and "restored" in res["error"] and list(fake.stops.values()) == [1.05]
    assert (row["status"], row["sent"]) == ("failed", 1)
    assert outcomes(t.j, row) == [("bridge", "placed"), ("stop", "rejected"), ("stop", "placed")]


async def test_an_entry_the_exchange_refuses_is_failed_with_its_typed_error_on_the_order(monkeypatch):
    t, fake = trader()
    fake.refuse = [err.margin_insufficient()]
    res, row = await submit(t, monkeypatch, lambda: open_default(t))
    assert not res["ok"] and "refused" in res["error"] and fake.position == 0
    assert (row["status"], row["sent"]) == ("failed", 1) and outcomes(t.j, row) == [("open", "rejected")]


# ------------------------------------------------------------ partial: part done
async def test_a_close_that_fills_short_is_partial(monkeypatch):
    t, fake = trader()
    assert (await open_default(t))["ok"]
    close_fills_short(fake, 0.5)
    res, row = await submit(t, monkeypatch, lambda: t.close(SYM, "target"))
    assert not res["ok"] and res["remaining"] == 50 and (row["status"], row["sent"]) == ("partial", 1)
    assert outcomes(t.j, row) == [("close", "part_filled")]


async def test_a_plan_step_whose_breakeven_swap_fails_after_its_target_close_is_partial():
    fake = FakeCcxt()
    g = guardian_with(fake)
    tid = await open_trade(g)
    plan = g.j.get_trade(tid)["plan"]
    g.j.update_trade(tid, plan={**plan, "targets": [{"price": 0.95, "fraction": 0.5}],
                                "auto": {**plan["auto"], "targets": True, "breakeven": True}})
    fake.price, fake.fail_stop = 0.93, 1          # T1 by market, then the breakeven stop is refused
    with audit.acting("guardian:positions"):
        await g.check_positions()
    await g.j.flush()
    (row,) = g.j._rows("SELECT * FROM intents WHERE kind='plan_step'")
    res = intents.get(g.j, row["id"])["result"]
    assert [(d["kind"], d["result"]["ok"]) for d in res["done"]] == [("close", True), ("move_stop", False)]
    assert res["ok"] is False and (row["status"], row["sent"]) == ("partial", 1) and fake.position == 50
    assert list(fake.stops.values()) == [1.05]    # the old stop was restored


# ------------------------------------------------------------ a send its flow found by client id is settled
async def test_a_bridge_found_by_its_client_id_after_a_lost_answer_is_settled_placed(monkeypatch):
    t, fake = trader()
    assert (await open_default(t))["ok"]
    fake.price, fake.ack_lost = 0.95, 1           # the bridge rests, its answer is lost: found by its client id
    res, row = await submit(t, monkeypatch, lambda: t.move_stop(SYM, 1.02, "tighten"))
    assert res["ok"] and row["status"] == "done" and list(fake.stops.values()) == [1.02]
    assert outcomes(t.j, row) == [("bridge", "placed"), ("stop", "placed")] and intents.unsettled(t.j) == []


# ------------------------------------------------------------ unknown: a send whose outcome stays unknown
async def test_a_partial_close_whose_answer_and_lookups_are_lost_is_unknown(monkeypatch):
    t, fake = trader()
    assert (await open_default(t))["ok"]
    fake.ack_lost = 1                             # it runs, then answers -1007
    blind_lookups(fake)
    res, row = await submit(t, monkeypatch, lambda: t.close(SYM, "half", fraction=0.5))
    assert res["unknown"] and fake.position == 50 and (row["status"], row["sent"]) == ("unknown", None)
    assert outcomes(t.j, row) == [("close", None)]


async def test_an_add_that_answers_unknown_after_its_stop_tighten_is_unknown(monkeypatch):
    ops, t, fake = await desk(**ROOMY)
    fake.price = 0.9
    unsent_sell(fake)
    res, row = await submit(t, monkeypatch, lambda: ops.add_to_winner(SYM, 44, 0.95, "press"))
    assert res["unknown"] and fake.position == 100 and list(fake.stops.values()) == [0.95]
    assert row["status"] == "unknown" and outcomes(t.j, row)[-1] == ("add", None)
    assert ("stop", "placed") in outcomes(t.j, row)


async def test_a_whole_close_whose_tries_all_time_out_is_unknown_not_failed(monkeypatch):
    t, fake = trader()
    assert (await open_default(t))["ok"]
    fake.fail_close = 3                           # each send times out before it runs
    res, row = await submit(t, monkeypatch, lambda: t.close(SYM, "out"))
    assert isinstance(res, RuntimeError) and fake.position == 100
    assert (row["status"], row["sent"]) == ("unknown", None) and outcomes(t.j, row) == [("close", None)] * 3
    assert intents.get(t.j, row["id"])["error"]["type"] == "Unexpected"


@pytest.mark.parametrize("raised", [False, True])
async def test_read_errors_before_any_send_are_refused_not_unknown(monkeypatch, raised):
    t, fake = trader()

    async def flow():
        if raised:
            await fake.fetch_order("1", SYM)      # -2013: a read, nothing was sent
        return {"ok": False, "error": "nothing to do"}
    res, row = await submit(t, monkeypatch, flow)
    assert (row["status"], row["sent"]) == ("refused", 0)
    if raised:
        assert intents.get(t.j, row["id"])["error"]["type"] == "Gone"
