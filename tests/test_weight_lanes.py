"""Review fixes to the host-wide weight guard: count lanes (/futures/data, funding) are counted for every desk/research
call and cool down on their own 429; the ledger recovers from a corrupt file; a dropped release is not counted twice;
nothing current means nothing in flight; the guardian's ledger never waits; the gateway's start-up is protective;
the published weights of bookTicker and historicalTrades."""

import asyncio
import contextlib
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from shortbot import gateway_acts, http, weight, weight_ledger, weight_share
from shortbot.weight import DESK, PROTECTIVE, RESEARCH, BudgetWait
from tests.fake_http import fake_clock

FAPI = "https://fapi.binance.com"
OI = f"{FAPI}/futures/data/openInterestHist"
KL = f"{FAPI}/fapi/v1/klines"
HOST = "fapi.binance.com"


@pytest.fixture
def clock(monkeypatch):
    weight.reset()
    yield fake_clock(monkeypatch, weight)
    weight.reset()


def admit(url, cls, wait_s=0.0, paced=False):
    with weight.tagged(cls, wait_s=wait_s, paced=paced):
        weight.release(weight.admit_blocking(url))


def passes(url, cls, wait_s=0.0) -> bool:
    try:
        admit(url, cls, wait_s)
        return True
    except BudgetWait:
        return False


def test_count_lane_calls_are_counted_paced_or_not_and_research_waits_at_the_cap(clock, monkeypatch):
    monkeypatch.setattr(weight_share, "COUNT_LANES", (("/futures/data/", "futures_data", 5.0),))
    for _ in range(3):
        admit(OI, DESK)                                  # the guardian's monitor lane, say: counted, never capped
    for _ in range(2):
        admit(OI, RESEARCH)                              # unpaced research: counted and capped
    assert not passes(OI, RESEARCH) and passes(OI, DESK) and passes(KL, RESEARCH)   # the lane only
    weight.reset()                                       # another process sees the same count
    assert not passes(OI, RESEARCH)
    clock.t += weight_share.COUNT_WINDOW_S
    assert passes(OI, RESEARCH)


def test_a_429_on_a_count_lane_cools_that_lane_not_the_ip(clock):
    weight.record(OI, 429, {"x-mbx-used-weight-1m": "10", "retry-after": "90"}, '{"code":-1003}')
    assert passes(KL, RESEARCH) and passes(KL, DESK)      # the IP's weight budget is untouched
    weight.reset()
    assert not passes(OI, RESEARCH) and not passes(OI, DESK) and passes(OI, PROTECTIVE)
    clock.t += 91
    assert passes(OI, RESEARCH)
    weight.record(OI, 418, {"retry-after": "300"}, "")    # a ban is a ban, whatever the endpoint
    assert not passes(KL, RESEARCH) and not passes(KL, DESK)


def test_nothing_current_means_nothing_in_flight(clock, monkeypatch):
    led = weight_ledger.get()
    led.reserve(HOST, 1300.0, clock.wall)                # another process's call, not answered yet
    assert not passes(KL, RESEARCH)
    monkeypatch.setenv(weight_ledger.ENV, "off")         # the ledger goes away: no frozen in-flight weight
    assert passes(KL, RESEARCH) and weight.budget(HOST).inflight == 0.0


def test_a_corrupt_ledger_is_moved_aside_and_recreated(clock):
    weight.record(KL, 200, {"x-mbx-used-weight-1m": "5"}, "")
    led = weight_ledger.get()
    led.close()
    led.path.write_bytes(b"not a database" * 512)        # a crash with synchronous=OFF
    for p in (led.path.with_name(led.path.name + "-wal"), led.path.with_name(led.path.name + "-shm")):
        p.unlink(missing_ok=True)
    assert passes(KL, RESEARCH)                           # the guard carries on with local readings
    assert list(led.path.parent.glob(f"{led.path.name}.corrupt-*"))
    weight.record(KL, 200, {"x-mbx-used-weight-1m": "1300"}, "")
    weight.reset()
    assert not passes(KL, RESEARCH)                       # the new ledger shares again


def test_a_dropped_release_is_released_by_the_next_write(tmp_path):
    led = weight_ledger.Ledger(tmp_path / "w.db")
    rid, _ = led.reserve(HOST, 40.0, 100.0)
    other = sqlite3.connect(led.path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    with pytest.raises(weight_ledger.Busy):
        led.done(rid)                                     # contended: dropped for now
    other.execute("ROLLBACK")
    other.close()
    assert led.state(HOST, 101.0).inflight == 40.0
    led.note(HOST, 101.0, used=50)                        # the next write releases it: not counted twice
    assert led.state(HOST, 102.0).inflight == 0.0
    led.close()


def test_the_guardians_ledger_never_waits(tmp_path, monkeypatch):
    weight_ledger.fail_fast()                              # Guardian() does this (BUSY_MS restored per test)
    led = weight_ledger.Ledger(tmp_path / "w.db")
    assert led.connection().execute("PRAGMA busy_timeout").fetchone()[0] == 0
    held, go = threading.Event(), threading.Event()

    def hold():
        with led._session():
            held.set()
            go.wait(5)
    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    with pytest.raises(weight_ledger.Busy):
        led.state(HOST, 1.0)                               # no wait at all on the process lock
    go.set()
    t.join(5)
    led.close()


def test_the_gateway_start_up_reconciles_as_protective(monkeypatch):
    seen = []

    async def steps(acts, desk, gw):
        seen.append(weight.current())
        return {"ok": True}
    monkeypatch.setattr(gateway_acts, "_steps", steps)

    @contextlib.asynccontextmanager
    async def reconciling(action, wait_s):
        yield
    tr = SimpleNamespace(ex=SimpleNamespace(trade=object()), symlocks=SimpleNamespace(reconciling=reconciling))
    acts = SimpleNamespace(app=SimpleNamespace(trader=tr), n=SimpleNamespace(as_trader_alert=None))
    assert asyncio.run(gateway_acts.start_up(acts, SimpleNamespace(ctx=None), None)) == {"ok": True}
    assert seen == [PROTECTIVE]


def test_book_ticker_and_historical_trades_weights():
    assert (http.cost("/fapi/v1/ticker/bookTicker", {"symbol": "BTCUSDT"}), http.cost("/fapi/v1/ticker/bookTicker")) \
        == (2, 5)
    assert http.cost("/fapi/v1/historicalTrades", {"symbol": "BTCUSDT"}) == 200
