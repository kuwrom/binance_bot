"""The live desk's market data comes from the other guardian (docs/DESKS.md): the demo guardian runs the market-wide
jobs (market map and its pause veto, scans, liquidations, streams, calendar) for both desks, and a guardian started
with SHORTBOT_GUARDIAN_MARKET=0 runs none. While that guardian is down, the live desk's market-pause veto reads
nothing (entry_gates: no fresh map, no pause) and its scans go stale, so the account guardian checks the feed and
alerts urgently; protection of live positions doesn't depend on it."""

from __future__ import annotations

import time
from typing import Any

from .killswitch import HEARTBEAT_KEY
from .market.market_map import cached

STALE_S = 900          # the market guardian's heartbeat (its positions pass, every 60 s) or map (every 600 s)
MAP_MAX_AGE_S = 1800   # entry_gates' own freshness window for the pause veto


def problem(j: Any, now: float | None = None) -> str | None:
    """Why the market feed can't be trusted now, or None. The market guardian is demo's: its heartbeat is the bare
    key (Journal.mode_key)."""
    raw = j.get(HEARTBEAT_KEY)
    try:
        age = (now or time.time()) - float(raw) if raw is not None else None
    except ValueError:
        age = None
    if age is None or age > STALE_S:
        seen = "never seen" if age is None else f"{age / 60:.0f} min old"
        return (f"the market guardian (demo's `guardian`) heartbeat is {seen}: the market map, its pause veto, scans, "
                "liquidations and streams aren't being updated for this desk. Restart it (docker compose up -d "
                "guardian); live positions stay protected by this guardian")
    if cached(j, MAP_MAX_AGE_S) is None:
        return (f"no market map newer than {MAP_MAX_AGE_S // 60} min although the market guardian beats: the "
                "market-pause veto reads nothing on this desk. Check `docker compose logs guardian` (market_map job)")
    return None
