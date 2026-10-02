"""Weight guard policy (#70, docs/SCANNING.md "Weight guard spec"): per-host readings of x-mbx-used-weight-1m against
the host's exchangeInfo limit; protective never waits, desk waits above 80%, research above 50% and for 2 min after a
429/418/-1003; yielding is waiting (polled every ~2 s) up to a deadline, then budget_wait."""

import asyncio
import json

import pytest

from shortbot import weight
from shortbot.weight import DESK, PROTECTIVE, RESEARCH, TIER2, BudgetWait
from tests.fake_http import fake_clock, rate_limits

FAPI = "https://fapi.binance.com/fapi/v1/klines"
DEMO = "https://demo-fapi.binance.com/fapi/v1/klines"


@pytest.fixture
def clock(monkeypatch):
    weight.reset()
    yield fake_clock(monkeypatch, weight)
    weight.reset()


def seen(url, used, status=200, retry_after=None, body=""):
    h = {"x-mbx-used-weight-1m": str(used)}
    if retry_after is not None:
        h["retry-after"] = str(retry_after)
    weight.record(url, status, h, body)


async def passes(cls, url=FAPI, wait_s=10.0) -> bool:
    try:
        with weight.tagged(cls, wait_s=wait_s):
            await weight.admit(url, "GET")
        return True
    except BudgetWait:
        return False


async def test_each_class_has_its_own_threshold(clock):
    seen(FAPI, 1200)                     # 50% of 2,400: not above it
    assert await passes(RESEARCH)
    seen(FAPI, 1201)
    assert not await passes(RESEARCH) and await passes(DESK) and await passes(PROTECTIVE)
    seen(FAPI, 1921)                     # above 80%
    assert not await passes(DESK) and not await passes(None) and await passes(PROTECTIVE)   # untagged = desk


async def test_yield_waits_and_polls_until_the_reading_ages_out(clock):
    seen(FAPI, 2000)
    t0 = clock.t
    with weight.tagged(RESEARCH, wait_s=200):
        await weight.admit(FAPI, "GET")
    assert weight.WINDOW_S <= clock.t - t0 <= weight.WINDOW_S + weight.POLL_S   # waited, not dropped


async def test_budget_wait_names_the_host_and_the_reason_at_the_deadline(clock):
    seen(FAPI, 2000)
    t0 = clock.t
    with pytest.raises(BudgetWait, match=r"budget_wait.*research.*fapi\.binance\.com.*2000/2400"):
        with weight.tagged(RESEARCH, wait_s=20):
            await weight.admit(FAPI, "GET")
    assert clock.t - t0 <= 20


async def test_a_429_cools_research_down_for_two_minutes_or_retry_after(clock):
    seen(FAPI, 100, status=429, retry_after=5)
    assert await passes(PROTECTIVE)
    assert not await passes(DESK, wait_s=50)            # the host is spent until the minute is over
    clock.t += weight.WINDOW_S
    assert await passes(DESK) and not await passes(RESEARCH, wait_s=4)
    clock.t += weight.COOLDOWN_S - weight.WINDOW_S
    assert await passes(RESEARCH)
    seen(FAPI, 100, status=418, retry_after=600)       # a ban: Retry-After is longer than the cooldown
    clock.t += 300
    assert not await passes(RESEARCH) and not await passes(DESK)
    clock.t += 300
    assert await passes(RESEARCH)


async def test_a_minus_1003_body_counts_like_a_429(clock):
    weight.record(FAPI, 403, {}, json.dumps({"code": -1003, "msg": "Too many requests queued."}))
    assert not await passes(RESEARCH) and await passes(PROTECTIVE)


async def test_the_limit_comes_from_the_hosts_exchange_info(clock):
    weight.record("https://demo-fapi.binance.com/fapi/v1/exchangeInfo", 200, {"x-mbx-used-weight-1m": "1"},
                  json.dumps({"timezone": "UTC", "rateLimits": rate_limits(6000), "symbols": []}))
    seen(DEMO, 2500)
    seen(FAPI, 2500)
    assert weight.budget("demo-fapi.binance.com").limit == 6000
    assert await passes(RESEARCH, DEMO) and not await passes(RESEARCH, FAPI)


async def test_endpoints_with_an_inaccurate_header_are_ignored(clock):
    seen(FAPI, 10)
    seen("https://fapi.binance.com/fapi/v1/ticker/bookTicker", 2000)   # official SDK: "not accurate ... ignore"
    seen("https://fapi.binance.com/fapi/v2/ticker/price", 2000)
    assert weight.budget("fapi.binance.com").used == 10


async def test_the_archive_cdn_is_never_guarded(clock):
    url = "https://data.binance.vision/data/futures/um/daily/klines/BTCUSDT/1h/x.zip"
    weight.record(url, 429, {"x-mbx-used-weight-1m": "9999", "retry-after": "600"}, "")
    assert await passes(RESEARCH, url) and "data.binance.vision" not in weight._hosts


async def test_header_names_are_matched_case_insensitively(clock):
    weight.record(FAPI, 200, {"X-MBX-USED-WEIGHT-1M": "1500"}, "")
    assert weight.budget("fapi.binance.com").used == 1500


async def test_nested_tags_never_lower_an_outer_class():
    with weight.tagged(PROTECTIVE):
        with weight.tagged(RESEARCH):
            assert weight.current() == PROTECTIVE
    with weight.tagged(DESK), weight.tagged(RESEARCH):
        assert weight.current() == DESK
    with weight.tagged(RESEARCH):
        with weight.tagged(TIER2):
            assert weight.current() == TIER2      # tier 2 is research with its own cap
        with weight.tagged(PROTECTIVE):
            assert weight.current() == PROTECTIVE
    with weight.tagged(None):
        assert weight.current() == DESK
    assert weight.current() == DESK


async def test_a_write_starts_an_order_sequence_nothing_after_it_waits(clock):
    seen(FAPI, 2300)
    with weight.tagged(RESEARCH, wait_s=10):
        await weight.admit("https://fapi.binance.com/fapi/v1/order", "POST")   # writes never wait
        assert weight.current() == PROTECTIVE
        await weight.admit(FAPI, "GET")
    assert weight.current() == DESK          # scoped to the tagged block


async def test_an_inner_deadline_can_only_tighten(clock):
    seen(FAPI, 2000)
    t0 = clock.t
    with pytest.raises(BudgetWait):
        with weight.tagged(RESEARCH, wait_s=10), weight.tagged(RESEARCH, wait_s=500):
            await weight.admit(FAPI, "GET")
    assert clock.t - t0 <= 10


async def test_without_a_deadline_each_class_waits_its_default(clock):
    seen(FAPI, 2000, status=429, retry_after=600)
    t0 = clock.t
    with pytest.raises(BudgetWait):
        with weight.tagged(DESK):
            await weight.admit(FAPI, "GET")
    assert clock.t - t0 <= weight.DEFAULT_WAIT_S[DESK]


async def test_tier2_has_its_own_cap_per_15_minute_window(clock):
    with weight.tagged(TIER2, wait_s=5):
        for _ in range(int(weight.TIER2_CAP // 10)):
            await weight.admit(FAPI, "GET", cost=10)
        assert weight.tier2_room() == 0 and weight.tier2_usage()[1] == weight.TIER2_CAP
        with pytest.raises(BudgetWait, match="tier-2"):
            await weight.admit(FAPI, "GET", cost=10)
    with weight.tagged(RESEARCH, wait_s=5):
        await weight.admit(FAPI, "GET", cost=10)          # the cap is tier 2's alone
    with weight.tagged(TIER2, wait_s=weight.TIER2_WINDOW_S):
        await weight.admit(FAPI, "GET", cost=10)          # waits for the next window
    assert weight.tier2_room() == weight.TIER2_CAP - 10


async def test_a_waiting_call_lets_other_calls_run(clock):
    seen(FAPI, 2000)
    order = []

    async def research():
        with weight.tagged(RESEARCH, wait_s=100):
            await weight.admit(FAPI, "GET")
        order.append("research")

    async def protective():
        with weight.tagged(PROTECTIVE):
            await weight.admit(FAPI, "GET")
        order.append("protective")
    await asyncio.gather(research(), protective())
    assert order == ["protective", "research"]


def test_guardian_jobs_get_the_lane_class_with_a_margin_before_their_timeout():
    assert weight.class_for("protective", "entries") == PROTECTIVE
    assert weight.class_for("watch", "watch") == PROTECTIVE
    assert weight.class_for("monitor", "governor") == PROTECTIVE      # kill-switch trip, halt withdraws entries
    assert weight.class_for("monitor", "margin_mode") == PROTECTIVE
    assert weight.class_for("monitor", "social") == RESEARCH
    assert weight.class_for("research", "metrics") == RESEARCH
    assert weight.class_for("elsewhere", "x") is None


def test_every_guardian_lane_and_job_has_a_class():
    from tests.fakes import FakeCcxt
    from tests.test_guardian import guardian_with
    lanes = guardian_with(FakeCcxt())._lanes()
    assert set(lanes) <= set(weight.GUARDIAN_LANES), "map the new lane in weight.GUARDIAN_LANES"
    unmapped = [(ln, j.name) for ln, lane in lanes.items() for j in lane.jobs if weight.class_for(ln, j.name) is None]
    assert not unmapped
    assert set(weight.GUARDIAN_JOBS) <= {j.name for lane in lanes.values() for j in lane.jobs}   # no stale names
