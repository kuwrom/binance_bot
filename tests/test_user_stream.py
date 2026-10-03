"""The user-data stream's own life (#54, user_stream.py) against the fake's stream (fake_stream.py): the key, the URL,
keepalive and the 24 h cut, backoff, health judged by keepalive and events (never by the connection), one alert per
outage, misses, stats, and the key kept out of logs and alerts."""

import asyncio
import json
import logging
import time

import pytest

from shortbot import user_stream as us
from shortbot.config import RiskLimits, Settings
from shortbot.journal import Journal
from shortbot.notify import Notifier
from shortbot.trading import Trader
from tests import fake_errors as err
from tests.fake_stream import Hub, expired_event, foreign_order
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks

KEY = "fakeListenKeyDoNotLog0123456789abcdef"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    for k, v in {"WAKE_S": 0.005, "CONNECT_S": 0.3, "REST_S": 0.3, "BACKOFF_MIN_S": 0.001, "BACKOFF_MAX_S": 0.004,
                 "CLOSE_S": 0.05}.items():
        monkeypatch.setattr(us, k, v)


def build(fake=None, clock=None, live=False, on_hit=None, **kw):
    fake = fake or FakeCcxt()
    ex = FakeEx(fake)
    if live:
        ex.settings = Settings("live", "k", "s", ":memory:", "", "", RiskLimits())
    tr = Trader(ex, Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
    hub, hits = Hub(fake), []
    s = us.UserStream(tr, Notifier(ex.settings, tr.j), on_hit or (lambda h, rx: hits.append(h)), connect=hub.connect,
                      clock=clock or time.monotonic, **kw)
    return s, fake, hub, hits


async def until(cond, timeout=2.0):
    t0 = time.monotonic()
    while not cond():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.002)


class running:
    def __init__(self, s):
        self.s = s

    async def __aenter__(self):
        self.task = asyncio.create_task(self.s.run())
        return self.task

    async def __aexit__(self, *exc):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


def alerts(s, kind):
    return [a for a in s.j.alerts(100) if a["kind"] == kind]


async def test_connects_on_the_private_path_with_the_accounts_own_key():
    s, fake, hub, _ = build()
    async with running(s):
        await until(lambda: s.up)
    assert hub.urls == [f"wss://demo-fstream.binance.com/private/ws?listenKey={KEY}"]
    assert fake.listen_key_calls == ["POST", "new"]
    live, *_ = build(live=True)
    assert live.url("K") == "wss://fstream.binance.com/private/ws?listenKey=K"


async def test_our_events_reach_the_handler_with_their_job():
    s, fake, hub, hits = build()
    async with running(s):
        await until(lambda: s.up)
        o = await fake.create_order(SYM, "limit", "sell", 50, 1.04, {"clientOrderId": "sb-e5-0a0b0c0d"})
        fake.fill_limit(o["id"])
        await until(lambda: any(h.job == "entries" for h in hits))
    (h,) = [h for h in hits if h.job]
    assert (h.kind, h.status, h.symbol, h.cid) == ("entry", "FILLED", SYM, "sb-e5-0a0b0c0d")
    assert s.stats["events"]["ORDER_TRADE_UPDATE"] == 2 and s.stats["events"]["ACCOUNT_UPDATE"] == 1
    assert s.stats["triggers"] == {"entries": 1}


async def test_foreign_orders_trigger_nothing_but_are_logged_for_the_untracked_check(caplog):
    s, fake, hub, hits = build()
    caplog.set_level(logging.INFO, "shortbot.user_stream")
    async with running(s):
        await until(lambda: s.up)
        hub.push(foreign_order(fake, status="FILLED"))
        await until(lambda: s.stats["kinds"].get("foreign"))
    assert hits == [] and s.last_foreign[SYM]["cid"] == "web_8xUe2Ab" and s.last_foreign[SYM]["status"] == "FILLED"
    assert "web_8xUe2Ab" in caplog.text


async def test_a_handler_that_raises_never_stops_the_stream():
    def bad(h, rx):
        raise RuntimeError("bug")
    s, fake, hub, _ = build(on_hit=bad)
    async with running(s):
        await until(lambda: s.up)
        for cid in ("sb-e1-0a0b0c0d", "sb-e2-0a0b0c0d"):
            o = await fake.create_order(SYM, "limit", "sell", 50, 1.04, {"clientOrderId": cid})
            fake.fill_limit(o["id"])
        await until(lambda: s.stats["triggers"].get("entries") == 2)
        assert s.up and len(hub.urls) == 1


async def test_keepalive_every_30_minutes_and_never_a_delete():
    clock = Clock()
    s, fake, hub, _ = build(clock=clock)
    async with running(s):
        await until(lambda: s.up)
        clock.t += us.KEEPALIVE_S - 1
        await asyncio.sleep(0.05)
        assert "PUT" not in fake.listen_key_calls
        clock.t += 1
        await until(lambda: fake.listen_key_calls.count("PUT") == 1)
        clock.t += us.KEEPALIVE_S
        await until(lambda: fake.listen_key_calls.count("PUT") == 2)
    assert s.stats["keepalives"] == 2 and len(hub.urls) == 1 and s.key == KEY


async def test_a_key_replaced_by_another_process_reconnects_on_the_new_one():
    clock = Clock()
    s, fake, hub, _ = build(clock=clock)
    async with running(s):
        await until(lambda: s.up)
        fake.listen_key = None   # DELETEd elsewhere: our connection still "connects" but nothing comes
        clock.t += us.KEEPALIVE_S
        await until(lambda: len(hub.urls) == 2 and s.up)
    assert fake.listen_key_calls == ["POST", "new", "PUT", "POST", "new", "POST"]
    assert hub.urls[1].endswith("listenKey=fakeListenKey2DoNotLog0123456789") and s.key == fake.listen_key
    assert s.stats["reconnects"] == {"listenKey changed": 1}


async def test_a_failed_keepalive_is_retried_a_minute_later_and_the_stream_is_down_meanwhile():
    clock = Clock()
    s, fake, hub, _ = build(clock=clock)

    async def broken(req=None):
        fake.listen_key_calls.append("refused")
        raise err.rate_limited()
    async with running(s):
        await until(lambda: s.up)
        fake.fapiPrivatePutListenKey = fake.fapiPrivatePostListenKey = broken
        clock.t += us.KEEPALIVE_S
        await until(lambda: fake.listen_key_calls.count("refused") == 2)
        assert s.up is False and s.stats["keepalive_failures"] == 1
        await asyncio.sleep(0.05)
        assert fake.listen_key_calls.count("refused") == 2   # not before KEEPALIVE_RETRY_S
        del fake.fapiPrivatePutListenKey, fake.fapiPrivatePostListenKey
        clock.t += us.KEEPALIVE_RETRY_S
        await until(lambda: s.up)
    assert len(hub.urls) == 1 and s.stats["keepalives"] == 1


async def test_the_connection_is_renewed_before_the_24h_cut():
    clock = Clock()
    s, fake, hub, _ = build(clock=clock)
    async with running(s):
        await until(lambda: s.up)
        clock.t += us.ROTATE_S - 60
        await asyncio.sleep(0.05)
        assert len(hub.urls) == 1
        clock.t += 60
        await until(lambda: len(hub.urls) == 2 and s.up)
        assert hub.sockets[0].closed and not hub.sockets[1].closed
    assert hub.urls[0] == hub.urls[1] and s.stats["reconnects"] == {"24 h cut": 1}


async def test_listen_key_expired_reconnects_with_a_fresh_key():
    s, fake, hub, _ = build()
    async with running(s):
        await until(lambda: s.up)
        fake.listen_key = None
        hub.push(expired_event(fake, KEY))
        await until(lambda: len(hub.urls) == 2 and s.up)
    assert hub.urls[1].endswith("listenKey=fakeListenKey2DoNotLog0123456789")
    assert s.stats["reconnects"] == {"listenKeyExpired": 1}


async def test_backoff_doubles_to_its_cap_and_resets_after_a_stable_connection(monkeypatch):
    monkeypatch.setattr(us, "BACKOFF_MIN_S", 1.0)
    monkeypatch.setattr(us, "BACKOFF_MAX_S", 60.0)
    clock, waits = Clock(), []

    async def sleep(d):
        waits.append(d)
        await asyncio.sleep(0)
    s, fake, hub, _ = build(clock=clock, sleep=sleep)
    hub.fail_connect = 8
    async with running(s):
        await until(lambda: s.up)
        assert waits == [1, 2, 4, 8, 16, 32, 60, 60]
        clock.t += us.STABLE_S
        hub.close_all()
        await until(lambda: len(waits) == 9)
        await until(lambda: s.up)
    assert waits[8] == 1 and s.stats["errors"] == 9


async def test_a_dead_stream_alerts_once_per_outage_and_once_when_it_is_back(monkeypatch):
    monkeypatch.setattr(us, "DOWN_ALERT_S", 0.05)
    monkeypatch.setattr(us, "CONNECT_S", 0.01)
    s, fake, hub, _ = build()
    hub.hang_connect = True   # connects never finish: the watch still alerts
    async with running(s):
        await until(lambda: alerts(s, "user_stream_down"))
        await asyncio.sleep(0.1)
        assert len(alerts(s, "user_stream_down")) == 1 and not s.up
        hub.hang_connect = False
        await until(lambda: alerts(s, "user_stream_up"))
    (down,) = alerts(s, "user_stream_down")
    assert "polls" in down["message"] and "TimeoutError" in down["message"]


async def test_the_key_never_reaches_a_log_line_or_an_alert(monkeypatch, caplog):
    monkeypatch.setattr(us, "DOWN_ALERT_S", 0.02)
    caplog.set_level(logging.DEBUG)
    s, fake, hub, _ = build()

    async def refuse(url):
        raise ConnectionRefusedError(f"fake: cannot reach {url}")
    s.connect = refuse
    async with running(s):
        await until(lambda: alerts(s, "user_stream_down"))
    assert KEY not in caplog.text and KEY not in json.dumps(s.j.alerts(100))
    assert "listenKey=<redacted>" in alerts(s, "user_stream_down")[0]["message"]


async def test_fills_the_poll_found_that_the_stream_never_reported_are_misses_and_reconnect(monkeypatch):
    monkeypatch.setattr(us, "UP_MARGIN_MS", 0)
    clock = Clock()
    s, fake, hub, _ = build(clock=clock)
    async with running(s):
        await until(lambda: s.up)
        o = await fake.create_order(SYM, "limit", "sell", 50, 1.04, {"clientOrderId": "sb-e7-0a0b0c0d"})
        fake.fill_limit(o["id"])
        await until(lambda: "sb-e7-0a0b0c0d" in s.traded)
        now = int(time.time() * 1000)
        s.expect("sb-e7-0a0b0c0d", now)              # the stream said so: a hit
        s.expect("sb-e8-0a0b0c0d", now - 3_600_000)  # filled before the stream was up: no verdict
        s.expect("sb-e9-0a0b0c0d", now)
        await until(lambda: s.stats["hits"] == 1)
        clock.t += us.EXPECT_S
        await until(lambda: s.stats["misses"] == 1)
        assert len(hub.urls) == 1
        s.expect("sb-e10-0a0b0c0d", now)
        clock.t += us.EXPECT_S
        await until(lambda: len(hub.urls) == 2 and s.up)
    assert s.stats["misses"] == 2 and s.stats["reconnects"] == {"missed fills": 1} and s.expected == {}


async def test_stats_are_written_to_kv_with_the_lanes_figures(monkeypatch):
    monkeypatch.setattr(us, "STATS_S", 0.0)
    s, fake, hub, _ = build(extra_stats=lambda: {"entries": {"runs": 3}})
    async with running(s):
        await until(lambda: s.up)
        hub.push(foreign_order(fake))
        await until(lambda: (json.loads(s.j.get(us.STATS_KEY) or "{}").get("kinds") or {}).get("foreign"))
    got = json.loads(s.j.get(us.STATS_KEY))
    assert got["up"] is True and got["lanes"] == {"entries": {"runs": 3}} and got["events"]["ORDER_TRADE_UPDATE"] == 1
    assert got["lag_ms"]["n"] == 1


async def test_entry_partial_to_full_times_are_measured():
    s, fake, hub, hits = build()
    async with running(s):
        await until(lambda: s.up)
        o = await fake.create_order(SYM, "limit", "sell", 50, 1.04, {"clientOrderId": "sb-e5-0a0b0c0d"})
        fake.fill_limit(o["id"], qty=20)
        await until(lambda: s.partial)
        assert [h for h in hits if h.job] == []   # a partial waits for the poll (#54)
        fake.fill_limit(o["id"])
        await until(lambda: s.stats["partial_to_full_ms"])
    assert s.partial == {} and s.stats["partial_to_full_ms"][0] >= 0


async def test_a_stop_fill_seen_before_its_algo_update_is_not_left_as_a_foreign_order():
    from tests.test_user_events import algo, order
    s, fake, hub, hits = build()
    async with running(s):
        await until(lambda: s.up)
        hub.push(order("autogen88001", "TRADE", "FILLED", i=88001))   # reordered: the fill first
        await until(lambda: s.last_foreign)
        hub.push(algo("sb-s41-0a0b0c0d", "FINISHED", ai="88001"))
        await until(lambda: hits)
    assert s.last_foreign == {} and [h.job for h in hits] == ["positions"]
