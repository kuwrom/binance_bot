"""The gateway's resolver for intents of unknown outcome (#36 step 3, docs/GATEWAY.md §5; gap 3 of §1.4).

An intent ends `unknown` when a send's answer was lost (a timeout, a 5xx, -1007) and its primitive could not settle it
by its client id; `interrupted` when its process died mid-run (intents.sweep). Each order it sent under a client id is
in `intent_orders`; one still without an outcome is looked up by that id (Query Order, or Query Algo Order then the
open conditional orders for a stop: its not-found answer is unverified, #55) and its outcome recorded once
(clientid.outcome; `not_found` once Binance hasn't heard of it GRACE_S after the send: it never reached the book, as
entry_intents decides for limits; Binance keeps a filled order 90 days, an unfilled cancelled one ~3). Once every
order is settled the intent is finalised once (`verdict`), with a decision row and an alert. It never sends anything:
an order is never re-sent (#9). Trade state stays with reconcile and the fill poll, which follow the position. Its
cancels and edits without an outcome are read from the order each targets (intent_changes.looked_up), never sent again.

Runs from the guardian's entry_reconcile job (60 s; its first tick as the guardian starts), after the sweep."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import ccxt.async_support as ccxt

from . import clientid, errors, intent_changes, intents
from .entry_intents import GRACE_S

if TYPE_CHECKING:
    from .trading import Trader

log = logging.getLogger("shortbot.resolver")
CONDITIONAL = ("stop", "bridge", "prearm")   # sent to the Algo service: looked up with trigger=True


async def resolve(tr: Trader, now: float | None = None) -> list[dict[str, Any]]:
    """Every unknown or interrupted intent of this mode, oldest first; one that can't be settled yet waits for the next
    run. Returns the resolutions."""
    out = []
    for row in intents.unresolved(tr.j):
        try:
            ev = await _one(tr, row, time.time() if now is None else now)
        except Exception as e:  # noqa: BLE001 - stays unresolved; the next run looks again (reported unless transient)
            if errors.transient(e):
                log.warning("intent %s unresolved: %s", row["id"], errors.describe(e))
            else:
                await errors.report(tr.alert, "resolver.resolve", e, row["symbol"])
            continue
        if ev:
            out.append(ev)
    return out


async def _one(tr: Trader, row: dict[str, Any], now: float) -> dict[str, Any] | None:
    j = tr.j
    orders = intents.orders(j, row["id"])
    doubted = {o["client_id"] for o in orders if o["outcome"] is None} or {o["client_id"] for o in orders}
    for o in orders:
        if o["outcome"] is None:
            outcome = await _settle(tr, o, now)
            if outcome is None:
                return None   # not found yet: it may still land
            await j.write(intents.record, o["client_id"], outcome)
            o["outcome"] = outcome
    for c in intent_changes.of_intent(j, row["id"]):   # what its cancels and edits did: recorded when it can be told
        if c["outcome"] is None:
            try:
                did = await _change(tr, c, now)
            except Exception as e:  # noqa: BLE001 - its outcome stays unknown; the verdict doesn't count changes yet
                log.warning("intent %s: change %s unread: %s", row["id"], c["ref"], errors.describe(e))
                did = None
            if did is not None:
                await j.write(intent_changes.record, c["ref"], did)
    status = verdict(orders, doubted)
    if not await j.write(intents.resolve, row["id"], row["status"], status, intents.now_ms()):
        return None   # its owner answered first (interrupted), or another resolver did
    got = {o["client_id"]: o["outcome"] for o in orders}
    msg = (f"intent {row['id']} ({row['kind']}, {row['actor']}) {row['status']} -> {status}: "
           + ", ".join(f"{c} {x}" for c, x in got.items()) + ". Nothing was sent again")
    j.log_decision("intent_resolved", msg, row["symbol"], trade_id=row["trade_id"], snapshot={
        "intent_id": row["id"], "kind": row["kind"], "was": row["status"], "status": status, "orders": got})
    await tr.alert("intent_resolved", msg, row["symbol"], status == "partial")
    return {"intent_id": row["id"], "kind": row["kind"], "was": row["status"], "status": status, "orders": got}


async def _settle(tr: Trader, o: dict[str, Any], now: float) -> str | None:
    """The order's outcome by its client id; None while Binance doesn't know it yet. A lookup error raises: in doubt."""
    trig = o["kind"] in CONDITIONAL
    found = await clientid.lookup(tr.t, o["symbol"], o["client_id"], trigger=trig)
    if found is None and trig:
        found = next((x for x in await tr.t.fetch_open_orders(o["symbol"], params={"trigger": True})
                      if x.get("clientOrderId") == o["client_id"]), None)
    if found is not None:
        return clientid.outcome(o["client_id"], found)
    return "not_found" if now - datetime.fromisoformat(o["at"]).timestamp() >= GRACE_S else None


async def _change(tr: Trader, c: dict[str, Any], now: float) -> str | None:
    """A cancel's or an edit's outcome, read from the order it targets (Query Order, or Query Algo Order then the open
    conditional orders); None while it can't be told yet. Never sent again."""
    trig = bool(c["trigger"])
    try:
        found = await (tr.t.fetch_order(c["order_id"], c["symbol"], params={"trigger": True}) if trig
                       else tr.t.fetch_order(c["order_id"], c["symbol"]))
    except ccxt.OrderNotFound:
        found = None
    if found is None and trig:
        found = next((x for x in await tr.t.fetch_open_orders(c["symbol"], params={"trigger": True})
                      if str(x.get("id")) == c["order_id"]), None)
    return intent_changes.looked_up(c, found, intent_changes.later_at(tr.j, c), now)


def swept(r: dict[str, Any]) -> tuple[str, bool]:
    """The intent_interrupted alert for an intent the sweep ended (intents.sweep), and whether it is urgent: it sent
    something, whose outcome the resolver now settles."""
    acts = [*r["orders"], *r["changes"]]
    what = (f"what it sent: {', '.join(acts)}, settled from the exchange (intent_resolved)" if acts
            else "it sent nothing" if r["status"] == "expired" else "no order, cancel or edit linked to it (at most an "
            "account setting, or a link lost with its process): the next passes re-read the book")
    return f"intent {r['id']} ({r['kind']}, {r['actor']}) {r['status']}: its process died mid-run; {what}", bool(acts)


def verdict(orders: list[dict[str, Any]], doubted: set[str]) -> str:
    """done: every send that was in doubt ran in full; refused: none of them ran and nothing else of the intent did;
    partial: anything between (part-filled, or one ran and another didn't). A market close's tries are one send: it ran
    if any of them filled."""
    units: dict[str, list[str]] = {}
    for o in orders:
        if o["client_id"] in doubted:
            key = f"close {o.get('symbol')}" if o["kind"] == "close" else o["client_id"]
            units.setdefault(key, []).append(o["outcome"])
    if all(any(x in clientid.FULL for x in u) for u in units.values()):
        return "done"
    ran = any(any(x in clientid.RAN for x in u) for u in units.values())
    if not ran and not any(o["outcome"] in clientid.RAN for o in orders if o["client_id"] not in doubted):
        return "refused"
    return "partial"
