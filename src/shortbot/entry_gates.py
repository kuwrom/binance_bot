"""Account and market gates on RESTING limit entries (#12). A resting entry is a trade that hasn't happened yet: it can
fill into a halt the governor called after it was placed, or into a market pause (BTC/ETH +0.8% in 15 min, a breadth
squeeze), when the alt catch-up fills several correlated shorts at once. New entries are refused in both; the
guardian's 5 s pass withdraws the resting ones too. Cancel, not freeze: Binance can't suspend an order, and an entry
re-proposed afterwards is re-checked and re-sized at the prices and budget of then.

- governor halt: the monitor lane's verdict (guardian check_governor, every 5 min) under the EFFECTIVE limits, the
  ones the session sizes and places under (as the kill-switch loss trips), so placing and withdrawing never fight.
  Ignored once older than MAX_AGE_S or past its `until`
- market pause: the cached market map, at most MAX_AGE_S old, so a stopped guardian never causes a cancel
- a row whose placement explicitly overrode that pause (veto_override while it was on: overrides.pause_overridden)
  rests on through it; nothing overrides the governor
Rows are withdrawn by killswitch_ops.cancel_resting: only an unfilled limit closes its row ("governor halt: ..." /
"market pause: ..."), a fill is the poll's to adopt. The kill switch's HALTED sweep is separate (guardian_positions).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

from .market.market_map import cached

log = logging.getLogger("shortbot.entries")
GOV_KEY = "governor_verdict"   # journal kv, per mode
MAX_AGE_S = 900
SWEEP_TIMEOUT_S = 60           # a hung cancel must not hold up the fill poll behind it
RESTING = "status IN ('pending','placing','unknown')"   # the rows killswitch_ops.cancel_resting withdraws


def publish_governor(j, gov: dict[str, Any], limits: str, now: float | None = None) -> None:
    """The guardian's governor verdict for the protective lane; `limits` names the limits it was judged under."""
    j.set(j.mode_key(GOV_KEY), json.dumps({"ts": time.time() if now is None else now, "halt": bool(gov.get("halt")),
                                           "until": gov.get("until"), "reasons": list(gov.get("reasons") or []),
                                           "limits": limits}))


def governor_halt(j, now: float | None = None) -> list[str]:
    """The governor's reasons while it halts new entries; [] when it doesn't, or its verdict is stale or ran out."""
    try:
        v = json.loads(j.get(j.mode_key(GOV_KEY)) or "{}")
        now = time.time() if now is None else now
        if not v.get("halt") or now - float(v["ts"]) > MAX_AGE_S:
            return []
        if v.get("until") and datetime.fromisoformat(v["until"]).timestamp() <= now:
            return []
    except (ValueError, KeyError, TypeError):   # unreadable: no verdict
        return []
    return list(v.get("reasons") or ["halted"])


def market_pause(j) -> list[str]:
    """The market-wide pause vetoes of a fresh market map; [] when there are none or the map is stale."""
    return list(((cached(j, MAX_AGE_S) or {}).get("vetoes") or {}).get("pause") or [])


def overrode_pause(e: dict[str, Any]) -> bool:
    try:
        return bool(json.loads(e.get("overrides") or "{}").get("pause_overridden"))
    except (ValueError, AttributeError):
        return False


async def sweep(desk) -> dict[str, Any] | None:
    """Withdraw this desk's resting entries while the governor halts or a market pause is on; None when neither."""
    if not desk._rows(RESTING):   # nothing rests: don't parse the market map every 5 s
        return None
    gov, pause = governor_halt(desk.j), market_pause(desk.j)
    if not gov and not pause:
        return None
    from .killswitch_ops import cancel_resting   # (it imports entries, which imports this module's users)
    reason = f"governor halt: {'; '.join(gov)}" if gov else f"market pause: {'; '.join(pause)}"
    return {"reason": reason, **await cancel_resting(desk, reason, keep=None if gov else overrode_pause)}


async def guardian_sweep(desk, n) -> None:
    """The guardian's hook (protective lane, every pass while not HALTED). Never fails the fill poll behind it."""
    from .guardian_loop import send_safe
    try:
        r = await asyncio.wait_for(sweep(desk), SWEEP_TIMEOUT_S)
        if r and r["cancelled"]:   # once per withdrawal (a cancel that keeps failing is retried, not re-alerted)
            await send_safe(n, "entries_withdrawn", f"{r['reason']}: withdrew resting entries {r['cancelled']}" + (
                f"; left for the fill poll {r['left_for_fill_poll']}" if r["left_for_fill_poll"] else "")
                + ". Re-propose once it lifts.", cooldown_s=0)
    except Exception as e:  # noqa: BLE001 (incl. TimeoutError) - retried next pass
        log.exception("entry gate sweep failed")
        await send_safe(n, "guardian_error:entry_gates", f"resting-entry sweep: {type(e).__name__}: {e}",
                        urgent=True, cooldown_s=1800)
