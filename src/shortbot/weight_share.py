"""weight.py's side of the host-wide ledger (weight_ledger.py): read the IP-wide state, share a response's news,
reserve a call's weight, and pace research.

- Best-effort and never blocking: every ledger call is bounded (weight_ledger.BUSY_MS); on contention (Busy) a write
  is dropped and a read falls back to the process's own readings. A corrupt file is moved aside and recreated. Any
  other ledger error warns once per process.
- Protective calls never wait on the ledger (weight.py). Only their limit answers (429/418/-1003) are shared, handed
  to a background thread (`note_later`): the protective call never waits for it.
- Pacing (paced research scopes: lab, shortbot.http): research weight per host is capped at PACE_WEIGHT_CAP per
  PACE_WINDOW_S across processes and threads, checked and reserved in one ledger transaction.
- Count lanes: /futures/data/* (1,000 requests/5 min/IP) and fundingRate + fundingInfo (500/5 min/IP) have their own
  request limits. Every desk/research call to them is counted in its lane, paced or not (the guardian included);
  research waits at COUNT_LANES' caps, and a 429 there cools that lane down, not the IP's weight budget.
- When the ledger is off, failing or contended, the same caps hold in-process (LocalPacer).
"""

from __future__ import annotations

import logging
import math
import queue
import sqlite3
import threading
import time
from collections import deque
from typing import Any

from . import weight_ledger
from .weight_ledger import Busy, Ledger, Pace, Shared

log = logging.getLogger("shortbot.weight")

# 900 research weight/min per host, 37.5% of fapi's 2,400; by call count: /futures/data/* "IP rate limit 1000
# requests/5min", fundingRate + fundingInfo "share 500/5min/IP"
# (https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api, read 2026-10-07)
PACE_WINDOW_S, PACE_WEIGHT_CAP = 10.0, 150.0
COUNT_WINDOW_S = 300.0
COUNT_LANES = (("/futures/data/", "futures_data", 900.0), ("/fapi/v1/fundingRate", "funding", 450.0),
               ("/fapi/v1/fundingInfo", "funding", 450.0))   # 90% of each documented limit
Ticket = tuple[Ledger, int] | None   # an admitted call's in-flight reservation, released on its answer
_warned = [False]


def _failed(what: str, e: BaseException, led: Ledger | None = None) -> None:
    if isinstance(e, Busy):
        log.debug("weight ledger %s skipped: %s", what, e)
        return
    if led is not None and isinstance(e, sqlite3.DatabaseError) and not isinstance(e, sqlite3.OperationalError):
        try:
            led.quarantine()   # malformed / not a database: a crash with synchronous=OFF
        except OSError:
            log.warning("weight ledger %s: could not move the corrupt file aside", led.path, exc_info=True)
        return
    log.log(logging.DEBUG if _warned[0] else logging.WARNING, "weight ledger %s failed: local readings only", what,
            exc_info=True)
    _warned[0] = True


def count_lane(path: str) -> str | None:
    """The count lane of an endpoint with its own request limit, or None."""
    return next((lane for prefix, lane, _ in COUNT_LANES if path.startswith(prefix)), None)


def pace_of(path: str, cost: float, research: bool, paced: bool) -> Pace | None:
    """The lane a desk/research call is counted in: its count lane (capped for research only), else the weight lane
    for a paced research scope, else none."""
    for prefix, lane, cap in COUNT_LANES:
        if path.startswith(prefix):
            return Pace(lane, 1.0, cap if research else math.inf, COUNT_WINDOW_S)
    return Pace("weight", cost, PACE_WEIGHT_CAP, PACE_WINDOW_S) if research and paced else None


class LocalPacer:
    """The pacing caps within this process, for when the ledger can't hold them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()   # held for a few list operations only
        self._spent: dict[tuple[str, str], deque[tuple[float, float]]] = {}

    def _window(self, host: str, pace: Pace, now: float) -> deque[tuple[float, float]]:
        q = self._spent.setdefault((host, pace.lane), deque())
        while q and q[0][0] <= now - pace.window_s:
            q.popleft()
        return q

    def note(self, host: str, pace: Pace, now: float) -> None:
        with self._lock:
            self._window(host, pace, now).append((now, pace.cost))

    def take(self, host: str, pace: Pace, now: float) -> str | None:
        """Admit one paced call (None) or say why it must wait."""
        with self._lock:
            q = self._window(host, pace, now)
            spent = sum(c for _, c in q)
            if spent and spent + pace.cost > pace.cap:
                return pace.full(spent)
            q.append((now, pace.cost))
            return None

    def clear(self) -> None:
        with self._lock:
            self._spent.clear()


_pacer = LocalPacer()


_later: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=64)
_worker: list[threading.Thread] = []


def _drain() -> None:
    while True:
        news = _later.get()
        try:
            if news is not None:
                note(**news)
        finally:
            _later.task_done()


def note_later(**news: Any) -> None:
    """note() on a background thread: for a protective call's limit answer, which never waits on the ledger. Dropped
    when the queue is full (the next desk/research answer shares the ban too)."""
    if not _worker or not _worker[0].is_alive():
        _worker[:] = [threading.Thread(target=_drain, name="weight-ledger", daemon=True)]
        _worker[0].start()
    try:
        _later.put_nowait(news)
    except queue.Full:
        log.debug("weight ledger: a protective limit answer was not shared (queue full)")


def flush(timeout_s: float = 2.0) -> None:
    """Wait (bounded) until note_later's queue is written: tests, and reset()."""
    end = time.monotonic() + timeout_s
    while _later.unfinished_tasks and _worker and _worker[0].is_alive() and time.monotonic() < end:
        time.sleep(0.005)


def reset() -> None:
    flush()
    _pacer.clear()
    weight_ledger.close_all()


def shared(host: str, now: float) -> tuple[Ledger | None, Shared | None]:
    """The ledger and the host's IP-wide state, or (None, None) when it is off, failing or contended."""
    led = None
    try:
        led = weight_ledger.get()
        return (led, led.state(host, now)) if led is not None else (None, None)
    except Exception as e:  # noqa: BLE001 - the guard falls back to this process's readings
        _failed("read", e, led)
        return None, None


def note(host: str, now: float, used: int | None = None, limit: int | None = None, full_until: float | None = None,
         cooldown_until: float | None = None, last_limit: str | None = None,
         lane_until: tuple[str, float] | None = None) -> None:
    """Share one response's news; dropped when the ledger is off, failing or contended."""
    led = None
    try:
        led = weight_ledger.get()
        if led is not None:
            led.note(host, now, used=used, limit=limit, full_until=full_until, cooldown_until=cooldown_until,
                     last_limit=last_limit, lane_until=lane_until)
    except Exception as e:  # noqa: BLE001 - bookkeeping must never fail a request
        _failed("write", e, led)


def reserve(led: Ledger | None, host: str, cost: float, now: float, pace: Pace | None) -> tuple[Ticket, str | None]:
    """Reserve a call's weight host-wide (and its pacing lane): (ticket, None), or (None, why it must wait)."""
    if led is not None:
        try:
            rid, why = led.reserve(host, cost, now, pace)
            if why is None and pace is not None:
                _pacer.note(host, pace, now)   # keeps the in-process history whole for a later fallback
            return ((led, rid) if rid is not None else None), why
        except Exception as e:  # noqa: BLE001
            _failed("reserve", e, led)
    return None, (_pacer.take(host, pace, now) if pace is not None else None)


def release(ticket: Ticket) -> None:
    if ticket is not None:
        try:
            ticket[0].done(ticket[1])   # contended: released by this process's next ledger write
        except Exception as e:  # noqa: BLE001
            _failed("release", e, ticket[0])
