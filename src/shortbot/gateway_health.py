"""Is the order gateway up? (#36 step 4, docs/GATEWAY.md §3.4). The gateway posts kv gateway_heartbeat (per mode)
every few seconds; with SHORTBOT_GATEWAY on, the session brief shows one line and the guardian alerts a stale one.
It gates nothing: a write learns the gateway is down when it can't connect or isn't greeted (gateway_client.py),
which is exact, while a heartbeat can lag behind a busy journal. With the flag off nothing reads it."""

from __future__ import annotations

import json
import time
from typing import Any

KEY = "gateway_heartbeat"
STALE_S = 15.0              # three missed beats
ALERT_COOLDOWN_S = 1800.0
STILL = ("new risk (open, add, widen, propose, amend, ladder, plan changes) is refused; closes, stop tightening, "
         "cancels, halts and flatten run in the session's own process as break-glass, each alerted")
FIX = "docker compose up -d gateway (deploy/DEPLOY.md), or set SHORTBOT_GATEWAY=0 in .env and restart the sessions"


def beat(j: Any, rec: dict[str, Any]) -> None:
    """Posted (#79): another process's write lock delays it, never the gateway."""
    j.post(lambda j: j.set(j.mode_key(KEY), json.dumps(rec)))


def read(j: Any) -> dict[str, Any] | None:
    try:
        rec = json.loads(j.get(j.mode_key(KEY)) or "null")
    except ValueError:
        return None
    return rec if isinstance(rec, dict) and isinstance(rec.get("at"), (int, float)) else None


def _state(j: Any, now: float | None) -> tuple[float | None, dict[str, Any] | None]:
    rec = read(j)
    return (None, None) if rec is None else ((now or time.time()) - float(rec["at"]), rec)


def brief_line(j: Any, settings: Any, now: float | None = None) -> str | None:
    if not settings.gateway:
        return None
    age, rec = _state(j, now)
    if rec is None or age is None:
        return f"### ⛔ order gateway: never seen (SHORTBOT_GATEWAY is on): {STILL}. Fix: {FIX}"
    if age > STALE_S:
        return f"### ⛔ order gateway DOWN: heartbeat {age:.0f} s old (pid {rec.get('pid')}): {STILL}. Fix: {FIX}"
    return (f"### order gateway: up, heartbeat {age:.0f} s ago (pid {rec.get('pid')}, code "
            f"{str(rec.get('code'))[:12]}, {rec.get('running', 0)} running)")


async def watch(j: Any, settings: Any, n: Any, now: float | None = None) -> None:
    """The guardian's job: alert a stale or missing heartbeat while the sessions depend on it (urgent, per cooldown)."""
    if not settings.gateway:
        return
    age, rec = _state(j, now)
    if rec is not None and age is not None and age <= STALE_S:
        return
    what = "has never sent a heartbeat" if rec is None else f"heartbeat is {age:.0f} s old (pid {rec.get('pid')})"
    await n.send("gateway_down", f"The order gateway {what}. While it is down {STILL}. Fix: {FIX}.",
                 cooldown_s=ALERT_COOLDOWN_S, urgent=True)
