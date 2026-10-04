"""Market clock: the moment the pipeline believes it is. Live it is the wall clock (the default, so nothing
changes); a replay pins it (backtest/replay_*.py) so the scanner, signals and DataHub stores see history as of that
moment and never after it. A ContextVar, so asyncio tasks created inside `at(...)` inherit it.

    from shortbot import clock
    with clock.at(ts_ms):
        await scan_shorts(replay_exchange, ...)

Market-time reads in signals/, market/ and scanner.py go through here; tests/test_clock.py fails on a direct
time.time()/datetime.now() there unless the line says `# wall-clock` (cache TTLs, rate limiters, recorders)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

_PINNED: ContextVar[float | None] = ContextVar("shortbot_clock", default=None)


def now_s() -> float:
    pinned = _PINNED.get()
    return time.time() if pinned is None else pinned


def now_ms() -> int:
    return int(now_s() * 1000)


def now_dt() -> datetime:
    return datetime.fromtimestamp(now_s(), UTC)


@contextmanager
def at(ts_ms: int | float) -> Iterator[None]:
    token = _PINNED.set(float(ts_ms) / 1000)
    try:
        yield
    finally:
        _PINNED.reset(token)
