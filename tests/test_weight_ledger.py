"""The host-wide weight ledger (weight_ledger.py): every process on the host shares the IP's readings, limit answers and
in-flight weight. A process is simulated by weight.reset() (its local state gone, the ledger file kept) and, once, by
a real child process. 2026-10-07: an ad-hoc script spent the IP's budget the desk's processes never saw, and its 418
ban (14 min) was news to them."""

import os
import subprocess
import sys
import textwrap

import pytest

from shortbot import weight, weight_ledger
from shortbot.weight import DESK, PROTECTIVE, RESEARCH, BudgetWait
from shortbot.weight_ledger import Ledger, Pace
from tests.fake_http import fake_clock

FAPI = "https://fapi.binance.com/fapi/v1/klines"
HOST = "fapi.binance.com"


@pytest.fixture
def clock(monkeypatch):
    weight.reset()
    yield fake_clock(monkeypatch, weight)
    weight.reset()


def seen(used, status=200, retry_after=None):
    h = {"x-mbx-used-weight-1m": str(used)}
    if retry_after is not None:
        h["retry-after"] = str(retry_after)
    weight.record(FAPI, status, h, "")


async def passes(cls, wait_s=4.0) -> bool:
    try:
        with weight.tagged(cls, wait_s=wait_s):
            weight.release(await weight.admit(FAPI, "GET"))
        return True
    except BudgetWait:
        return False


def test_two_clients_on_one_file_see_the_freshest_reading_and_the_latest_ban(tmp_path):
    a, b = Ledger(tmp_path / "w.db"), Ledger(tmp_path / "w.db")
    a.note(HOST, 100.0, used=900)
    b.note(HOST, 99.0, used=50)                     # an older reading answered late: not the freshest
    s = b.state(HOST, 101.0)
    assert (s.used, s.seen) == (900, 100.0)
    b.note(HOST, 102.0, full_until=200.0, cooldown_until=400.0, last_limit="HTTP 418")
    a.note(HOST, 103.0, full_until=150.0, cooldown_until=160.0, last_limit="HTTP 429")   # never shortens a ban
    s = a.state(HOST, 104.0)
    assert (s.full_until, s.cooldown_until) == (200.0, 400.0)
    rid, why = a.reserve(HOST, 40.0, 104.0)
    assert why is None and b.state(HOST, 105.0).inflight == 40.0
    b.done(rid)
    assert a.state(HOST, 105.0).inflight == 0.0
    a.reserve(HOST, 40.0, 104.0)                     # never answered (a crashed caller): it stops counting
    assert b.state(HOST, 104.0 + weight_ledger.INFLIGHT_TTL_S + 1).inflight == 0.0
    a.close()
    b.close()


async def test_another_process_reading_counts_here(clock):
    seen(1300)                                       # process A: 54% of the IP's budget
    weight.reset()                                   # process B: no reading of its own
    assert weight.budget(HOST).used == 0
    assert not await passes(RESEARCH) and await passes(DESK)


async def test_a_418_one_process_saw_blocks_research_and_desk_in_another_until_retry_after(clock):
    seen(2400, status=418, retry_after=840)          # the 2026-10-07 ban: 14 min
    weight.reset()
    assert await passes(PROTECTIVE)                  # protective never waits (nor reads the ledger)
    clock.t += 300
    assert not await passes(RESEARCH) and not await passes(DESK)
    clock.t += 540                                   # Retry-After is over
    assert await passes(DESK) and await passes(RESEARCH)


async def test_weight_in_flight_elsewhere_counts_before_its_answer(clock, tmp_path):
    led = weight_ledger.get()
    rid, _ = led.reserve(HOST, 1300.0, clock.wall)   # another process: 1,300 weight sent, no answer yet
    assert not await passes(RESEARCH) and await passes(DESK)
    led.done(rid)
    assert await passes(RESEARCH)


async def test_the_learned_limit_is_shared(clock):
    weight.record("https://fapi.binance.com/fapi/v1/exchangeInfo", 200, {"x-mbx-used-weight-1m": "1"},
                  '{"rateLimits":[{"rateLimitType":"REQUEST_WEIGHT","interval":"MINUTE","intervalNum":1,"limit":6000}]}')
    weight.reset()
    seen(2000)
    weight.reset()
    assert await passes(RESEARCH)                    # 2,000 of 6,000
    assert weight.budget(HOST).limit == 6000


async def test_the_guard_works_alone_when_the_ledger_is_off_or_broken(clock, monkeypatch, tmp_path):
    monkeypatch.setenv(weight_ledger.ENV, "off")
    seen(1300)
    assert not await passes(RESEARCH)                # this process's own reading still guards it
    weight.reset()
    assert await passes(RESEARCH)                    # but nothing is shared
    (tmp_path / "dir.db").mkdir()
    monkeypatch.setenv(weight_ledger.ENV, str(tmp_path / "dir.db"))   # not a database: every operation fails
    seen(100, status=429, retry_after=5)
    assert await passes(PROTECTIVE) and not await passes(RESEARCH)


def test_a_paced_lane_admits_up_to_its_cap_per_window(tmp_path):
    led, pace = Ledger(tmp_path / "w.db"), Pace("weight", 5.0, 12.0, 10.0)
    assert led.reserve(HOST, 5, 0.0, pace)[1] is None and led.reserve(HOST, 5, 1.0, pace)[1] is None
    rid, why = led.reserve(HOST, 5, 2.0, pace)
    assert rid is None and why and "10/12 weight" in why
    assert led.reserve(HOST, 5, 10.5, pace)[1] is None   # the first call left the window
    big = Pace("weight", 50.0, 12.0, 10.0)
    assert Ledger(tmp_path / "x.db").reserve(HOST, 50, 0.0, big)[1] is None   # one call over the cap: alone it goes
    led.close()


@pytest.mark.spawns_shortbot   # a real second process records the ban; this one obeys it
def test_a_ban_recorded_by_a_child_process_blocks_research_here(tmp_path, monkeypatch):
    weight.reset()
    path = tmp_path / "ledger.db"
    monkeypatch.setenv(weight_ledger.ENV, str(path))
    child = textwrap.dedent(f"""
        from shortbot import weight
        weight.record("{FAPI}", 418, {{"x-mbx-used-weight-1m": "2400", "retry-after": "600"}},
                      '{{"code":-1003,"msg":"Way too much request weight used; IP banned until 1."}}')
    """)
    env = {**os.environ, weight_ledger.ENV: str(path)}
    subprocess.run([sys.executable, "-c", child], check=True, env=env, timeout=60)
    with weight.tagged(PROTECTIVE):
        assert weight.admit_blocking(FAPI, "GET") is None
    for cls in (RESEARCH, DESK):
        with pytest.raises(BudgetWait, match="HTTP 418|2400/2400"):
            with weight.tagged(cls, wait_s=0):
                weight.admit_blocking(FAPI, "GET")
    weight.reset()
