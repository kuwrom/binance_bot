"""The gateway's resolver for intents of unknown outcome (#36 step 3, docs/GATEWAY.md §5; gap 3 of §1.4).

An intent ends `unknown` while anything it sent is in doubt (results.py, #86): a send whose answer was lost (a timeout,
a 5xx, -1007) and that its flow could not settle by its client id, or a cancel or an edit whose answer was lost;
`interrupted` when its process died mid-run (intents.sweep). Each order it sent under a client id is in
`intent_orders`; one still without an outcome is looked up by that id (Query Order, or Query Algo Order then the open
conditional orders for a stop: its not-found answer is unverified, #55) and its outcome recorded once
(clientid.outcome; `not_found` once Binance hasn't heard of it GRACE_S after the send: it never reached the book, as
entry_intents decides for limits; Binance keeps a filled order 90 days, an unfilled cancelled one ~3). Each cancel or
edit without an outcome is read from the order it targets (intent_changes.looked_up). Once every one is settled the
intent is finalised once (`verdict`), with a decision row and an alert listing its steps. It never sends anything: an
order, a cancel or an edit is never sent again (#9). Trade state stays with reconcile and the fill poll, which follow
the position.

Runs from the guardian's entry_reconcile job (60 s; its first tick as the guardian starts), after the sweep."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import ccxt.async_support as ccxt

from . import clientid, errors, intent_changes, intents
from .entry_intents import GRACE_S
from .results import declares_doubt

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
    orders, changes = intents.orders(j, row["id"]), intent_changes.of_intent(j, row["id"])
    doubted = ({o["client_id"] for o in orders if o["outcome"] is None}
               | {c["ref"] for c in changes if c["outcome"] is None}) or {*(o["client_id"] for o in orders),
                                                                         *(c["ref"] for c in changes)}
    for o in orders:
        if o["outcome"] is None:
            outcome = await _settle(tr, o, now)
            if outcome is None:
                return None   # not found yet: it may still land
            await j.write(intents.record, o["client_id"], outcome)
            o["outcome"] = outcome
    for c in changes:
        if c["outcome"] is None:
            did = await _change(tr, c, now)
            if did is None:
                return None   # still resting inside the grace: it may yet land
            await j.write(intent_changes.record, c["ref"], did)
            c["outcome"] = did
    body = json.loads(row["result"]) if row["result"] is not None else None
    status = verdict(row["status"], body, acts(orders, changes), doubted)
    if not await j.write(intents.resolve, row["id"], row["status"], status, intents.now_ms()):
        return None   # its owner answered first (interrupted), or another resolver did
    got = {o["client_id"]: o["outcome"] for o in orders}
    steps = intent_changes.steps(j, row["id"])
    msg = (f"intent {row['id']} ({row['kind']}, {row['actor']}) {row['status']} -> {status}: "
           + "; ".join(f"{s['step']} {s['ref']} {s['outcome']}" for s in steps)
           + (" (it stopped there)" if row["status"] == "interrupted" else "") + ". Nothing was sent again")
    j.log_decision("intent_resolved", msg, row["symbol"], trade_id=row["trade_id"], snapshot={
        "intent_id": row["id"], "kind": row["kind"], "was": row["status"], "status": status, "orders": got,
        "steps": steps})
    # urgent: a caller told `unknown` learns that part of it ran (an interrupted one was alerted when swept)
    await tr.alert("intent_resolved", msg, row["symbol"], status == "partial" and row["status"] == "unknown")
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


def acts(orders: list[dict[str, Any]], changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """An intent's sends and changes as the verdict weighs them: each with its unit (a market close's tries are one
    send; an order's cancels or edits one change) and its settled outcome."""
    return [*({"ref": o["client_id"], "unit": f"close {o.get('symbol')}" if o["kind"] == "close" else o["client_id"],
               "outcome": o["outcome"], "full": o["outcome"] in clientid.FULL, "ran": o["outcome"] in clientid.RAN}
              for o in orders),
            *({"ref": c["ref"], "unit": f"{c['op']} {c['order_id']}", "outcome": c["outcome"],
               "full": c["outcome"] in clientid.CHANGED, "ran": c["outcome"] in clientid.CHANGED} for c in changes)]


def verdict(was: str, body: Any, acts: list[dict[str, Any]], doubted: set[str]) -> str:
    """How an unknown or interrupted intent ended, its sends and changes (`acts`) settled; `doubted`: their refs.
    - interrupted: refused if nothing it sent took effect, else partial. Never done: it stopped before its end, so its
      sends can't say it reached it (a stop swap killed after its bridge: the stop never moved).
    - unknown, raised or a body that names what it couldn't settle (results.declares_doubt): by the doubted units: done
      if each took full effect, refused if nothing of the intent ran, else partial.
    - unknown, a body definite about its goal (a close done, a swap that says its stop didn't move): what it said,
      the doubted acts being side steps (an earlier try, a target, a cleanup cancel): done, or partial if a market
      order filled short; with ok false, refused if nothing of it ran (the bridge never landed), else partial (one
      landed after all)."""
    ran = any(a["ran"] for a in acts)
    if was == "interrupted":
        return "partial" if ran else "refused"
    units: dict[str, list[dict[str, Any]]] = {}
    for a in acts:
        units.setdefault(a["unit"], []).append(a)
    if body is None or declares_doubt(body):
        keys = {a["unit"] for a in acts if a["ref"] in doubted}
        if all(any(a["full"] for a in units[k]) for k in keys):
            return "done"
        return "partial" if ran else "refused"
    if isinstance(body, dict) and body.get("ok") is False:
        return "partial" if ran else "refused"
    short = any(not any(a["full"] for a in u) and any(a["outcome"] == "part_filled" for a in u) for u in units.values())
    return "partial" if short else "done"
