"""The user-data stream in the guardian (#54): an event about one of our orders runs the handler that already
reconciles it (the fill poll, the positions pass) at once; the polls stay the truth, so a dropped, duplicated or
reordered event changes nothing but latency, and a dead stream never holds up the protective lane."""

import asyncio
import json
import time

import pytest

from shortbot import guardian as guardian_mod, user_stream as us
from shortbot.guardian_positions import UNTRACKED_GRACE_S
from shortbot.killswitch import HEARTBEAT_KEY
from shortbot.symlock import SymbolBusy
from tests.fake_stream import Hub, foreign_order
from tests.fakes import SYM, FakeCcxt
from tests.test_entries import favorable, prop
from tests.test_guardian import guardian_with, kinds, open_trade
from tests.test_target_paths import placed
from tests.test_user_stream import until

SLOW = 3000   # the polls' schedule in these tests: whatever runs sooner was triggered by the stream


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    for k, v in {"WAKE_S": 0.005, "CONNECT_S": 0.3, "REST_S": 0.3, "BACKOFF_MIN_S": 0.001, "BACKOFF_MAX_S": 0.004,
                 "CLOSE_S": 0.05, "UP_MARGIN_MS": -1000}.items():
        monkeypatch.setattr(us, k, v)
    monkeypatch.setattr(guardian_mod, "PROTECTIVE", {"entries": (SLOW, 240), "positions": (SLOW, 240),
                                                     "entry_reconcile": (SLOW, 240)})
    monkeypatch.setattr(guardian_mod, "TRIGGER_GAP_S", {"entries": 0.0, "positions": 0.0})
    monkeypatch.setattr(guardian_mod, "TICK_S", 0.01)


def counted(g, name):
    runs, real = [], getattr(g, name)

    async def run():
        runs.append(time.monotonic())
        await real()
    setattr(g, name, run)
    return runs


class live:
    """The protective lane and the user stream, as Guardian.run starts them (the other lanes aren't needed)."""

    def __init__(self, g, hub):
        self.g, self.hub = g, hub

    async def __aenter__(self):
        g = self.g
        g.user_stream.connect = self.hub.connect
        g.lanes = g._lanes()
        lane = g.lanes["protective"]
        self.tasks = [asyncio.create_task(lane.run()), asyncio.create_task(g.user_stream.run())]
        await until(lambda: g.user_stream.up and all(lane.last.values()))
        return lane

    async def __aexit__(self, *exc):
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


async def entry_desk(fake=None):
    fake = fake or FakeCcxt()
    g = guardian_with(fake)
    g.entries._regime_fn = favorable
    r = await g.entries.propose_group([prop(1.04), prop(1.06, stop=1.12)])
    assert r["ok"], r
    return g, fake, next(i for i, o in fake.limits.items() if o["price"] == 1.04)


async def test_an_entry_fill_event_runs_the_fill_poll_at_once():
    g, fake, oid = await entry_desk()
    hub = Hub(fake)
    async with live(g, hub) as lane:
        fake.fill_limit(oid)
        await until(lambda: g.j.open_trades(), timeout=1.0)
        await until(lambda: all(o["status"] != "open" for o in fake.limits.values()))
    assert list(fake.stops.values()) == [1.10] and fake.prearms == {}
    st = lane.trigger_stats["entries"]
    assert st["runs"] >= 1 and st["ahead_max_s"] > SLOW - 60 and st["delay_max_s"] < 1.0
    assert g.user_stream.stats["triggers"]["entries"] >= 1


async def test_a_stop_out_event_reconciles_the_closed_trade_at_once():
    fake = FakeCcxt()
    g = guardian_with(fake)
    tid = await open_trade(g)
    async with live(g, Hub(fake)) as lane:
        fake.price = 1.06   # the closePosition stop at 1.05 triggers: ALGO_UPDATE TRIGGERED, FINISHED
        await until(lambda: g.j.get_trade(tid)["status"] == "closed", timeout=1.0)
    assert lane.trigger_stats["positions"]["runs"] >= 1 and "trade_closed" in kinds(g)


async def test_a_target_fill_event_settles_it_at_once(tmp_path):
    g, fake, tid = await placed(tmp_path)
    async with live(g, Hub(fake)) as lane:
        fake.price = 0.95   # T1 (a reduce-only limit) fills on the way down
        await until(lambda: any(d["action"] == "target_fill" for d in g.j.decisions(SYM)), timeout=1.0)
    assert lane.trigger_stats["positions"]["runs"] >= 1


async def test_a_dropped_event_is_still_caught_by_the_poll_and_counted_as_a_miss(monkeypatch):
    monkeypatch.setattr(guardian_mod, "PROTECTIVE", {"entries": (0.2, 240), "positions": (SLOW, 240),
                                                     "entry_reconcile": (SLOW, 240)})
    monkeypatch.setattr(us, "EXPECT_S", 0.05)
    g, fake, oid = await entry_desk()
    hub = Hub(fake)
    hub.drop = lambda ev: ev["e"] == "ORDER_TRADE_UPDATE" and ev["o"]["x"] == "TRADE"
    async with live(g, hub):
        fake.fill_limit(oid)
        await until(lambda: g.j.open_trades(), timeout=1.0)
        await until(lambda: g.user_stream.stats["misses"] == 1)
    assert list(fake.stops.values()) == [1.10] and hub.dropped and g.user_stream.stats["hits"] == 0


async def test_duplicate_and_reordered_events_are_harmless():
    g, fake, oid = await entry_desk()
    hub = Hub(fake)
    hub.dup = hub.reverse = True
    async with live(g, hub):
        fake.fill_limit(oid)
        await until(lambda: g.j.open_trades(), timeout=1.0)
        await asyncio.sleep(0.1)
        (tr,) = g.j.open_trades()
        fake.price = 1.11   # past the stop: the stop-out's events come twice, each batch reversed
        await until(lambda: g.j.get_trade(tr["id"])["status"] == "closed", timeout=1.0)
        await asyncio.sleep(0.1)
    assert len(g.j._rows("SELECT id FROM trades")) == 1
    assert len([o for o in fake.orders if o[3].get("closePosition")]) == 1
    assert kinds(g).count("trade_closed") == 1 and kinds(g).count("action_fill") == 1
    assert not [k for k in kinds(g) if k.startswith(("guardian_error", "guardian_failing", "fill_unresolved",
                                                     "entry_unknown", "entry_cancel_failing", "stop_"))]


@pytest.mark.parametrize("fault", ["connect hangs", "silent on a dead key"])
async def test_a_dead_or_fake_stream_never_holds_up_the_protective_lane_and_alerts_once(monkeypatch, fault):
    monkeypatch.setattr(guardian_mod, "PROTECTIVE", {"entries": (0.02, 240), "positions": (SLOW, 240),
                                                     "entry_reconcile": (SLOW, 240)})
    monkeypatch.setattr(us, "DOWN_ALERT_S", 0.05)
    monkeypatch.setattr(us, "CONNECT_S", 0.01)
    monkeypatch.setattr(us, "KEEPALIVE_S", 0.02)
    fake = FakeCcxt()
    g = guardian_with(fake)
    runs = counted(g, "check_entries")
    hub = Hub(fake)
    if fault == "connect hangs":
        hub.hang_connect = True
    else:   # it connected, then the key went (deleted elsewhere) and no new one can be had: it "connects", says nothing
        real = fake.fapiPrivatePostListenKey

        async def once(req=None):
            if fake.listen_key_calls.count("POST"):
                raise ConnectionResetError("fake: POST /fapi/v1/listenKey failed")
            return await real(req)
        fake.fapiPrivatePostListenKey = once
        hub.silent = True
    g.user_stream.connect = hub.connect
    g.lanes = g._lanes()
    tasks = [asyncio.create_task(g.lanes["protective"].run()), asyncio.create_task(g.user_stream.run())]
    try:
        if fault != "connect hangs":
            await until(lambda: g.user_stream.connected)
            fake.listen_key = None
        await asyncio.sleep(0.4)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert len(runs) >= 8 and not g.user_stream.up
    assert kinds(g).count("user_stream_down") == 1


async def test_a_triggered_positions_pass_that_meets_a_busy_symbol_never_counts_toward_its_streak():
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)

    async def busy(p):
        raise SymbolBusy(SYM, 0.5, "a session's close")
    g._one_position = busy
    g.lanes = g._lanes()
    lane = g.lanes["protective"]
    lane.last = {k: time.time() for k in lane.last}   # every job just ran on schedule
    for _ in range(5):
        lane.trigger("positions", why="test", at=time.monotonic())
        assert await lane.tick()
    assert g.pos_streak.fails == {} and "guardian_failing:position" not in kinds(g)
    await g.check_positions()   # a scheduled pass counts, as before
    assert g.pos_streak.fails == {SYM: 1}


async def test_a_triggered_pass_that_verifies_everything_stamps_the_heartbeat():
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    g.lanes = g._lanes()
    lane = g.lanes["protective"]
    lane.last = {k: time.time() for k in lane.last}
    assert g.j.get(HEARTBEAT_KEY) is None
    lane.trigger("positions", why="test", at=time.monotonic())
    await lane.tick()
    assert g.j.get(HEARTBEAT_KEY) and lane.trigger_stats["positions"]["runs"] == 1


async def test_the_untracked_alert_quotes_the_foreign_order_the_stream_saw():
    fake = FakeCcxt()
    g = guardian_with(fake)
    hub = Hub(fake)
    async with live(g, hub):
        hub.push(foreign_order(fake, status="FILLED"))
        await until(lambda: SYM in g.user_stream.last_foreign)
    fake.position, fake.entry, fake.stops["s1"] = 50, 1.0, 1.1   # opened in the Binance app
    await g.check_positions()
    g.untracked_since[SYM] -= UNTRACKED_GRACE_S
    await g.check_positions()
    (a,) = [a for a in g.j.alerts(100) if a["kind"] == "untracked_position"]
    assert "web_8xUe2Ab" in a["message"] and "FILLED" in a["message"]


async def test_the_stream_runs_only_with_trade_keys_and_its_stats_carry_the_lanes():
    fake = FakeCcxt()
    g = guardian_with(fake)
    assert g.user_stream is not None
    g.ex.trade = None
    assert guardian_mod.Guardian(g.app).user_stream is None
    g.lanes = g._lanes()
    g.lanes["protective"].trigger("entries", why="x", at=time.monotonic())
    await g.lanes["protective"].tick()
    assert json.loads(json.dumps(g._trigger_stats()))["entries"]["runs"] == 1
