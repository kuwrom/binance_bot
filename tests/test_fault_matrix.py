"""#46 plumbing gate, the fault matrix (part 1: market orders and stops; part 2, test_fault_matrix_entries.py: limit
entries, their cancel, plan targets). Every order operation meets each answer Binance gives when it can't confirm:
-1007 after the request ran or without it, HTTP 503 "Unknown error" (ran) and "Service Unavailable." (not), -1008
(not ran) and -1021 after it ran (tests/fake_faults.py). Each operation runs as an intent (gateway.py), as the desk runs
it, and each cell asserts:
- no re-send under a new client id while the outcome is unknown (the by-design exceptions: a WHOLE close is sent again
  once its first send is found not to have run and the position is still there, as reduce-only can't buy back more than
  the position; a target written off 'lost' after GRACE_S is placed again)
- exactly one closePosition stop while the position is open (none once flat), no bridge left behind
- the final state settles within the grace: the intent is final, the journal matches the exchange
- the expected intent_resolved alert (only an intent that ENDED unknown is resolved: one whose own flow settled the
  send by its client id ends done or failed and needs none)

Cells (op x fault). "covered": the primitive's handling is already pinned by that test (without the intent, the
resolver or these invariants, which this matrix adds); "new": first test of that cell; "skip": not a Binance answer.
| op           | 1007_ran              | 1007_unsent          | 503_unknown           | 503_unavailable       | 1008 | 1021_after              |
| open         | order_ids::entry_whose_answer_was_lost | order_ids::entry_of_unknown_outcome | order_ids (ack_lost_503) | order_ids (unavailable_503) | new | new (error_paths::clock_drift_on_a_send: not ran) |
| stop place   | order_ids::entry_stop_whose_answer | order_ids::a_stop_that_never_landed | order_ids (ack_lost_503) | order_ids (unavailable_503) | skip | new |
| stop cancel  | new                   | new                  | new                   | new                   | new  | stop_swap::widen_stop_cancel_drift |
| bridge       | new                   | new                  | new                   | new                   | skip | new |
| close        | order_ids::full_close_whose_answer | order_ids::full_close_that_never_ran | order_ids (ack_lost_503) | order_ids (unavailable_503) | skip | new |
| partial      | order_ids::partial_close_whose_answer | resolver::unknown_close_that_never_ran | new | order_ids::partial_close_of_unknown (unavailable_503) | skip | new |
skip = -1008 on a reduce-only or closePosition order: Binance exempts those from -1008 (general-info doc, HTTP 503 case
C), so the fake never answers it.
"""

import time

import pytest

from shortbot import audit, clientid, intents, reconcile, resolver
from shortbot.journal import Journal
from shortbot.trading import Trader
from tests.fake_faults import FAULTS, RAN, FaultyCcxt
from tests.fakes import SYM, FakeEx, neutral_budget, ok_prechecks
from tests.test_guardian import guardian_with, open_trade
from tests.test_intent_status import submit
from tests.test_trading import open_default

LATER = resolver.GRACE_S + 1
EXEMPT = "Binance exempts reduce-only and closePosition orders from -1008 (general-info doc, HTTP 503 case C)"


def faults(reduce_only: bool = False) -> list:
    return [pytest.param(f, marks=pytest.mark.skip(reason=EXEMPT)) if reduce_only and f == "1008" else f
            for f in FAULTS]


def trader() -> tuple[Trader, FaultyCcxt]:
    fake = FaultyCcxt()
    return Trader(FakeEx(fake), Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget), fake


def cids(fake, kind: str) -> list[str]:
    """Distinct client ids of the create_order calls of this kind ("open", "stop", "bridge", "close"...), in order."""
    out: list[str] = []
    for *_, p in fake.orders:
        c = (p or {}).get("clientOrderId")
        if c and clientid.kind_of(c) == kind and c not in out:
            out.append(c)
    return out


def close_stops(fake, s: str = SYM) -> list[float]:
    return [t for i, t in fake.stops.items() if fake._algo_sym(i) == s]


def bridges(fake, s: str = SYM) -> list[str]:
    return [i for i in fake.prearms if fake._algo_sym(i) == s and str(fake.algo[i].get("cid") or "").startswith("sb-b")]


def resolutions(j) -> list[str]:
    """'<was> -> <status>' of every intent_resolved alert."""
    return [a["message"].split(") ", 1)[1].split(":", 1)[0] for a in j.alerts(200) if a["kind"] == "intent_resolved"]


def resynced(t) -> bool:
    """The clock was resynced (after a -1021)."""
    return getattr(t.ex, "resyncs", 0) > 0


def final(j, row: dict) -> str:
    return intents.get(j, row["id"])["status"]


async def settle(t: Trader, monkeypatch) -> None:
    """What the guardian's jobs do once the grace has passed: the resolver, then reconcile ('opening' rows)."""
    monkeypatch.setattr(reconcile, "OPENING_GRACE_S", -1)
    await resolver.resolve(t, now=time.time() + LATER)
    await reconcile.reconcile(t)
    await t.j.flush()


# ------------------------------------------------------------ open: the market SELL
@pytest.mark.parametrize("fault", faults())
async def test_open(fault, monkeypatch):
    t, fake = trader()
    fake.arm("create_order", fault, "open")
    res, row = await submit(t, monkeypatch, lambda: open_default(t))
    ran = fault in RAN
    assert row["status"] == ("done" if ran else "unknown") and bool(res["ok"]) is ran, res
    if not ran:   # may still fill: the symbol refuses a new entry, nothing is sent again
        again = await open_default(t)
        assert not again["ok"] and "unresolved" in again["error"] and fake.position == 0 and fake.stops == {}
    await settle(t, monkeypatch)
    assert len(cids(fake, "open")) == 1 and fake.position == (100 if ran else 0)
    assert close_stops(fake) == ([1.05] if ran else []) and fake.prearms == {}
    assert final(t.j, row) == ("done" if ran else "refused")
    assert resolutions(t.j) == ([] if ran else ["unknown -> refused"])
    assert t.j.get_trade(res["trade_id"])["status"] == ("open" if ran else "aborted")
    assert resynced(t) is (fault == "1021_after") and fake.fired == [("create_order", "open", fault)]


# ------------------------------------------------------------ stop place: the guardian restores a missing stop
@pytest.mark.parametrize("fault", faults(reduce_only=True))
async def test_stop_place(fault):
    fake = FaultyCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    fake.stops.clear()                                          # the stop is gone (cancelled by hand, say)
    fake.arm("create_order", fault, "stop")
    with audit.acting("guardian:positions"):
        await g.check_positions()
    await g.j.flush()
    (row,) = g.j._rows("SELECT * FROM intents WHERE kind='restore_stop'")
    assert row["status"] == "done" and close_stops(fake) == [1.05] and fake.position == 100
    stop_sends = [o for o in fake.orders if (o[3] or {}).get("closePosition")]
    assert len(cids(fake, "stop")) == 2   # the entry's, and the restore's: re-sent under that same id if it didn't run
    assert len(stop_sends) == 2 + (fault not in RAN)
    assert [o["outcome"] for o in intents.orders(g.j, row["id"])] == ["placed"] and resolutions(g.j) == []
    assert "NAKED_POSITION" not in [a["kind"] for a in g.j.alerts(100)]
    assert fake.fired == [("create_order", "stop", fault)]


# ------------------------------------------------------------ stop swap: the old stop's cancel, and the bridge
@pytest.mark.parametrize("fault", faults())
async def test_stop_swap_cancel(fault, monkeypatch):
    t, fake = trader()
    assert (await open_default(t))["ok"]
    fake.price = 0.95
    fake.arm("cancel_order", fault, "stop")
    res, row = await submit(t, monkeypatch, lambda: t.move_stop(SYM, 1.02, "trail"))
    assert res["ok"], res
    assert close_stops(fake) == [1.02] and bridges(fake) == [] and fake.prearms == {} and fake.position == 100
    # not ran: the old stop still rests, so the new one is refused (-4130, definite: nothing ran) and placed again in
    # the second round under a new id; the entry's stop is the first id
    assert row["status"] == "done" and len(cids(fake, "stop")) == 2 + (fault not in RAN)
    assert len(cids(fake, "bridge")) == 1
    assert t.j.active_trade(SYM)["stop_price"] == 1.02 and resolutions(t.j) == []
    assert resynced(t) is (fault == "1021_after") and fake.fired == [("cancel_order", "stop", fault)]


@pytest.mark.parametrize("fault", faults(reduce_only=True))
async def test_stop_swap_bridge(fault, monkeypatch):
    t, fake = trader()
    assert (await open_default(t))["ok"]
    fake.price = 0.95
    fake.arm("create_order", fault, "bridge")
    res, row = await submit(t, monkeypatch, lambda: t.move_stop(SYM, 1.02, "trail"))
    ran = fault in RAN
    assert bool(res["ok"]) is ran, res
    assert bridges(fake) == [] and fake.prearms == {} and fake.position == 100 and len(cids(fake, "bridge")) == 1
    if ran:   # found by its client id: the swap went on
        assert close_stops(fake) == [1.02] and row["status"] == "done"
    else:     # never placed: nothing was cancelled, the old stop stays; a bridge is never sent twice
        assert close_stops(fake) == [1.05] and "bridge stop was not placed" in res["error"]
        assert row["status"] == "failed" and t.j.active_trade(SYM)["stop_price"] == 1.05
    assert resolutions(t.j) == [] and fake.fired == [("create_order", "bridge", fault)]


# ------------------------------------------------------------ closes
@pytest.mark.parametrize("fault", faults(reduce_only=True))
async def test_close(fault, monkeypatch):
    t, fake = trader()
    tid = (await open_default(t))["trade_id"]
    fake.arm("create_order", fault, "close")
    res, row = await submit(t, monkeypatch, lambda: t.close(SYM, "exit", "lesson"))
    assert res["ok"] and row["status"] == "done", res
    await settle(t, monkeypatch)
    assert fake.position == 0 and close_stops(fake) == [] and bridges(fake) == []
    # ran: found by its id, never re-sent; not ran: the whole close is sent again (reduce-only: never more than held)
    assert len(cids(fake, "close")) == (1 if fault in RAN else 2)
    assert sum(f["amount"] for f in fake.fills if f["side"] == "buy") == 100
    tr = t.j.get_trade(tid)
    assert tr["status"] == "closed" and tr["pnl"] is not None and resolutions(t.j) == []
    assert fake.fired == [("create_order", "close", fault)]


@pytest.mark.parametrize("fault", faults(reduce_only=True))
async def test_partial_close(fault, monkeypatch):
    t, fake = trader()
    assert (await open_default(t))["ok"]
    fake.arm("create_order", fault, "close")
    res, row = await submit(t, monkeypatch, lambda: t.close(SYM, "half", fraction=0.5, expect_contracts=100))
    ran = fault in RAN
    assert row["status"] == ("done" if ran else "unknown") and bool(res.get("unknown")) is not ran, res
    await settle(t, monkeypatch)
    assert len(cids(fake, "close")) == 1 and fake.position == (50 if ran else 100)   # never re-sent
    assert close_stops(fake) == [1.05] and bridges(fake) == []
    assert final(t.j, row) == ("done" if ran else "refused")
    assert resolutions(t.j) == ([] if ran else ["unknown -> refused"])
    assert t.j.active_trade(SYM)["status"] == "open" and fake.fired == [("create_order", "close", fault)]


# ------------------------------------------------------------ the reads that settle a send: Query Order, userTrades
@pytest.mark.parametrize("fault", faults())
async def test_a_failed_lookup_of_an_unknown_open_is_looked_up_again_never_resent(fault, monkeypatch):
    t, fake = trader()
    fake.arm("create_order", "1007_ran", "open")
    fake.arm("fetch_order", fault, "open")                      # its first lookup by client id fails too
    res, row = await submit(t, monkeypatch, lambda: open_default(t))
    assert res["ok"] and row["status"] == "done" and len(cids(fake, "open")) == 1 and fake.position == 100
    assert close_stops(fake) == [1.05] and [f[2] for f in fake.fired] == ["1007_ran", fault]


@pytest.mark.parametrize("fault", faults())
async def test_a_failed_lookup_leaves_the_intent_unknown_until_the_next_run(fault, monkeypatch):
    t, fake = trader()
    assert (await open_default(t))["ok"]
    fake.arm("create_order", "1007_unsent", "close")
    _, row = await submit(t, monkeypatch, lambda: t.close(SYM, "half", fraction=0.5, expect_contracts=100))
    n = len(fake.orders)
    fake.arm("fetch_order", fault, "close")                     # the resolver's lookup fails: it decides nothing
    assert await resolver.resolve(t, now=time.time() + LATER) == [] and final(t.j, row) == "unknown"
    assert "unexpected_error" not in [a["kind"] for a in t.j.alerts(100)]   # transient: logged, retried next run
    (ev,) = await resolver.resolve(t, now=time.time() + LATER)
    assert ev["status"] == "refused" and len(fake.orders) == n and fake.position == 100


@pytest.mark.parametrize("fault", faults())
async def test_a_failed_history_read_closes_the_trade_once_on_the_next_pass(fault):
    t, fake = trader()
    tid = (await open_default(t))["trade_id"]
    fake.price = 1.06                                           # stopped out on the exchange
    fake.arm("fetch_my_trades", fault)
    assert await reconcile.reconcile(t) == [] and t.j.get_trade(tid)["status"] == "open"
    assert "reconcile_error" in [a["kind"] for a in t.j.alerts(100)]
    (done,) = await reconcile.reconcile(t)
    assert done["trade_id"] == tid and t.j.get_trade(tid)["status"] == "closed" and await reconcile.reconcile(t) == []
    assert len([d for d in t.j.decisions(SYM, 100) if d["action"] == "trade_closed"]) == 1
