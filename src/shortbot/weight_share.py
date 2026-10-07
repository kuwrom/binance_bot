"""weight.py's side of the host-wide ledger (weight_ledger.py): read the IP-wide state, share a response's news,
reserve a call's weight, and pace research.

- Best-effort and never blocking: every ledger call is bounded (weight_ledger.BUSY_MS); on contention (Busy) a write
  is dropped and a read falls back to the process's own readings. Any other ledger error warns once per process.
- Protective calls never come here (weight.py): not before they are sent, not when their answer is recorded.
- Pacing (paced research scopes: lab, shortbot.http): research weight per host is capped at PACE_WEIGHT_CAP per
  PACE_WINDOW_S across processes and threads, checked and reserved in one ledger transaction. The /futures/data and
  funding-rate endpoints have their own per-IP request limits, so they are paced by call count. When the ledger is off,
  failing or contended, the same caps hold in-process (LocalPacer), so a script's loop still can't burst.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

from . import weight_ledger
from .weight_ledger import Busy, Ledger, Pace, Shared

log = logging.getLogger("shortbot.weight")

# 900 research weight/min per host, 37.5% of fapi's 2,400; by call count: /futures/data/* "IP rate limit 1000
# requests/5min", fundingRate + fundingInfo "share 500/5min/IP"
# (https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api, read 2026-10-07)
PACE_WINDOW_S, PACE_WEIGHT_CAP = 10.0, 150.0
PACE_LANES = (("/futures/data/", "futures_data", 30.0), ("/fapi/v1/fundingRate", "funding", 15.0),
              ("/fapi/v1/fundingInfo", "funding", 15.0))
Ticket = tuple[Ledger, int] | None   # an admitted call's in-flight reservation, released on its answer
_warned = [False]


def _failed(what: str, e: BaseException) -> None:
    if isinstance(e, Busy):
        log.debug("weight ledger %s skipped: %s", what, e)
        return
    log.log(logging.DEBUG if _warned[0] else logging.WARNING, "weight ledger %s failed: local readings only", what,
            exc_info=True)
    _warned[0] = True


def pace_of(path: str, cost: float) -> Pace:
    for prefix, lane, cap in PACE_LANES:
        if path.startswith(prefix):
            return Pace(lane, 1.0, cap, PACE_WINDOW_S)
    return Pace("weight", cost, PACE_WEIGHT_CAP, PACE_WINDOW_S)


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


def reset() -> None:
    _pacer.clear()
    weight_ledger.close_all()


def shared(host: str, now: float) -> tuple[Ledger | None, Shared | None]:
    """The ledger and the host's IP-wide state, or (None, None) when it is off, failing or contended."""
    try:
        led = weight_ledger.get()
        return (led, led.state(host, now)) if led is not None else (None, None)
    except Exception as e:  # noqa: BLE001 - the guard falls back to this process's readings
        _failed("read", e)
        return None, None


def note(host: str, now: float, used: int | None, limit: int | None, full_until: float | None,
         cooldown_until: float | None, last_limit: str | None) -> None:
    """Share one response's news; dropped when the ledger is off, failing or contended."""
    try:
        led = weight_ledger.get()
        if led is not None:
            led.note(host, now, used=used, limit=limit, full_until=full_until, cooldown_until=cooldown_until,
                     last_limit=last_limit)
    except Exception as e:  # noqa: BLE001 - bookkeeping must never fail a request
        _failed("write", e)


def reserve(led: Ledger | None, host: str, cost: float, now: float, pace: Pace | None) -> tuple[Ticket, str | None]:
    """Reserve a call's weight host-wide (and its pacing lane): (ticket, None), or (None, why it must wait)."""
    if led is not None:
        try:
            rid, why = led.reserve(host, cost, now, pace)
            if why is None and pace is not None:
                _pacer.note(host, pace, now)   # keeps the in-process history whole for a later fallback
            return ((led, rid) if rid is not None else None), why
        except Exception as e:  # noqa: BLE001
            _failed("reserve", e)
    return None, (_pacer.take(host, pace, now) if pace is not None else None)


def release(ticket: Ticket) -> None:
    if ticket is not None:
        try:
            ticket[0].done(ticket[1])
        except Exception as e:  # noqa: BLE001
            _failed("release", e)
