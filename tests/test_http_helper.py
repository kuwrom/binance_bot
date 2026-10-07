"""shortbot.http, the guarded REST helper for scripts and lab, and level_watch on it: each call is costed up front from
Binance's published weights, paced host-wide, and its answer recorded host-wide; a 418 stops research and the watch
backs off. HTTP is faked at http._send with tests/fake_http.FakeHost (the same headers and -1003 bodies)."""

import io
import json
import sqlite3
import threading
import time

import pytest

from shortbot import http, level_watch, weight
from shortbot.weight import BudgetWait
from tests.fake_http import FakeHost, fake_clock


@pytest.fixture
def clock(monkeypatch):
    weight.reset()
    yield fake_clock(monkeypatch, weight)
    weight.reset()


@pytest.fixture
def fapi(monkeypatch):
    """fapi.binance.com behind http._send; `sent` keeps (time, path, cost) per request that reached it."""
    host = FakeHost("fapi.binance.com", 2400, step=0)
    host.at = time.monotonic
    host.log = []

    def send(url, timeout):
        from urllib.parse import parse_qsl, urlsplit
        u = urlsplit(url)
        params = dict(parse_qsl(u.query))
        c = http.cost(u.path, params)
        host.used += c
        host.log.append((host.at(), u.path, c))
        r = host.respond("GET", u.path)
        if u.path.endswith("/premiumIndex") and r.status == 200:
            r._text = json.dumps({"markPrice": "2.0", "lastFundingRate": "0.0001"})
        return r.status, r.headers, r._text
    monkeypatch.setattr(http, "_send", send)
    return host


def test_klines_cost_by_limit_and_the_other_published_weights():
    assert [http.cost("/fapi/v1/klines", {"limit": n}) for n in (1, 99, 100, 499, 500, 1000, 1001, 1500)] == \
        [1, 1, 2, 2, 5, 5, 10, 10]
    assert http.cost("/fapi/v1/klines", {}) == 5                          # default limit 500
    assert http.cost("/fapi/v1/markPriceKlines", {"limit": 1500}) == 10   # the variants share the table
    assert (http.cost("/fapi/v1/ticker/24hr", {"symbol": "BTCUSDT"}), http.cost("/fapi/v1/ticker/24hr")) == (1, 40)
    assert (http.cost("/fapi/v1/premiumIndex", {"symbol": "X"}), http.cost("/fapi/v1/premiumIndex")) == (1, 10)
    assert [http.cost("/fapi/v1/depth", {"limit": n}) for n in (5, 50, 100, 500, 1000)] == [2, 2, 5, 10, 20]
    assert http.cost("/futures/data/openInterestHist", {"symbol": "X"}) == 0
    assert http.cost("/fapi/v1/somethingNew") == http.UNKNOWN_COST


def test_a_get_is_recorded_and_its_reading_shared(clock, fapi):
    fapi.at = lambda: clock.t
    assert http.get("/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "1h", "limit": 1000}) == []
    assert fapi.log == [(clock.t, "/fapi/v1/klines", 5)]
    weight.reset()
    assert weight.budget("fapi.binance.com").used == 0
    weight.record("https://fapi.binance.com/fapi/v1/time", 200, {"x-mbx-used-weight-1m": "1300"}, "")
    with pytest.raises(BudgetWait, match="1300/2400"):
        http.get("/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "1h"}, wait_s=0)


def test_pacing_caps_a_loop_like_the_one_that_got_the_ip_banned(clock, fapi):
    """472 klines limit=1000 sent as fast as a loop can: never more than PACE_WEIGHT_CAP weight in any 10 s."""
    fapi.at = lambda: clock.t
    for _ in range(80):
        http.get("/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "1h", "limit": 1000}, wait_s=1e9)
    times = [t for t, _, _ in fapi.log]
    for t0 in times:
        assert sum(c for t, _, c in fapi.log if t0 <= t < t0 + weight.PACE_WINDOW_S) <= weight.PACE_WEIGHT_CAP
    assert times[-1] - times[0] >= (80 * 5 / weight.PACE_WEIGHT_CAP - 1) * weight.PACE_WINDOW_S


def test_futures_data_endpoints_are_paced_by_call_count(clock, fapi):
    fapi.at = lambda: clock.t
    for _ in range(40):
        http.get("/futures/data/openInterestHist", {"symbol": "BTCUSDT", "period": "1h"}, wait_s=1e9)
    first = [t for t, _, _ in fapi.log if t < fapi.log[0][0] + weight.PACE_WINDOW_S]
    assert len(first) == 30


def test_threads_cannot_burst_past_the_pacing_cap(fapi, monkeypatch):
    """Eight threads on the real clock (the 2026-10-07 script's shape), with a small cap and window."""
    weight.reset()
    monkeypatch.setattr(weight, "PACE_WEIGHT_CAP", 20.0)
    monkeypatch.setattr(weight, "PACE_WINDOW_S", 0.4)
    monkeypatch.setattr(weight, "POLL_S", 0.02)

    def work():
        for _ in range(3):
            http.get("/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "1h", "limit": 1000}, wait_s=30)
    t0 = time.monotonic()
    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(fapi.log) == 24                      # 120 weight at 20 per 0.4 s: at least 5 windows
    assert time.monotonic() - t0 >= 4 * 0.4
    weight.reset()


def test_a_418_through_the_helper_stops_research_until_retry_after(clock, fapi):
    fapi.answer_next(418, retry_after=600)
    with pytest.raises(http.BinanceHTTPError) as e:
        http.get("/fapi/v1/ticker/24hr")
    assert e.value.limited and e.value.status == 418 and e.value.retry_after == "600"
    weight.reset()                                   # any other process on the host
    with pytest.raises(BudgetWait, match="418"):
        http.get("/fapi/v1/ticker/24hr", wait_s=0)
    assert len(fapi.log) == 1                         # nothing more reached Binance
    clock.t += 601
    http.get("/fapi/v1/ticker/24hr", wait_s=0)


def test_the_helper_never_runs_protective_or_full_urls():
    with pytest.raises(ValueError, match="protective"):
        http.get("/fapi/v1/time", cls=weight.PROTECTIVE)
    with pytest.raises(ValueError, match="path"):
        http.get("https://fapi.binance.com/fapi/v1/time")


def test_the_level_watch_backs_off_on_a_ban_and_says_so_once(clock, fapi, monkeypatch, tmp_path):
    db = tmp_path / "j.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY, kind TEXT, symbol TEXT, message TEXT, mode TEXT, "
                  "actor TEXT)")
    monkeypatch.setattr(level_watch, "_sleep", clock.sleep_sync)
    fapi.at = lambda: clock.t
    fapi.answer_next(418, retry_after=100)
    out = io.StringIO()
    level_watch.run({"ZROUSDT": [1.85, 2.1]}, db, "demo", every_s=45, out=out, polls=4)
    lines = out.getvalue().splitlines()
    assert len(fapi.log) == 2                        # the 418, then nothing until Retry-After (t+135)
    assert len(lines) == 2 and lines[0].startswith("watch paused") and "418" in lines[0]
    assert lines[1].startswith("watch resumed")
