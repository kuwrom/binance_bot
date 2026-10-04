"""An intent run for the guardian inside a process that serves sessions too (#36 step 6, origin.py, GATEWAY.md §4.1):
the .env baseline limits and no experiment, the guardian's alert sink, a 5 s symbol-lock wait. Outside it, nothing
changes."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from shortbot import origin, symlock
from shortbot.config import RiskLimits, Settings
from shortbot.exchange import Exchanges
from shortbot.journal import Journal
from shortbot.symlock import SymbolBusy, SymbolLocks
from shortbot.trading import Trader
from tests.fakes import FakeCcxt, FakeEx


async def test_limits_are_the_baseline_and_no_experiment(tmp_path):
    ex = Exchanges(Settings("demo", "", "", tmp_path / "j.db", "", ""))
    try:
        ex.params = SimpleNamespace(effective=lambda: RiskLimits(max_leverage=5), experiment=lambda: "lev5",
                                    active=True)
        assert (ex.limits.max_leverage, ex.experiment) == (5, "lev5")
        with origin.guardian(None):
            assert (ex.limits.max_leverage, ex.experiment) == (RiskLimits().max_leverage, None)

            async def inner():   # a task started inside inherits it
                return ex.limits.max_leverage
            assert await asyncio.create_task(inner()) == RiskLimits().max_leverage
        assert ex.limits.max_leverage == 5
    finally:
        await ex.close()


async def test_alerts_go_to_the_guardians_sink():
    got = []

    def sink(name):
        async def send(kind, msg, symbol, urgent):
            got.append((name, kind, urgent))
        return send
    tr = Trader(FakeEx(FakeCcxt()), Journal(":memory:"), alert=sink("session"))
    await tr.alert("stop_failed", "x", "QNT", True)
    with origin.guardian(sink("guardian")):
        await tr.alert("action_fill", "y", "QNT")
    with origin.guardian(None):   # no sink of its own: the Trader's
        await tr.alert("action_fill", "z", "QNT")
    assert got == [("session", "stop_failed", True), ("guardian", "action_fill", False),
                   ("session", "action_fill", False)]


async def test_a_busy_symbol_is_skipped_after_the_guardian_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(symlock, "GUARDIAN_WAIT_S", 0.05)
    held, other = SymbolLocks(tmp_path), SymbolLocks(tmp_path, wait_s=0.4)   # two holders: two open files
    async with held.hold("QNT/USDT:USDT", "session close"):
        t0 = time.monotonic()
        with origin.guardian(None), pytest.raises(SymbolBusy):
            async with other.hold("QNT/USDT:USDT", "guardian plan"):
                pytest.fail("entered a held lock")
        assert time.monotonic() - t0 < 0.3
        t0 = time.monotonic()
        with pytest.raises(SymbolBusy):   # a session's intent waits its own time
            async with other.hold("QNT/USDT:USDT", "session move"):
                pytest.fail("entered a held lock")
        assert time.monotonic() - t0 >= 0.35
        with origin.guardian(None), pytest.raises(SymbolBusy):   # an explicit wait is kept
            async with other.hold("QNT/USDT:USDT", "guardian", wait_s=0.2):
                pytest.fail("entered a held lock")
