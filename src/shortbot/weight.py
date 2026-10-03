"""Exchange weight guard (#70, docs/SCANNING.md "Weight guard spec"): research calls yield to protective ones.

Binance limits request weight per IP and host and reports the IP's usage on every response in `x-mbx-used-weight-1m`.
A 429 means over the limit; failing to back off after 429s earns a 418 IP ban (2 min to 3 days). Both answers carry
`Retry-After` in seconds (https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info,
https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md). A ban blinds the stop checks, so research
must never be the call that tips it.

- Budget per host: the latest reading against the host's exchangeInfo REQUEST_WEIGHT limit (measured 2026-10-02:
  fapi.binance.com 2,400/min, demo-fapi 6,000/min). Demo's trade host and mainnet public data are separate budgets; in
  live both clients share fapi.binance.com, so they share one. data.binance.vision is a CDN: never guarded.
- Whether the minute is fixed or rolling isn't documented: a reading is trusted for WINDOW_S, which covers both.
- Classes, set by the caller through a contextvar (ccxt needs no knowledge of them); untagged = desk:
  protective never waits; desk waits while usage > 80%; research while usage > 50% and for COOLDOWN_S (or Retry-After)
  after a 429 / 418 / -1003 on that host; tier 2 is research with its own TIER2_CAP per 15m window.
- A write (any non-GET: orders, stops, cancels) starts an order sequence: the rest of the tagged scope never waits.
- Yielding is waiting, polled every POLL_S, up to the scope's deadline (a guardian job's timeout, an MCP/CLI call's
  ENTRY_WAIT_S), then BudgetWait. A waiting call holds no lock or connection: it waits before the request is sent.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import math
import re
import time
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

log = logging.getLogger("shortbot.weight")

PROTECTIVE, DESK, RESEARCH, TIER2 = "protective", "desk", "research", "tier2"
_RANK = {TIER2: 0, RESEARCH: 1, DESK: 2, PROTECTIVE: 3}
DESK_FRAC, RESEARCH_FRAC = 0.80, 0.50
DEFAULT_LIMIT = 2400          # fapi REQUEST_WEIGHT per minute, until the host's own exchangeInfo says otherwise
WINDOW_S = 60.0               # a reading's lifetime (the limit's interval)
COOLDOWN_S = 120.0            # research yields this long after a 429 / 418 / -1003 on the host
POLL_S = 2.0
DEFAULT_WAIT_S = {DESK: 60.0, RESEARCH: 90.0, TIER2: 90.0}   # a scope without a deadline
ENTRY_WAIT_S = 90.0           # an MCP tool / CLI command / lab call as a whole
JOB_MARGIN_S = 5.0            # a guardian job's budget_wait comes this long before its timeout
# a deep score costs ~29 weight per coin (ccxt cost model, 2026-10-02; order book 500 alone is 10): 20 coins per 15m
# window is ~600, ~1.7% of a 2,400/min budget. scan_windows.tier2_weight logs what it really spends (#72).
TIER2_CAP, TIER2_WINDOW_S = 600, 900
HEADER = "x-mbx-used-weight-1m"
UNGUARDED = ("data.binance.vision",)
# the official SDK's docstrings: "The field X-MBX-USED-WEIGHT-1M in response header is not accurate from this endpoint,
# please ignore" (binance-connector-python, derivatives_trading_usds_futures rest_api.py: bookTicker, ticker price v2)
INACCURATE = ("/ticker/bookTicker", "/ticker/price")
_CODE_1003 = re.compile(r'"code"\s*:\s*-1003\b')
_RATE_LIMITS = re.compile(r'"rateLimits"\s*:\s*(\[[^\]]*\])')

# guardian lanes (guardian.py) -> class; a job named in GUARDIAN_JOBS overrides its lane
GUARDIAN_LANES = {"protective": PROTECTIVE, "watch": PROTECTIVE, "monitor": RESEARCH, "research": RESEARCH,
                  "scan": RESEARCH}   # the tiered scan (#72); its deep scores narrow themselves to TIER2
GUARDIAN_JOBS = {"governor": PROTECTIVE,       # the kill-switch drawdown trip; a halt withdraws resting entries
                 "margin_mode": PROTECTIVE}    # margin checks

_CLASS: contextvars.ContextVar[str | None] = contextvars.ContextVar("shortbot_weight_class", default=None)
_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("shortbot_weight_deadline", default=None)
_now = time.monotonic
_wall = time.time


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)   # indirection so tests run on a fake clock


class BudgetWait(Exception):
    """budget_wait: the call yielded until its deadline and was never sent."""


@dataclass
class HostBudget:
    host: str
    limit: int = DEFAULT_LIMIT
    used: int = 0
    seen: float = -math.inf            # when `used` was read (monotonic)
    full_until: float = -math.inf      # a 429/418 said the host is spent until then
    cooldown_until: float = -math.inf  # research yields until then
    last_limit: str = ""                # the last limit answer: HTTP 429 / HTTP 418 / -1003

    def usage(self, now: float) -> int:
        u = self.used if now - self.seen < WINDOW_S else 0
        return max(u, self.limit) if now < self.full_until else u

    def blocked(self, cls: str, now: float) -> str | None:
        """Why a `cls` call must wait now, or None."""
        u = self.usage(now)
        if cls == DESK:
            return f"{u}/{self.limit} weight used (> {DESK_FRAC:.0%})" if u > DESK_FRAC * self.limit else None
        if now < self.cooldown_until:
            return f"{self.last_limit} on this host, cooling down {self.cooldown_until - now:.0f}s more"
        return f"{u}/{self.limit} weight used (> {RESEARCH_FRAC:.0%})" if u > RESEARCH_FRAC * self.limit else None


_hosts: dict[str, HostBudget] = {}
_tier2 = [-1, 0.0]   # [15m window index, weight charged in it]


def reset() -> None:
    _hosts.clear()
    _tier2[:] = [-1, 0.0]


def budget(host: str) -> HostBudget:
    return _hosts.setdefault(host, HostBudget(host))


def _guarded_host(url: str) -> str | None:
    host = urlsplit(str(url)).hostname
    return None if not host or any(host == h or host.endswith("." + h) for h in UNGUARDED) else host


# ------------------------------------------------------------------ tagging
def current() -> str:
    return _CLASS.get() or DESK


def _merge(cur: str | None, new: str | None) -> str | None:
    if new is None or cur is None:
        return cur or new
    if cur == RESEARCH and new == TIER2:
        return TIER2
    return new if _RANK[new] > _RANK[cur] else cur


@contextmanager
def tagged(cls: str | None = None, wait_s: float | None = None) -> Iterator[None]:
    """Run the block as `cls` (None keeps the caller's class; untagged = desk). A nested tag never lowers an outer
    class (research may narrow to tier 2), and a nested `wait_s` only tightens the deadline. Always a scope: an order
    sequence started inside ends with the block."""
    if cls is not None and cls not in _RANK:
        raise ValueError(f"unknown weight class {cls!r}")
    dl = _DEADLINE.get()
    if wait_s is not None:
        mine = _now() + max(0.0, wait_s)
        dl = mine if dl is None else min(dl, mine)
    t_cls, t_dl = _CLASS.set(_merge(_CLASS.get(), cls)), _DEADLINE.set(dl)
    try:
        yield
    finally:
        _DEADLINE.reset(t_dl)
        _CLASS.reset(t_cls)


def class_for(lane: str, job: str) -> str | None:
    return GUARDIAN_JOBS.get(job) or GUARDIAN_LANES.get(lane)


def lane_job(lane: str, job: str, timeout_s: float) -> AbstractContextManager[None]:
    """A guardian job's scope: its lane's class, and a deadline JOB_MARGIN_S before the job's own timeout."""
    return tagged(class_for(lane, job), wait_s=max(0.0, timeout_s - JOB_MARGIN_S))


# ------------------------------------------------------------------ responses
def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    for k, v in (headers or {}).items():
        if k.lower() == name:
            return v
    return None


def _learn_limit(b: HostBudget, body: str) -> None:
    m = _RATE_LIMITS.search(body[:65536])
    if not m:
        return
    for r in json.loads(m.group(1)):
        if (r.get("rateLimitType"), r.get("interval"), r.get("intervalNum")) == ("REQUEST_WEIGHT", "MINUTE", 1):
            b.limit = int(r["limit"])


def record(url: str, status: int | None, headers: Mapping[str, str] | None, body: str | None) -> None:
    """Every response of a guarded client: its usage reading, a 429/418/-1003, an exchangeInfo limit."""
    host = _guarded_host(url)
    if host is None:
        return
    b, now, path = budget(host), _now(), urlsplit(str(url)).path
    used = _header(headers, HEADER)
    if used is not None and not path.endswith(INACCURATE):
        try:
            b.used, b.seen = int(used), now
        except ValueError:
            pass
    status = status or 0
    if status in (418, 429) or (status >= 400 and body and _CODE_1003.search(body[:512])):
        try:
            retry = float(_header(headers, "retry-after") or 0)
        except ValueError:
            retry = 0.0
        b.full_until = max(b.full_until, now + max(WINDOW_S, retry))
        b.cooldown_until = max(b.cooldown_until, now + max(COOLDOWN_S, retry))
        b.last_limit = f"HTTP {status}" if status in (418, 429) else "-1003"
        log.warning("%s answered %s (Retry-After %ss, %s/%s used): research yields %.0fs", host, b.last_limit,
                    _header(headers, "retry-after"), used, b.limit, b.cooldown_until - now)
    if path.endswith("/exchangeInfo") and body and status == 200:
        _learn_limit(b, body)


# ------------------------------------------------------------------ requests
def _tier2_blocked(cost: float) -> str | None:
    idx = int(_wall() // TIER2_WINDOW_S)
    if idx != _tier2[0]:
        _tier2[:] = [idx, 0.0]
    if _tier2[1] and _tier2[1] + cost > TIER2_CAP:
        return f"tier-2 cap: {_tier2[1]:.0f}/{TIER2_CAP} weight this 15m window"
    return None


def tier2_room() -> float:
    """Weight tier 2 may still spend in this 15m window (for the tier-2 scheduler to plan its coins)."""
    _tier2_blocked(0)
    return max(0.0, TIER2_CAP - _tier2[1])


def tier2_usage() -> tuple[int, float]:
    """(15m window index, weight tier 2 has spent in it): the scheduler measures a deep score's cost with it."""
    _tier2_blocked(0)
    return int(_tier2[0]), float(_tier2[1])


async def admit(url: str, method: str = "GET", cost: float = 1.0) -> None:
    """Called by a guarded client before it sends a request: returns when it may go, or raises BudgetWait."""
    host = _guarded_host(url)
    if host is None:
        return
    if method.upper() != "GET":
        _CLASS.set(PROTECTIVE)   # an order sequence has started: nothing after it in this scope waits
        return
    cls = current()
    if cls == PROTECTIVE:
        return
    b, start = budget(host), _now()
    deadline = _DEADLINE.get()
    if deadline is None:
        deadline = start + DEFAULT_WAIT_S[cls]
    while True:
        now = _now()
        why = b.blocked(cls, now) or (_tier2_blocked(cost) if cls == TIER2 else None)
        if why is None:
            break
        if now + POLL_S > deadline:
            raise BudgetWait(f"budget_wait: {cls} call to {host} yielded {now - start:.0f}s: {why}")
        if now == start:
            log.info("%s call to %s yields: %s", cls, host, why)
        await _sleep(POLL_S)
    if cls == TIER2:
        _tier2[1] += cost

