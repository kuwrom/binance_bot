"""Adopting a hand-placed short (trading_adopt.py): the position gets a journal trade found by its own entry order ids,
with the stop on first; whatever the trade history can't explain is refused; the kill switch trips as the guardian
would have and is never cleared; reconcile closes the adopted trade with its own income."""

import dataclasses
from datetime import UTC, datetime

import pytest

from shortbot import episodes, order_ids, reconcile
from shortbot import killswitch as ks
from shortbot.guardian_positions import UNTRACKED_GRACE_S
from shortbot.journal import Journal, now_iso
from shortbot.killswitch import ACTIVE, HALTED, REDUCING
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks
from tests.test_guardian import guardian_with, kinds

WHY = "the owner's hand short: fading the pump, out above the 1.08 swing high"


def setup(lev=5):
    fake = FakeCcxt()
    fake.sym[SYM]["lev"] = lev   # liquidation ~1.19: at the fake's 20x (~1.04) every stop here is past the cap (#64)
    return Trader(FakeEx(fake), Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget), fake


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds")


def net(fake, tids=None):
    return sum(float(i["income"]) for i in fake.income if tids is None or i["tradeId"] in tids)


async def adopt(t, stop=1.08, setup="exp_manual", thesis=WHY, **kw):
    return await t.adopt(SYM, stop, setup, thesis, **kw)


async def test_a_hand_short_is_journalled_by_its_entry_orders_with_the_stop_on_first():
    t, fake = setup()
    oid = fake.hand_order(qty=50)
    fill = fake.fills[-1]
    fake.advance(600)
    r = await adopt(t, plan={"targets": [{"price": 0.9, "fraction": 0.5}]})
    assert r["ok"] and r["contracts"] == 50 and r["stop"] == 1.08 and r["entry_orders"] == [oid], r
    tr = t.j.get_trade(r["trade_id"])
    assert (tr["status"], tr["entry_price"], tr["amount"], tr["stop_price"], tr["initial_stop"]) == \
        ("open", 1.0, 50, 1.08, 1.08)
    assert tr["opened_at"] == iso(fill["timestamp"])              # its first entry fill, not the adoption
    assert tr["entry_features"]["entry_type"] == "adopted" and tr["setup"] == "exp_manual" and tr["thesis"] == WHY
    assert tr["plan"]["targets"] == [{"price": 0.9, "fraction": 0.5}]
    assert order_ids.read(t.j, tr["id"])["entry"] == {oid: None}
    assert list(fake.stops.values()) == [1.08]
    assert (await episodes.match(t, tr))["by"] == "ids"
    assert t.j.decisions()[0]["action"] == "adopt" and "position_adopted" in [a["kind"] for a in t.j.alerts()]


async def test_what_can_not_be_adopted_is_refused_with_nothing_journalled_and_no_stop_moved():
    t, fake = setup()
    assert "no open position" in (await adopt(t))["error"]
    fake.hand_order(qty=50)
    for kw, text in (({"thesis": " "}, "thesis"), ({"setup": "nope"}, "unknown setup"),
                     ({"plan": {"bogus": 1}}, "unknown plan fields"), ({"stop": 0.99}, "above entry"),
                     ({"stop": 1.3}, "liquidation"), ({"lookback_days": 0}, "lookback_days"),
                     ({"plan": {"uses_learnings": [999]}}, "no learning #999")):
        r = await adopt(t, **kw)
        assert not r["ok"] and text in str(r), (kw, r)
    fake.sym[SYM]["margin"] = "CROSSED"
    assert "cross margin" in (await adopt(t))["error"]
    fake.sym[SYM]["margin"] = "ISOLATED"
    fake.stops["s1"] = 1.05                                       # the owner's own stop, tighter than the one asked
    assert "stops only tighten" in (await adopt(t))["error"] and list(fake.stops.values()) == [1.05]
    fake.stops.clear()
    fake.sym[SYM]["lev"] = 2                                      # liquidation far away: the desk's own limit decides
    assert "max stop distance" in (await adopt(t, stop=1.3))["error"]
    assert t.j.trades() == [] and fake.stops == {} and ks.read(t.j)["state"] == ACTIVE


async def test_a_long_a_journalled_trade_and_a_symbol_with_resting_orders_are_refused():
    t, fake = setup()
    fake.hand_order(qty=50)
    real = fake.fetch_positions

    async def longs():
        return [{**(await real())[0], "side": "long"}]
    fake.fetch_positions = longs
    assert "short-only" in (await adopt(t))["error"]
    fake.fetch_positions = real
    t.j.db.execute("CREATE TABLE IF NOT EXISTS entries (id INTEGER PRIMARY KEY, group_id TEXT, symbol TEXT, status "
                   "TEXT, order_id TEXT, mode TEXT)")
    t.j.db.execute("INSERT INTO entries (group_id, symbol, status, order_id, mode) VALUES ('external', ?, 'external',"
                   " '77', 'demo')", (SYM,))
    r = await adopt(t)
    assert not r["ok"] and "cancel-external" in r["hint"] and t.j.trades() == []
    t.j.db.execute("DROP TABLE entries")
    first = await adopt(t)
    again = await adopt(t)
    assert first["ok"] and not again["ok"] and again["trade_id"] == first["trade_id"] and len(t.j.trades()) == 1


async def test_history_that_does_not_explain_the_position_is_refused_never_matched_by_time(monkeypatch):
    t, fake = setup()
    fake.position, fake.entry = 50, 1.0                           # a position with no fills in the history read
    assert "no short still open" in (await adopt(t))["error"]
    t, fake = setup()
    monkeypatch.setattr("shortbot.trading_adopt._now_ms", fake.now_ms)   # the desk's clock follows the exchange's
    fake.hand_order(qty=50)
    fake.advance(3 * 86400)
    fake.price = 1.02
    fake.hand_order(qty=30, cid="web_hand2")                      # added to two days after it opened
    r = await adopt(t, lookback_days=1)                           # the read begins inside the position
    assert not r["ok"] and "trade history explains 30" in r["error"] and t.j.trades() == [] and fake.stops == {}
    r = await adopt(t, lookback_days=7)
    assert r["ok"] and r["amount"] == 80 and r["entry"] == pytest.approx((50 * 1.0 + 30 * 1.02) / 80), r


async def test_a_part_bought_back_by_hand_before_the_adoption_is_in_the_trades_size_and_income():
    t, fake = setup()
    fake.hand_order(qty=50)
    fake.advance(60)
    fake.price = 0.98
    fake.hand_order(side="buy", qty=20, cid="web_hand2")
    fake.advance(60)
    r = await adopt(t)
    assert r["ok"] and r["contracts"] == 30 and r["amount"] == 50, r   # original size: targets and R count from it
    fake.advance(60)
    fake.price = 1.08                                             # stopped out
    (res,) = await reconcile.reconcile(t)
    tr = t.j.get_trade(r["trade_id"])
    assert res["trade_id"] == tr["id"] and tr["status"] == "closed" and tr["pnl"] == pytest.approx(net(fake))


async def test_an_adopted_trade_closes_by_its_ids_beside_an_earlier_desk_trade_on_the_symbol(monkeypatch):
    t, fake = setup()
    monkeypatch.setattr("shortbot.journal.now_iso", lambda: iso(fake.now_ms()))
    old = (await t.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100))["trade_id"]
    fake.advance(60)
    fake.price = 1.05                                             # the desk trade is stopped out
    fake.advance(60)
    mark = len(fake.fills)
    fake.hand_order(qty=50)                                       # the owner shorts again by hand, before any pass
    early = await adopt(t, stop=1.10)
    assert not early["ok"] and early["trade_id"] == old and "within a minute" in early["error"]
    assert [d["trade_id"] for d in await reconcile.reconcile(t)] == [old]   # the pass closes the old trade at its exit
    r = await adopt(t, stop=1.10)
    assert r["ok"], r
    fake.advance(60)
    fake.price = 1.10
    mine = {f["id"] for f in fake.fills[mark:]}
    new = t.j.get_trade(r["trade_id"])
    assert [d["trade_id"] for d in await reconcile.reconcile(t)] == [new["id"]]
    new = t.j.get_trade(new["id"])
    assert new["status"] == "closed" and new["exit_price"] == pytest.approx(1.10)
    assert new["pnl"] == pytest.approx(net(fake, mine))           # its own fills' income: the entry commission too
    assert t.j.get_trade(old)["pnl"] == pytest.approx(net(fake) - net(fake, mine))


@pytest.mark.parametrize("state", [REDUCING, HALTED])
async def test_adoption_works_while_the_kill_switch_is_tripped_and_never_loosens_it(state):
    t, fake = setup()
    await ks.trip(t.j, t.ex.settings, state, "guardian", "drawdown")
    fake.hand_order(qty=50)
    r = await adopt(t)
    assert r["ok"] and r["kill_switch"] == state and ks.read(t.j)["cause"] == "manual", r
    assert (await t.move_stop(SYM, 1.06, "tighter"))["ok"] and t.j.active_trade(SYM)["stop_price"] == 1.06


async def test_adopting_inside_the_guardians_grace_still_trips_reducing_as_the_guardian_would():
    t, fake = setup()
    fake.hand_order(qty=50)
    r = await adopt(t)
    st = ks.read(t.j)
    assert r["ok"] and r["kill_switch"] == REDUCING and st["cause"] == "reconciliation" and st["by"] == "adopt", r


async def test_a_position_a_swept_hand_limit_explains_is_adopted_without_a_trip_as_today():
    t, fake = setup()
    fake.hand_order(qty=50)
    from shortbot.entries import EntryDesk
    EntryDesk(t)                                                  # the entries table
    t.j._insert("INSERT INTO entries (group_id, symbol, status, created_at, expires_ms) VALUES ('external', ?, "
                "'external_filled', ?, 0)", (SYM, now_iso()))     # the sweep saw the order rest, then fill
    assert (await adopt(t))["ok"] and ks.read(t.j)["state"] == ACTIVE


async def test_a_human_clear_holds_once_the_position_is_adopted_and_is_re_tripped_while_it_is_not():
    fake = FakeCcxt()
    fake.sym[SYM]["lev"] = 5
    g = guardian_with(fake)
    g.j.set(ks.HEARTBEAT_KEY, "9" * 12)
    fake.hand_order(qty=50)
    await g.check_positions()                                     # the guardian's fallback stop goes on (entry + 10%)
    await g.check_positions()                                     # protected: first seen untracked
    g.untracked_since[SYM] -= UNTRACKED_GRACE_S
    await g.check_positions()
    assert ks.read(g.j)["state"] == REDUCING and "untracked_position" in kinds(g)

    async def human_clears():
        await ks.clear(g.j, g.app.settings, ACTIVE, "human:kib (tty)", "my own trade", g.j.get(ks.state_key(g.j)))
    await human_clears()
    await g.check_positions()
    assert ks.read(g.j)["state"] == REDUCING                      # today: still untracked, so tripped again
    r = await g.app.trader.adopt(SYM, 1.08, "exp_manual", WHY)
    assert r["ok"] and list(fake.stops.values()) == [1.08], r     # tightened from the fallback, make before break
    await human_clears()
    await g.check_positions()
    assert ks.read(g.j)["state"] == ACTIVE and SYM not in g.untracked_since


async def test_adopt_is_refused_on_the_live_desk_for_now():
    t, fake = setup()
    fake.hand_order(qty=50)
    t.ex.settings = dataclasses.replace(t.ex.settings, mode="live")
    r = await adopt(t)
    assert not r["ok"] and "demo desk only" in r["error"] and t.j.trades() == [] and fake.stops == {}
