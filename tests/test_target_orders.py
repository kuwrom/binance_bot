"""Resting plan targets through the guardian (#65): placed as reduce-only limits, filled on a wick at their price and
journalled, the market fallback kept for a target crossed without its fill, restarts and lost answers."""

import json
from types import SimpleNamespace

from shortbot import fill_quality, targets, targets_sync
from shortbot.guardian import Guardian
from shortbot.journal import Journal
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks

PLAN = {"targets": [{"price": 0.95, "fraction": 0.5}, {"price": 0.9, "fraction": 0.5}], "auto": {"targets": True}}


def guardian(fake, db):
    ex = FakeEx(fake)
    tr = Trader(ex, Journal(db), prechecks=ok_prechecks, budget=neutral_budget)
    g = Guardian(SimpleNamespace(settings=ex.settings, journal=tr.j, ex=ex, trader=tr))

    async def quiet(*a, **k):
        return None
    g._squeeze_and_funding = g._news = quiet
    return g


async def desk(tmp_path, plan=PLAN):
    fake = FakeCcxt()
    g = guardian(fake, tmp_path / "shortbot.db")
    r = await g.app.trader.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100, plan=plan)
    assert r["ok"] and r["amount"] == 100, r
    return g, fake, r["trade_id"]


def placed_by_the_guardian(monkeypatch):
    """No target rests at entry (as when its sync there fails, #71): the guardian's pass makes the first placement."""
    async def none(*a, **k):
        return None
    monkeypatch.setattr("shortbot.targets_sync.at_entry", none)


def resting(fake):
    return sorted(((o["price"], o["amount"] - o["filled"], o["cid"]) for o in fake.limits.values()
                   if o["status"] == "open" and o["side"] == "buy"), reverse=True)


def market_buys(fake):
    return [o[2] for o in fake.orders if o[0] == "market" and o[1] == "buy"]


def target_creates(fake):
    return [o for o in fake.orders if o[0] == "limit" and o[1] == "buy"]


async def test_the_guardian_rests_each_automatic_target_as_a_reduce_only_gtc_limit(tmp_path):
    g, fake, tid = await desk(tmp_path)
    await g.check_positions()
    assert [(p, q) for p, q, _ in resting(fake)] == [(0.95, 50), (0.9, 50)]
    assert all(c.startswith(f"sb-t{tid}-") for _, _, c in resting(fake))
    assert all(o[3]["reduceOnly"] and o[3]["timeInForce"] == "GTC" for o in target_creates(fake))
    assert [r["status"] for r in targets.rows(g.j)] == ["resting", "resting"]
    n = len(fake.orders)
    await g.check_positions()                                   # in sync: nothing sent
    assert len(fake.orders) == n


async def test_alert_only_targets_never_rest(tmp_path):
    g, fake, _ = await desk(tmp_path, {**PLAN, "auto": {"targets": False}})
    await g.check_positions()
    assert resting(fake) == [] and target_creates(fake) == []


async def test_a_wick_fills_the_target_at_its_price_and_each_fill_is_journalled(tmp_path):
    g, fake, tid = await desk(tmp_path)
    await g.check_positions()
    fake.price = 0.94                                           # the wick between two passes
    fake.price = 0.97
    assert fake.position == 50
    await g.check_positions()
    await fill_quality.drain(5)
    assert market_buys(fake) == []                              # no market close: the limit did it
    t = g.j.get_trade(tid)
    assert t["targets_hit"] == [0.95]
    (fill,) = [json.loads(d["snapshot"]) for d in g.j.decisions(SYM) if d["action"] == "target_fill"]
    row = next(r for r in targets.rows(g.j) if r["target"] == 0.95)
    assert fill["px"] == 0.95 and fill["amount"] == 50 and fill["order"] == row["order_id"] and fill["ms"]
    assert row["status"] == "filled" and row["filled"] == 50 and row["fill_ms"]
    assert [h["status"] for h in json.loads(row["history"])] == ["placing", "resting", "filled"]
    assert [(f["type"], f["px"]) for f in t["fill_refs"]["fills"] if f["leg"] == "exit"] == [("target", 0.95)]
    assert [(p, q) for p, q, _ in resting(fake)] == [(0.9, 50)]


async def test_a_target_whose_order_left_the_book_falls_back_to_market_when_crossed(tmp_path):
    g, fake, tid = await desk(tmp_path)
    await g.check_positions()
    t1 = next(i for i, o in fake.limits.items() if o["price"] == 0.95)
    fake.limits[t1]["status"] = "canceled"                      # Binance took it away (competition, a sweep...)
    fake.price = 0.949
    await g.check_positions()
    assert market_buys(fake) == [50] and fake.position == 50 and g.j.get_trade(tid)["targets_hit"] == [0.95]
    row = next(r for r in targets.rows(g.j) if r["target"] == 0.95)
    assert row["status"] == "cancelled" and json.loads(row["history"])[-1]["ex"] == "canceled"
    assert [(p, q) for p, q, _ in resting(fake)] == [(0.9, 50)]


async def test_a_partly_filled_target_closes_only_the_rest_at_market(tmp_path):
    g, fake, tid = await desk(tmp_path)
    await g.check_positions()
    t1 = next(i for i, o in fake.limits.items() if o["price"] == 0.95)
    fake.fill_limit(t1, 20)                                     # touched, 20 of 50 filled; mark sits at 0.95
    await g.check_positions()
    assert market_buys(fake) == [30] and fake.position == 50 and g.j.get_trade(tid)["targets_hit"] == [0.95]
    assert [(p, q) for p, q, _ in resting(fake)] == [(0.9, 50)]


async def test_the_last_target_flattens_and_the_trade_closes_as_a_target_exit(tmp_path):
    g, fake, tid = await desk(tmp_path)
    await g.check_positions()
    fake.price = 0.85
    assert fake.position == 0
    await g.check_positions()
    await fill_quality.drain(5)
    t = g.j.get_trade(tid)
    assert t["status"] == "closed" and t["exit_reason"].startswith("plan targets")
    assert sorted(t["targets_hit"]) == [0.9, 0.95]
    assert sorted((f["type"], f["px"]) for f in t["fill_refs"]["fills"] if f["leg"] == "exit") == [
        ("target", 0.9), ("target", 0.95)]
    assert targets.live(g.j) == [] and market_buys(fake) == []


async def test_flat_on_the_exchange_cancels_leftover_targets_whatever_binance_does(tmp_path):
    g, fake, tid = await desk(tmp_path)
    fake.ro_expire_on_flat = False                              # the open question: left resting
    await g.check_positions()
    fake.trigger_stop()                                         # stopped out on the exchange
    assert fake.position == 0 and len(resting(fake)) == 2
    await g.check_positions()
    assert resting(fake) == [] and targets.live(g.j) == []
    assert {r["status"] for r in targets.rows(g.j)} == {"cancelled"}
    assert all("flat" in r["why"] for r in targets.rows(g.j))


async def test_a_restart_adopts_the_book_and_replaces_a_target_it_has_no_row_for(tmp_path):
    g, fake, tid = await desk(tmp_path)
    await g.check_positions()
    g2 = guardian(fake, tmp_path / "shortbot.db")              # a new process on the same journal
    n = len(target_creates(fake))
    await g2.check_positions()
    assert len(target_creates(fake)) == n                       # adopted: nothing re-placed
    old = next(i for i, o in fake.limits.items() if o["price"] == 0.9)
    g2.j.db.execute("DELETE FROM target_orders WHERE target=0.9")   # a journal restored from before it
    g2.j.db.commit()
    await g2.check_positions()
    assert fake.limits[old]["status"] == "canceled" and len(target_creates(fake)) == n + 1
    assert [(p, q) for p, q, _ in resting(fake)] == [(0.95, 50), (0.9, 50)]
    assert "target_orphan_cancelled" in [a["kind"] for a in g2.j.alerts(50)]


async def test_a_lost_answer_is_resolved_by_client_id_never_resent_blind(tmp_path, monkeypatch):
    placed_by_the_guardian(monkeypatch)
    g, fake, tid = await desk(tmp_path)
    assert resting(fake) == []
    fake.ack_lost = 1                                           # the first target lands, its answer is lost
    await g.check_positions()
    assert [(p, q) for p, q, _ in resting(fake)] == [(0.95, 50), (0.9, 50)]
    assert [r["status"] for r in targets.rows(g.j)] == ["resting", "resting"]


async def test_a_send_that_never_landed_waits_out_the_grace_then_is_placed_again(tmp_path, monkeypatch):
    placed_by_the_guardian(monkeypatch)
    g, fake, tid = await desk(tmp_path)
    fake.ack_lost_unsent = 1                                    # -1007, and it did NOT run
    await g.check_positions()
    assert [p for p, _, _ in resting(fake)] == [0.9]
    assert [r["status"] for r in targets.rows(g.j)] == ["unknown", "resting"]
    await g.check_positions()                                   # still within the grace: never re-sent
    assert [p for p, _, _ in resting(fake)] == [0.9]
    monkeypatch.setattr("shortbot.targets_sync.GRACE_S", -1)
    await g.check_positions()
    assert [r["status"] for r in targets.rows(g.j)][:2] == ["lost", "resting"]
    assert [(p, q) for p, q, _ in resting(fake)] == [(0.95, 50), (0.9, 50)]


async def test_an_empty_positions_answer_alone_never_pulls_the_targets(tmp_path):
    g, fake, tid = await desk(tmp_path)
    await g.check_positions()
    tr, real, calls = g.app.trader, g.app.trader.raw_positions, []

    async def glitch():
        calls.append(1)
        return [] if len(calls) == 1 else await real()
    tr.raw_positions = glitch
    await g.check_positions()
    assert [(p, q) for p, q, _ in resting(fake)] == [(0.95, 50), (0.9, 50)]
    assert [r["status"] for r in targets.rows(g.j)] == ["resting", "resting"]


async def test_a_send_written_off_that_lands_late_is_tracked_again(tmp_path):
    g, fake, tid = await desk(tmp_path, {**PLAN, "auto": {"targets": False}})
    tr = g.app.trader
    rid = targets.add(g.j, tid, SYM, 0.95, 0.95, 50, f"sb-t{tid}-1a7e1a7e")
    targets.move(g.j, targets.rows(g.j, "id=?", (rid,))[0], "lost", "never reached the book")
    o = await fake.create_order(SYM, "limit", "buy", 50, 0.95, {"reduceOnly": True, "timeInForce": "GTC",
                                                               "clientOrderId": f"sb-t{tid}-1a7e1a7e"})
    await targets_sync.settle(tr, SYM)
    (row,) = targets.rows(g.j)
    assert row["status"] == "resting" and row["order_id"] == o["id"]
    await targets_sync.sync(tr, SYM)                           # alert-only plan: it doesn't belong on the book
    assert resting(fake) == [] and targets.rows(g.j)[0]["status"] == "cancelled"


async def test_positions_show_the_resting_targets(tmp_path):
    g, fake, tid = await desk(tmp_path)
    await g.check_positions()
    (p,) = await g.app.trader.positions()
    assert p["targets_resting"] == [{"target": 0.95, "qty": 50, "filled": 0, "status": "resting"},
                                    {"target": 0.9, "qty": 50, "filled": 0, "status": "resting"}]
