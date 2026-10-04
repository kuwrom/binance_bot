"""One guardian runs the market-wide jobs for both desks (Settings.guardian_market, docs/DESKS.md): the live guardian
runs only its own account's jobs, and records no liquidations or market streams."""

import asyncio
import dataclasses

import pytest

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
    assert jobs(g) == ACCOUNT_JOBS
    assert "scan" not in g._lanes()   # a lane left without jobs is dropped


async def test_an_account_guardian_records_no_market_data(monkeypatch):
    started = []

    class Recorder:
        def __init__(self, *a):
            pass

        async def run(self):
            started.append("liquidations")
            await asyncio.Event().wait()
    g = fast_guardian(monkeypatch)
    monkeypatch.setattr(gmod, "LiquidationRecorder", Recorder)
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
