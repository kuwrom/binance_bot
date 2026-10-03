"""Orphaned reduce-only stops (#37). A pre-armed entry stop (`sb-p<entry id>-…`, entries_place._prearm) or a stop-swap
bridge (`sb-b…`, stops.py) whose cancel failed stays on the book with nothing tracking it, and buys back its size at
its own level: it cuts the next short on the symbol, or a leg after a widen. entry_intents' sweep lists regular
orders only, so this one lists every conditional order (GET /fapi/v1/openAlgoOrders without a symbol, weight 40:
Current-All-Algo-Open-Orders doc) every SWEEP_EVERY_S, from the same reconcile job. Under each suspect symbol's lock, on
a fresh listing of that symbol, it cancels one of OURS when both hold:
- no live owner: a pre-arm whose entry row is missing (or names another pre-arm) or final (filled, cancelled, expired,
  rejected); a live row (pending, placing, unknown) may still fill and needs it, and an 'error' row (a fill nobody
  could journal) is a human's. A bridge never has one: a swap holds the lock throughout, so one seen under the lock was
  left behind
- nothing needs it as protection: the symbol is flat, or a closePosition stop rests (it covers the whole position). With
  a position and no closePosition stop it may be the only stop: it stays (the guardian restores the closePosition stop,
  then the next sweep cancels it), told once
Orders without our client-id prefix are never touched. Each cancel is told once."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from . import clientid, errors, stops
from .exchange import to_symbol
from .symlock import SymbolBusy

log = logging.getLogger("shortbot.entries")
SWEEP_EVERY_S = 300
PREARM = f"{clientid.PREFIX}-{clientid.KINDS['prearm']}"   # client id prefix of a pre-armed entry stop
_PREARM_ID = re.compile(rf"^{re.escape(PREARM)}(\d+)-")
LIVE = ("pending", "placing", "unknown")   # the entry may still fill: its pre-arm is its protection
HUMAN = ("error",)                         # a reported fill nobody could journal: maybe its only stop


def orphan(desk, cid: str | None) -> str | None:
    """Why the conditional order with this client id has no live owner, or None (owned, or not ours to judge)."""
    cid = cid or ""
    if cid.startswith(stops.BRIDGE):
        return "a stop-swap bridge left behind"
    m = _PREARM_ID.match(cid)
    if not m:
        return None
    eid = int(m.group(1))
    rows = desk._rows("id=?", (eid,))
    if not rows or rows[0].get("prearm_cid") != cid:
        return f"no entry row #{eid} owns it"
    st = rows[0]["status"]
    return None if st in LIVE or st in HUMAN else f"its entry #{eid} is {st}"


async def sweep(desk) -> list[dict[str, Any]]:
    """Every SWEEP_EVERY_S: cancel our orphaned pre-arms and bridges (module doc). One symbol never costs the others."""
    if time.time() - desk._orphans_at < SWEEP_EVERY_S:
        return []
    desk._orphans_at = time.time()
    rows = await desk.tr.t.fapiPrivateGetOpenAlgoOrders()
    suspects = sorted({to_symbol(r["symbol"]) for r in rows if orphan(desk, r.get("clientAlgoId"))})
    events: list[dict[str, Any]] = []
    for sym in suspects:
        try:
            async with desk.tr.symbol_lock(sym, "orphan sweep: our reduce-only stops nothing owns"):
                events += await _symbol(desk, sym)
        except SymbolBusy:
            continue   # a session acts on it: the next sweep
        except Exception as e:  # noqa: BLE001 - the other symbols still get swept; the next sweep retries this one
            if errors.transient(e):
                log.warning("orphan sweep %s: %s", sym, errors.describe(e))
            else:
                await errors.report(desk.tr.alert, "orphans.sweep", e, sym)
    return events


async def _symbol(desk, sym: str) -> list[dict[str, Any]]:
    tr, events = desk.tr, []
    listed = await tr.t.fetch_open_orders(sym, params=stops.TRIG)   # fresh, under the lock (weight 1)
    covered = any(stops.is_close_position(o) for o in listed) or await tr.position(sym) is None
    for o in listed:
        why = orphan(desk, o.get("clientOrderId"))
        if why is None:
            continue
        what = f"{o.get('clientOrderId')} (buy stop {stops.trigger_of(o):g} for {o.get('amount')})"
        if not covered:
            if ("orphan_kept", str(o["id"])) not in desk._flagged:
                desk._flagged.add(("orphan_kept", str(o["id"])))
                await tr.alert("orphan_stop_kept", f"{what}: {why}, but the position has no closePosition stop, so it "
                               "may be its only one: kept until the guardian restores that stop", sym, False)
            continue
        try:
            await tr.cancel(sym, o["id"], trigger=True)
        except Exception as e:  # noqa: BLE001 - Gone: off the book already; anything else: the next sweep retries
            if errors.classify(e) is not errors.Gone:
                log.warning("orphan %s on %s not cancelled: %s", o["id"], sym, errors.describe(e))
                continue
        await tr.alert("orphan_stop_cancelled", f"{what} cancelled: {why}; nothing needs it as protection", sym, False)
        events.append({"event": "orphan_cancelled", "symbol": sym, "order_id": str(o["id"]),
                       "client_id": o.get("clientOrderId"), "why": why})
    return events
