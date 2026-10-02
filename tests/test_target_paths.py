"""Resting plan targets stay consistent through everything that changes the position, the stop or the plan (#65):
partial and full closes, adds, a widen, update_plan, a stop swap's bridge, the kill switch, a re-entry, the entry
sweep."""

import json
from types import SimpleNamespace

import ccxt

from shortbot import entry_intents, killswitch as ks, killswitch_ops, mcp_trade, targets
from shortbot.config import RiskLimits
from shortbot.entries import EntryDesk
from shortbot.journal import Journal
from shortbot.positioning import PositionOps
from shortbot.trading import Trader
from tests.fakes import SYM, FakeEx, neutral_budget, ok_prechecks
from tests.test_entry_amend import desk as entry_desk, one
from tests.test_killswitch import prop
from tests.test_target_orders import PLAN, desk, market_buys, resting


def session(g, limits=None):
    """Another process (an MCP server) on the guardian's journal and account."""
    return Trader(FakeEx(g.app.ex.fake, limits), Journal(g.j.db.execute("PRAGMA database_list").fetchone()[2]),
                  prechecks=ok_prechecks, budget=neutral_budget)


def book(fake):
    return [(p, q) for p, q, _ in resting(fake)]


async def placed(tmp_path, plan=PLAN):
    g, fake, tid = await desk(tmp_path, plan)
    await g.check_positions()
    assert book(fake) == [(0.95, 50), (0.9, 50)] or not (plan.get("auto") or {}).get("targets")
    return g, fake, tid


async def test_a_partial_close_pulls_the_targets_first_and_resizes_them_after(tmp_path):
    g, fake, tid = await placed(tmp_path)
    r = await session(g).close(SYM, "trim", fraction=0.2, expect_contracts=100)
    assert r["ok"] and r["closed_amount"] == 20 and fake.position == 80 and market_buys(fake) == [20]
    assert book(fake) == [(0.95, 30), (0.9, 50)]                 # the plan's shares of what is left
    assert not [r for r in targets.rows(g.j) if r["status"] == "expired"]   # never competed with the close


async def test_a_target_that_fills_during_the_pull_is_seen_and_the_close_decides_again(tmp_path):
    g, fake, tid = await placed(tmp_path)
    t1 = next(i for i, o in fake.limits.items() if o["price"] == 0.95)
    real = type(fake).cancel_order

    async def racing(self, i, s, params=None):                  # T1 fills just before its cancel lands
        if i == t1 and self.limits[t1]["status"] == "open":
            self.fill_limit(t1)
        return await real(self, i, s, params)
    type(fake).cancel_order = racing
    try:
        r = await session(g).close(SYM, "half", fraction=0.5, expect_contracts=100)
    finally:
        type(fake).cancel_order = real
    assert not r["ok"] and "position is 50" in r["error"] and "target_fill" in r["error"], r
    assert market_buys(fake) == [] and fake.position == 50          # nothing more than the target closed
    assert book(fake) == [(0.9, 50)] and g.j.get_trade(tid)["targets_hit"] == [0.95]


async def test_a_full_close_cancels_the_targets_even_when_binance_leaves_them(tmp_path):
    g, fake, tid = await placed(tmp_path)
    fake.ro_expire_on_flat = False
    r = await session(g).close(SYM, "done", lesson="l")
    assert r["ok"] and fake.position == 0 and book(fake) == []
    assert {(x["status"], x["why"]) for x in targets.rows(g.j)} == {("cancelled", "flat after a full close")}


async def test_update_plan_moves_or_withdraws_the_resting_targets(tmp_path, monkeypatch):
    g, fake, tid = await placed(tmp_path)
    s = session(g)
    monkeypatch.setattr(mcp_trade, "get_app", lambda: SimpleNamespace(journal=s.j, trader=s))
    r = await mcp_trade.update_plan(SYM, {"targets": [{"price": 0.92, "fraction": 1.0}]}, "one target")
    assert r["ok"] and book(fake) == [(0.92, 100)], r
    r = await mcp_trade.update_plan(SYM, {"auto": {"targets": False}}, "alerts only")
    assert r["ok"] and book(fake) == []


async def test_an_add_grows_the_targets(tmp_path):
    plan = {**PLAN, "targets": [{"price": 0.85, "fraction": 0.5}, {"price": 0.8, "fraction": 0.5}]}
    g, fake, tid = await desk(tmp_path, plan)
    await g.check_positions()
    fake.price = 0.94                                             # +1.2R
    lim = RiskLimits(max_position_notional=300)
    s = session(g, lim)
    r = await PositionOps(s).add_to_winner(SYM, 47, 0.99, "press it")
    assert r["ok"] and fake.position == 150, r
    assert book(fake) == [(0.85, 75), (0.8, 75)]


async def test_a_widen_pulls_the_targets_before_its_size_cut(tmp_path):
    g, fake, tid = await placed(tmp_path)
    r = await PositionOps(session(g)).widen_stop(SYM, 1.08, "room")
    assert r["ok"] and market_buys(fake) == [r["size_cut"]], r
    left = fake.position
    assert book(fake) == [(0.95, round(50 - r["size_cut"], 8)), (0.9, 50)] and sum(q for _, q in book(fake)) == left
    assert not [x for x in targets.rows(g.j) if x["status"] == "expired"]


async def test_a_stop_swap_bridge_keeps_the_targets_or_they_are_placed_again(tmp_path):
    g, fake, tid = await placed(tmp_path)
    s = session(g)
    assert (await s.move_stop(SYM, 1.03, "tighten"))["ok"] and book(fake) == [(0.95, 50), (0.9, 50)]
    real = type(fake)._new_stop

    def bridge_competes(self, sym, side, amount, trig, params, cid):   # the open question: suppose it does
        if params.get("reduceOnly"):
            for _, o in self._ro_open(sym):
                o["status"] = "expired"
        return real(self, sym, side, amount, trig, params, cid)
    type(fake)._new_stop = bridge_competes
    try:
        assert (await s.move_stop(SYM, 1.02, "tighten more"))["ok"]
    finally:
        type(fake)._new_stop = real
    assert book(fake) == [(0.95, 50), (0.9, 50)]
    gone = [json.loads(d["snapshot"]) for d in g.j.decisions(SYM) if d["action"] == "target_gone"]
    assert sorted(x["target"] for x in gone) == [0.9, 0.95] and {x["status"] for x in gone} == {"expired"}


async def test_halted_keeps_the_targets_and_flatten_cancels_them(tmp_path):
    g, fake, tid = await placed(tmp_path)
    await ks.trip(g.j, g.app.settings, ks.HALTED, "test", "drill")
    await g.check_entries()
    await g.check_positions()
    assert book(fake) == [(0.95, 50), (0.9, 50)]                  # reduce-only: risk-reducing orders stay
    fake.ro_expire_on_flat = False
    r = await killswitch_ops.flatten(g.app.trader, "test", "drill")
    assert r["ok"] and fake.position == 0 and book(fake) == []


async def test_a_new_short_clears_targets_left_by_the_last_trade(tmp_path):
    g, fake, tid = await placed(tmp_path)
    fake.ro_expire_on_flat = False
    fake.trigger_stop()                                           # stopped out; the guardian hasn't passed yet
    assert fake.position == 0 and len(book(fake)) == 2
    r = await session(g).open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="again", notional=100)
    assert r["ok"] and fake.position == 100 and book(fake) == []


async def test_a_limit_entry_fill_clears_targets_of_another_trade():
    d, t, fake, _ = entry_desk()
    await one(d, prop(1.03))
    fake.fill_limit(next(i for i, o in fake.limits.items() if o["side"] == "sell"))
    stale = await fake.create_order(SYM, "limit", "buy", 10, 0.9, {"reduceOnly": True, "timeInForce": "GTC",
                                                                   "clientOrderId": "sb-t999-0badf00d"})
    (ev,) = await d.poll()
    assert ev["event"] == "filled" and fake.limits[stale["id"]]["status"] == "canceled"


async def test_the_entry_sweep_never_calls_a_target_external(tmp_path):
    g, fake, tid = await placed(tmp_path)
    d = EntryDesk(g.app.trader)
    assert await entry_intents._sweep(d) == []
    assert d._rows("status='external'") == [] and entry_intents.external_orders(g.j) == []


async def test_an_unreadable_book_never_blocks_a_close(tmp_path):
    g, fake, tid = await placed(tmp_path)
    fake.ro_expire_on_flat = False
    s = session(g)
    real = type(fake).fetch_open_orders

    async def down(self, sym, params=None):
        if not (params or {}).get("trigger"):
            raise ccxt.NetworkError("timeout")
        return await real(self, sym, params)
    type(fake).fetch_open_orders = down
    try:
        r = await s.close(SYM, "out", lesson="l")
    finally:
        type(fake).fetch_open_orders = real
    assert r["ok"] and fake.position == 0
    assert "target_cancel_failed" in [a["kind"] for a in s.j.alerts(50)]
    await g.check_positions()                                     # the guardian sweeps them
    assert book(fake) == [] and targets.live(g.j) == []
