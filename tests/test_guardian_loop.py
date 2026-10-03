"""Guardian lanes: the protective lane (fills, stops) never waits on slow jobs, escalates failure streaks and
writes the heartbeat only when protection actually worked; background tasks are supervised and cancelled."""

import asyncio
import time

import ccxt
import pytest

from shortbot import guardian as gmod
from shortbot import guardian_loop as loop_mod
from shortbot.guardian_loop import KV_LAST_RUN, Job, Lane, supervised
from shortbot.journal import Journal
from shortbot.notify import Notifier
from tests.fakes import SYM, FakeCcxt, FakeEx
from tests.test_guardian import SETTINGS, guardian_with, kinds, open_trade

# the scan lane's jobs run for real: they must never block the loop (#78; test_scan_lane_lag.py on real-sized data)
SLOW_JOBS = ("watch_positions", "check_governor", "check_announcements", "record_social", "research_refresh",
             "housekeeping", "record_metrics", "label_research", "refresh_calendar", "market_map", "sweep_study",
             "refresh_supply", "ladder_shadow", "laggard_study")


def lane_with(*jobs, journal=None, **kw):
    j = journal or Journal(":memory:")
    return Lane("t", list(jobs), Notifier(SETTINGS, j), FakeEx(FakeCcxt()), j, tick_s=0.01, **kw)


def lane_kinds(ln):
    return [a["kind"] for a in ln.j.alerts(100)]


async def noop():
    return None


class NoStream:
    """Stands in for the liquidation websocket."""

    def __init__(self, store):
        pass

    async def run(self):
        await asyncio.Event().wait()


def fast_guardian(monkeypatch, research_delay=0.0):
    """A guardian on 10 ms ticks whose slow jobs are no-ops: for real they reach the network (announcements, social,
    calendar, supply, market map) or spawn the weekly refresh (#80). A test sets its own over them."""
    monkeypatch.setattr(gmod, "TICK_S", 0.01)
    monkeypatch.setattr(gmod, "RESEARCH_START_DELAY_S", research_delay)
    monkeypatch.setattr(gmod, "PROTECTIVE", {"entries": (0.01, 5), "positions": (0.01, 5)})
    monkeypatch.setattr(gmod, "LiquidationRecorder", NoStream)
    g = guardian_with(FakeCcxt())
    for name in SLOW_JOBS:
        setattr(g, name, noop)
    return g


async def test_hung_slow_jobs_never_delay_the_protective_lane(monkeypatch):
    g = fast_guardian(monkeypatch)
    passes, hung, cancelled = [], [], []

    async def entries():
        passes.append(time.time())

    def hang(name):
        async def fn():
            hung.append(name)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(name)
        return fn
    g.check_entries = entries
    for name in SLOW_JOBS:
        setattr(g, name, hang(name))
    task = asyncio.create_task(g.run())
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(passes) >= 10 and g.j.get("guardian_heartbeat")
    # nothing blocked the loop either: a 0.2 s stall still leaves ~12 passes, so the count alone can't see it (#78);
    # passes come every ~13 ms, at worst ~45 ms with every core busy
    assert max(b - a for a, b in zip(passes, passes[1:])) < 0.1
    assert {"watch_positions", "check_governor", "record_metrics"} <= set(hung)   # one hung job per slow lane
    assert set(g.lanes["scan"].started) == {"scan", "cadence_shadow", "mtf_views"}   # ran for real, not hung
    assert sorted(cancelled) == sorted(hung) and all(t.done() for t in g._tasks)   # nothing left running


async def test_research_lane_waits_for_the_first_protective_pass(monkeypatch):
    g = fast_guardian(monkeypatch, research_delay=0.05)
    order = []

    async def entries():
        order.append("entries")

    async def metrics():
        order.append("metrics")
    g.check_entries = entries
    for name in SLOW_JOBS:
        setattr(g, name, noop)
    g.record_metrics = metrics
    task = asyncio.create_task(g.run())
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert order[0] == "entries" and "metrics" in order


async def test_protective_failures_escalate_once_then_recover():
    script = [RuntimeError("RequestTimeout")] * 4 + [None]

    async def positions():
        err = script.pop(0)
        if err:
            raise err
    ln = lane_with(Job("positions", positions, 0, 5), escalate_after=3)
    for _ in range(5):
        await ln.tick()
    k = lane_kinds(ln)
    assert k.count("guardian_failing:positions") == 1 and k.count("guardian_recovered:positions") == 1
    assert k.count("guardian_error:positions") == 1   # the per-error alert keeps its cooldown


async def test_guardian_heartbeat_stays_stale_while_positions_fail():
    """Only a positions pass that verified the book stamps it (#61): a lane tick of good fill polls doesn't."""
    g = guardian_with(FakeCcxt())

    async def boom():
        raise RuntimeError("RequestTimeout")
    g.check_positions = boom
    await g._lanes()["protective"].tick()
    await g._lanes()["protective"].tick()
    assert g.j.get("guardian_heartbeat") is None
    del g.check_positions   # the real pass, over a flat account
    await g._lanes()["protective"].tick()
    assert g.j.get("guardian_heartbeat")


async def test_clock_drift_resyncs_and_retries_without_an_error_alert():
    calls = []

    async def positions():
        calls.append(1)
        if len(calls) == 1:
            raise ccxt.InvalidNonce('binanceusdm {"code":-1021,"msg":"Timestamp outside of the recvWindow"}')
    ln = lane_with(Job("positions", positions, 60, 5), escalate_after=3)
    await ln.tick()
    await ln.tick()   # retried at once, not 60 s later
    assert len(calls) == 2 and ln.ex.resyncs == 1 and not lane_kinds(ln)


async def test_failing_resync_or_alerts_never_kill_a_lane():
    async def drift():
        raise ccxt.InvalidNonce("-1021")

    async def broken(*a, **k):
        raise RuntimeError("database is locked")
    ln = lane_with(Job("positions", drift, 0, 5), escalate_after=1)
    ln.ex.resync_time = broken
    ln.n.send = broken
    await ln.tick()
    ln.jobs[0].fn = noop
    await ln.tick()   # the recovery alert raises: still no crash


async def test_timeout_counts_as_a_failure():
    async def hang():
        await asyncio.Event().wait()
    ln = lane_with(Job("positions", hang, 0, 0.01), escalate_after=3)
    await ln.tick()
    a = ln.j.alerts(5)[0]
    assert a["kind"] == "guardian_error:positions" and "timed out" in a["message"]
    assert ln.streak.fails == {"positions": 1}


async def test_slow_jobs_remember_their_last_successful_run_across_restarts():
    j, runs = Journal(":memory:"), []

    async def daily():
        runs.append("sweeps")

    async def flaky_daily():
        runs.append("supply")
        if runs.count("supply") == 1:
            raise RuntimeError("coingecko down")

    async def fast():
        runs.append("fast")

    def lane():   # a restart = a new lane on the same journal
        return lane_with(Job("sweeps", daily, 86400, 5), Job("supply", flaky_daily, 86400, 5),
                         Job("market_map", fast, 600, 5), journal=j)
    first = lane()
    await first.tick()
    await first.tick()   # in process, the failed daily job waits its full period: no spinning
    await lane().tick()  # after a restart only the failed one is retried (fills the research gap)
    assert runs == ["sweeps", "supply", "fast", "supply", "fast"]
    assert j.get(KV_LAST_RUN + "sweeps") and j.get(KV_LAST_RUN + "supply")
    assert j.get(KV_LAST_RUN + "market_map") is None


async def test_trigger_runs_a_job_now():
    runs = []

    async def entries():
        runs.append(1)
    ln = lane_with(Job("entries", entries, 3600, 5))
    ln.tick_s = 60
    task = asyncio.create_task(ln.run())
    await asyncio.sleep(0.02)
    ln.trigger("entries")   # e.g. a user-data stream fill event
    await asyncio.sleep(0.02)
    task.cancel()
    assert runs == [1, 1]


async def test_a_burst_of_triggers_is_one_run_and_never_sooner_than_the_gap_after_the_last(caplog):
    caplog.set_level("INFO", "shortbot.guardian")
    runs, seen = [], []

    async def positions():
        runs.append(time.time())
        seen.append(ln.triggered("positions"))
    ln = lane_with(Job("positions", positions, 60, 5))
    await ln.tick()                       # on schedule
    t0 = time.monotonic()
    for i in range(5):                    # a burst of events about one stop-out
        ln.trigger("positions", why=f"ALGO_UPDATE stop FINISHED #{i}", at=t0, min_gap_s=0.05)
    assert not await ln.tick()            # inside the gap after the scheduled run
    await asyncio.sleep(0.06)
    assert await ln.tick() and not await ln.tick()
    assert len(runs) == 2 and runs[1] - runs[0] >= 0.05 and seen == [False, True]
    st = ln.trigger_stats["positions"]
    assert st["runs"] == 1 and st["events"] == 5 and 0.05 <= st["delay_max_s"] < 1 and st["ahead_max_s"] > 55
    assert "ALGO_UPDATE stop FINISHED #0" in caplog.text and "ahead of its schedule" in caplog.text
    ln.trigger("nope", why="unknown job")   # a job this lane doesn't run: ignored
    assert not await ln.tick()


async def test_lane_logs_when_the_event_loop_was_blocked(monkeypatch, caplog):
    monkeypatch.setattr(loop_mod, "LAG_WARN_S", 0.02)
    ln = lane_with(Job("entries", noop, 0, 5))
    task = asyncio.create_task(ln.run())
    await asyncio.sleep(0.005)
    time.sleep(0.06)   # noqa: ASYNC251 - synchronous work elsewhere in the process, on purpose
    await asyncio.sleep(0.02)
    task.cancel()
    assert "blocked the event loop" in caplog.text


async def test_a_dying_task_is_alerted_and_restarted():
    j = Journal(":memory:")
    runs = []

    async def body():
        runs.append(1)
        if len(runs) < 3:
            raise RuntimeError("bug")
        await asyncio.Event().wait()
    task = asyncio.create_task(supervised("protective", body, Notifier(SETTINGS, j), restart_s=0))
    await asyncio.sleep(0.05)
    task.cancel()
    assert len(runs) == 3 and [a["kind"] for a in j.alerts(10)] == ["guardian_task_failed:protective"]


async def test_a_stray_cancelled_error_restarts_the_task_but_a_real_cancel_stops_it():
    j = Journal(":memory:")
    runs = []

    async def body():
        runs.append(1)
        if len(runs) == 1:
            raise asyncio.CancelledError()   # e.g. awaiting an inner task someone else cancelled
        await asyncio.Event().wait()
    task = asyncio.create_task(supervised("watch", body, Notifier(SETTINGS, j), restart_s=0))
    await asyncio.sleep(0.02)
    assert len(runs) == 2 and [a["kind"] for a in j.alerts(10)] == ["guardian_task_failed:watch"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled() and len(runs) == 2


async def test_watch_lane_escalates_silent_squeeze_checks_without_touching_the_heartbeat(monkeypatch):
    monkeypatch.setattr(gmod, "WATCH", {"watch": (0, 5)})
    g = guardian_with(FakeCcxt())

    async def boom():
        raise RuntimeError("RequestTimeout")
    g.watch_positions = boom
    watch = g._lanes()["watch"]
    for _ in range(4):
        await watch.tick()
    watch.jobs[0].fn = noop
    await watch.tick()
    rows = [(a["kind"], a["message"]) for a in g.j.alerts(100)]
    failing = [m for k, m in rows if k == "guardian_failing:watch"]
    assert len(failing) == 1 and "squeeze" in failing[0].lower()
    assert [k for k, _ in rows].count("guardian_recovered:watch") == 1
    assert g.j.get("guardian_heartbeat") is None   # only the protective lane beats


async def test_social_task_failure_is_alerted_not_lost():
    g = guardian_with(FakeCcxt())

    async def boom():
        raise RuntimeError("square throttled")
    g.ex.coin_perps = boom
    await g.record_social()
    await g._social_task
    assert "guardian_error:social" in kinds(g)


async def test_stop_pass_skips_alert_feeds_and_watch_covers_each_short():
    g = guardian_with(FakeCcxt())
    await open_trade(g)
    seen = []

    async def squeeze(sym):
        seen.append(("squeeze", sym))

    async def news(sym):
        if sym.startswith("A/"):
            raise RuntimeError("feed down")
        seen.append(("news", sym))
    g._squeeze_and_funding, g._news = squeeze, news
    await g.check_positions()
    assert seen == []   # the stop pass no longer waits on public data or news feeds

    async def three():
        return [{"symbol": "A/USDT:USDT", "side": "short"}, {"symbol": "B/USDT:USDT", "side": "short"},
                {"symbol": "C/USDT:USDT", "side": "long"}]
    g.app.trader.raw_positions = three
    await g.watch_positions()
    assert seen == [("squeeze", "A/USDT:USDT"), ("squeeze", "B/USDT:USDT"), ("news", "B/USDT:USDT")]
    assert "position_watch_error" in kinds(g) and "position_check_error" not in kinds(g)


async def test_one_symbol_failing_repeatedly_escalates_then_recovers():
    g = guardian_with(FakeCcxt())
    await open_trade(g)
    left, real = [4], g._run_plan

    async def flaky(trade, p):   # ONDO: one symbol's check kept hitting -1021 while the pass "succeeded"
        if left[0]:
            left[0] -= 1
            raise ccxt.InvalidNonce("-1021")
        return await real(trade, p)
    g._run_plan = flaky
    for _ in range(5):
        await g.check_positions()
    rows = [(a["kind"], a["symbol"]) for a in g.j.alerts(100)]
    assert rows.count(("guardian_failing:position", SYM)) == 1
    assert rows.count(("guardian_recovered:position", SYM)) == 1
    assert not g.pos_streak.fails
