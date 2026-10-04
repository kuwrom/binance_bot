"""One guardian runs the market-wide jobs for both desks (Settings.guardian_market, docs/DESKS.md): the live guardian
runs only its own account's jobs, and records no liquidations or market streams."""

import asyncio
import dataclasses

import pytest

from shortbot import alerts_view
from shortbot import guardian as gmod
from shortbot.config import Settings
from tests.fakes import FakeCcxt
from tests.test_guardian import guardian_with
from tests.test_guardian_loop import SLOW_JOBS, fast_guardian, noop

ACCOUNT_JOBS = {"entries", "positions", "entry_reconcile", "watch", "governor", "announcements", "margin_mode",
                "gateway", "setup_ladder", "setup_ladder_stops", "ladder", "coin_memory"}


def jobs(g) -> set[str]:
    return {job.name for ln in g._lanes().values() for job in ln.jobs}


def without_market(g):
    g.app.settings = dataclasses.replace(g.app.settings, guardian_market=False)
    return g


def test_the_flag_reads_from_the_environment(monkeypatch):
    monkeypatch.delenv("SHORTBOT_GUARDIAN_MARKET", raising=False)
    assert Settings.from_env().guardian_market
    for off in ("0", "false", "off"):
        monkeypatch.setenv("SHORTBOT_GUARDIAN_MARKET", off)
        assert not Settings.from_env().guardian_market


def test_the_market_guardian_runs_every_job():
    g = guardian_with(FakeCcxt())
    assert jobs(g) == ACCOUNT_JOBS | gmod.MARKET_JOBS
    assert set(g._lanes()) == {"protective", "watch", "monitor", "research", "scan", "coins"}


def test_an_account_guardian_runs_only_its_account_jobs():
    g = without_market(guardian_with(FakeCcxt()))
    assert jobs(g) == ACCOUNT_JOBS | {"market_feed"}   # and watches the market guardian's feed
    assert "scan" not in g._lanes()   # a lane left without jobs is dropped


def test_both_sessions_see_the_alerts_of_every_market_job():
    assert {a.removeprefix("guardian:") for a in alerts_view.MARKET_ACTORS} == gmod.MARKET_JOBS


async def test_an_account_guardian_records_no_market_data(monkeypatch, tmp_path):
    started = []

    class Recorder:
        def __init__(self, *a):
            pass

        async def run(self):
            started.append("market data")
            await asyncio.Event().wait()
    g = fast_guardian(monkeypatch)
    monkeypatch.setattr(gmod, "LiquidationRecorder", Recorder)
    monkeypatch.setattr(gmod, "StreamTask", Recorder)
    g.streams_db = tmp_path / "streams.db"   # an in-memory journal has none: give it one, so streams could start
    without_market(g)
    for name in SLOW_JOBS:
        setattr(g, name, noop)
    task = asyncio.create_task(g.run())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    names = {t.get_name() for t in g._tasks}
    assert started == [] and not {"guardian:liquidations", "guardian:streams"} & names
    assert "guardian:protective" in names and g.j.get("guardian_heartbeat")


async def test_the_listing_heads_up_comes_from_the_market_guardian_only():
    a = {"catalog": "Delisting", "title": "Binance Futures Will Delist ZZZUSDT", "url": "https://x/1"}
    live = without_market(guardian_with(FakeCcxt()))
    await live._announce(a)
    assert live.j.alerts() == []
    demo = guardian_with(FakeCcxt())
    await demo._announce(a)
    assert [x["kind"] for x in demo.j.alerts()] == ["binance_delisting"]


def test_one_guardian_per_desk(tmp_path):
    s = dataclasses.replace(guardian_with(FakeCcxt()).app.settings, db_path=tmp_path / "shortbot.db")
    fd = gmod.hold_desk(s)
    with pytest.raises(SystemExit, match="another guardian already runs the demo desk"):
        gmod.hold_desk(s)
    assert gmod.hold_desk(dataclasses.replace(s, mode="live")) is not None   # the other desk is free
    assert fd is not None and gmod.hold_desk(dataclasses.replace(s, db_path=":memory:")) is None
