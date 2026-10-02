"""Guardian scheduling. Jobs are grouped in lanes: jobs in one lane run one after another, each time-boxed; lanes
run concurrently, so a hung job only ever delays its own lane (never the protective one). Failures are alerted per
job; on lanes that escalate (protective, watch) a streak sends one urgent alert and its end is announced. Slow jobs
remember their last successful run in kv, so a restart doesn't re-run the daily studies (but retries failed ones).

asyncio can't pre-empt synchronous work (CPU-heavy steps, a SQLite busy-wait): that still stalls every lane, which
is why a lane logs when it wakes late."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import audit, weight
from .stops import is_clock_drift

log = logging.getLogger("shortbot.guardian")
ESCALATE_AFTER_FAILS = 3
ERROR_COOLDOWN_S = 1800
PERSIST_EVERY_S = 3600       # jobs at least this slow keep their last run across restarts
KV_LAST_RUN = "guardian_last_run:"
LAG_WARN_S = 5.0             # a lane waking this late means something blocked the event loop
RESTART_S = 30


@dataclass
class Job:
    name: str
    fn: Callable[[], Awaitable[Any]]
    every_s: float
    timeout_s: float


async def send_safe(n, kind: str, message: str, symbol: str | None = None, **kw: Any) -> None:
    """Alerting must never take down the loop that is reporting a failure (a locked DB, a Telegram outage)."""
    try:
        await n.send(kind, message, symbol, **kw)
    except Exception:  # noqa: BLE001
        log.exception("alert %s could not be sent", kind)


class Streak:
    """Consecutive failures per key: escalate once when a streak reaches `after`, recover on the next success."""

    def __init__(self, after: int | None = ESCALATE_AFTER_FAILS):
        self.after = after
        self.fails: dict[str, int] = {}
        self.since: dict[str, float] = {}

    def fail(self, key: str) -> bool:
        """True exactly once per streak: when it reaches `after`."""
        self.since.setdefault(key, time.time())
        n = self.fails[key] = self.fails.get(key, 0) + 1
        return n == self.after

    def ok(self, key: str) -> tuple[int, float] | None:
        """(failures, seconds) when this success ends an escalated streak."""
        n, t0 = self.fails.pop(key, 0), self.since.pop(key, None)
        return (n, time.time() - t0) if self.after and t0 and n >= self.after else None

    def forget(self, keys) -> None:
        for k in list(keys):
            self.fails.pop(k, None)
            self.since.pop(k, None)

    def started(self, key: str) -> str:
        return datetime.fromtimestamp(self.since.get(key, time.time()), timezone.utc).strftime("%H:%M UTC")


class Lane:
    def __init__(self, name: str, jobs: list[Job], notifier, ex, journal, tick_s: float = 1.0,
                 escalate_after: int | None = None, escalate_note: dict[str, str] | None = None):
        self.name, self.jobs, self.n, self.ex, self.j = name, jobs, notifier, ex, journal
        self.tick_s = tick_s
        self.streak = Streak(escalate_after)
        self.escalate_note = escalate_note or {}   # job -> what is and isn't happening while it keeps failing
        self.last = {job.name: self._seed(job) for job in jobs}
        self._wake = asyncio.Event()

    def _seed(self, job: Job) -> float:
        if job.every_s < PERSIST_EVERY_S:
            return 0.0
        try:
            return min(float(self.j.get(KV_LAST_RUN + job.name) or 0), time.time())
        except Exception:  # noqa: BLE001 - unknown: run it now
            log.exception("last run of %s unreadable", job.name)
            return 0.0

    def trigger(self, name: str) -> None:
        """Run a job at the next tick, now (hook for event-driven checks, e.g. a user-data stream fill)."""
        self.last[name] = 0.0
        self._wake.set()

    async def run(self, start_after_s: float = 0.0) -> None:
        await asyncio.sleep(start_after_s)
        while True:
            self._wake.clear()
            await self.tick()
            t0 = time.monotonic()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.tick_s)
            late = time.monotonic() - t0 - self.tick_s
            if late > LAG_WARN_S:
                log.warning("%s lane woke %.1fs late: synchronous work blocked the event loop", self.name, late)

    async def tick(self) -> bool:
        """Run every due job once. True if any ran."""
        ran = False
        for job in self.jobs:
            if time.time() - self.last[job.name] < job.every_s:
                continue
            self.last[job.name] = time.time()
            ran = True
            await self._run(job)
        return ran

    def failing(self, name: str) -> bool:
        """Is this job in a failure streak (its last run failed)?"""
        return name in self.streak.fails

    async def _run(self, job: Job) -> None:
        try:   # a hung await (socket, DB lock) only ever holds up this lane
            # what it journals is attributed to the job (#24); its exchange calls wait their turn by lane (#70)
            with audit.acting(f"guardian:{job.name}"), weight.lane_job(self.name, job.name, job.timeout_s):
                await asyncio.wait_for(job.fn(), job.timeout_s)
        except Exception as e:  # noqa: BLE001  (incl. TimeoutError from wait_for)
            await self._failed(job, e)
        else:
            r = self.streak.ok(job.name)
            if r:
                await send_safe(self.n, f"guardian_recovered:{job.name}", f"{job.name} OK again after {r[0]} "
                                f"failures ({r[1] / 60:.0f} min)", cooldown_s=0)
            if job.every_s >= PERSIST_EVERY_S:   # successes only: a restart retries a failed daily job
                try:
                    self.j.set(KV_LAST_RUN + job.name, str(self.last[job.name]))
                except Exception:  # noqa: BLE001
                    log.exception("could not store the last run of %s", job.name)

    async def _failed(self, job: Job, e: Exception) -> None:
        escalate = self.streak.fail(job.name)
        why = f"timed out after {job.timeout_s:g}s" if isinstance(e, TimeoutError) else f"{type(e).__name__}: {e}"
        if is_clock_drift(e):   # the Mac's clock drifts after sleep (-1021): resync and retry next tick, no alert
            self.last[job.name] = 0.0
            log.warning("clock drift (-1021) in %s: resyncing time", job.name)
            try:
                await self.ex.resync_time()
            except Exception:  # noqa: BLE001
                log.exception("time resync failed")
        else:
            log.error("%s failed: %s", job.name, why, exc_info=not isinstance(e, TimeoutError))
            await send_safe(self.n, f"guardian_error:{job.name}", why, cooldown_s=ERROR_COOLDOWN_S)
        if escalate:
            await send_safe(self.n, f"guardian_failing:{job.name}", f"{job.name} failed {self.streak.after}x in a "
                            f"row since {self.streak.started(job.name)} (last: {why}). "
                            f"{self.escalate_note.get(job.name, '')}".strip(),
                            urgent=True, cooldown_s=0)


async def supervised(name: str, run: Callable[[], Awaitable[Any]], notifier, restart_s: float = RESTART_S) -> None:
    """Keep a long-running task alive: if it dies (a bug: lanes catch job failures), log, alert and restart it."""
    while True:
        try:
            await run()
            log.error("%s task returned; restarting", name)
        except (Exception, asyncio.CancelledError) as e:  # noqa: BLE001
            current = asyncio.current_task()
            if isinstance(e, asyncio.CancelledError) and current and current.cancelling():
                raise   # a real cancel (shutdown); a stray one (e.g. from an inner task) is a death like any other
            log.exception("%s task died", name)
            await send_safe(notifier, f"guardian_task_failed:{name}", f"{type(e).__name__}: {e}; restarting in "
                            f"{restart_s:g}s", urgent=True, cooldown_s=ERROR_COOLDOWN_S)
        await asyncio.sleep(restart_s)


async def guarded(name: str, coro: Awaitable[Any], notifier) -> Any:
    """A one-shot background coroutine whose exception is logged and alerted instead of lost with the task. Returns
    its result, or None when it failed."""
    try:
        return await coro
    except Exception as e:  # noqa: BLE001
        log.exception("%s failed", name)
        await send_safe(notifier, f"guardian_error:{name}", f"{type(e).__name__}: {e}", cooldown_s=ERROR_COOLDOWN_S)
