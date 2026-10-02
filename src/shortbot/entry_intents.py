"""Write-ahead intents for limit entries (0G 2026-10-01: a leg reported rejected stayed live and filled untracked).

Every entry row and its client order id are journalled BEFORE the order is sent (entries_place.py), then the send's
outcome: placing -> pending (acked, resting) | rejected (definite refusal) | unknown (timeout, 5xx, -1007...).
A pre-armed stop's send is unresolved while prearm_cid is set and prearm_id is NULL.

`reconcile` (its own protective guardian job, 60 s; the 5 s fill poll runs `resolve` on 'unknown' rows too) resolves
them by client id, never by resending (clientid.py):
- unknown, found -> pending: the poll adopts its fill and protects it. If abandoned (a rolled-back split leg, or a
  placing call that never returned, so nobody was told it rests) and unfilled -> cancelled. A fill is never stripped.
- unknown, not found GRACE_S after the send -> rejected (it never reached the book)
- placing older than GRACE_S: its call died mid-send (crash, cancelled request) -> unknown + abandoned
- an unresolved pre-armed stop, matched by client id among the open trigger orders: kept by a pending entry,
  cancelled for a dead one; unmatched -> left unresolved and alerted once (never guessed)
- every SWEEP_EVERY_S, all open orders: one the journal doesn't know -> an 'external' row + alert (exposure with no
  known stop until it leaves the book: 'external_filled' while the position it made is open, else 'external_gone');
  a late 'sb-' order whose row is already dead -> cancelled (adopted instead if it has a fill)
Transitions are compare-and-set (EntryDesk._settle: one UPDATE ... WHERE status=?), so the MCP server and the
guardian never overwrite each other, and no write transaction is held across an exchange call.
Known gap (accepted): a propose call that dies after every split leg is acked but before tranches.finalize leaves
the legs resting, unpinned; only a leg still 'placing' gets its split rolled back.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import ccxt.async_support as ccxt

from . import clientid, targets
from .exchange import to_symbol
from .journal import now_iso

log = logging.getLogger("shortbot.entries")
GRACE_S = 90            # > the longest a send can take (ccxt timeout 20 s, recvWindow 10 s): not found = never placed
SWEEP_EVERY_S = 300     # open orders of all symbols cost request weight 40
PREARM_CHASE_S = 86400  # stop looking for a dead entry's unresolved pre-armed stop after a day
LATE_S = 600            # a rejected send this young may still execute late (explained_symbols)
ON_BOOK = "status IN ('pending','placing','unknown','external')"   # (maybe) resting: exposure, the analyst's view
HINTS = {"placing": "send in flight, or its call died: the guardian resolves it by client id within ~3 min.",
         "unknown": "send status unknown: it may rest on the book. Don't re-propose it; the guardian resolves it by "
                    "client id within ~3 min (adopts it if it rests, else marks it rejected).",
         "external": "NOT placed by shortbot: no plan, no stop, not managed (counted as exposure at the max stop "
                     "distance). Cancel it on Binance, or tell the human."}


def external_orders(j) -> list[dict[str, Any]]:
    """Resting orders the desk didn't place: exposure whose stop is unknown (risk_budget takes the worst case). This
    journal's mode only (filtered here, so a table not migrated yet still counts as demo)."""
    try:
        rows = j._rows("SELECT * FROM entries WHERE status='external'")
    except sqlite3.OperationalError:   # no entries table yet
        return []
    return [{k: r[k] for k in ("symbol", "amount", "limit_price")} for r in rows if (r.get("mode") or "demo") == j.mode]


class SendUnknown(Exception):
    """The order may or may not rest on the book; its row stays 'unknown' until reconcile() resolves it."""

    def __init__(self, entry_id: int, cause: BaseException):
        super().__init__(f"send status unknown ({type(cause).__name__}: {cause})")
        self.entry_id = entry_id


def _age_s(r: dict[str, Any]) -> float:
    return time.time() - datetime.fromisoformat(r["created_at"]).timestamp()


def _unit(r: dict[str, Any]) -> dict[str, Any]:
    """An entry row in the shape _place_one returns (what _rollback and tranches.finalize read)."""
    return {"entry_id": r["id"], "amount": r["amount"],
            "risk_usdt": round(r["amount"] * (r["stop_price"] - r["limit_price"]), 2)}


async def located(t, e: dict[str, Any]) -> dict[str, Any] | str:
    """For entries.withdraw(): a send of unknown outcome (no order id yet), found by its client id. Returns the row
    with the order id it got, "unfilled" if it never reached the book (not found GRACE_S after the send), or
    "unknown" (not found yet, or the lookup failed)."""
    if not e.get("client_id"):
        return "unknown"
    try:
        o = await clientid.lookup(t, e["symbol"], e["client_id"])
    except Exception:  # noqa: BLE001 - can't tell: a later pass retries
        return "unknown"
    if o is None:
        return "unfilled" if _age_s(e) >= GRACE_S else "unknown"
    return {**e, "order_id": o["id"]}


async def reconcile(desk) -> list[dict[str, Any]]:
    """Resolve every limit-entry send whose outcome the journal doesn't know. One failure never blocks the rest."""
    events: list[dict[str, Any]] = []
    for r in desk._rows("status IN ('placing','unknown') AND client_id IS NOT NULL"):
        try:
            ev = await resolve(desk, r)
        except Exception as e:  # noqa: BLE001 - stays unresolved; retried next tick
            log.warning("entry #%s unresolved: %s: %s", r["id"], type(e).__name__, e)
            continue
        if ev:
            events.append(ev)
    for step in (_resolve_prearms, _sweep):
        try:
            events += await step(desk)
        except Exception as e:  # noqa: BLE001
            log.warning("%s failed: %s: %s", step.__name__, type(e).__name__, e)
    return events


async def resolve(desk, r: dict[str, Any]) -> dict[str, Any] | None:
    """One placing/unknown row, by its client id (module doc). The 5 s fill poll runs it on each 'unknown' row too:
    with no pre-armed stop, its fill must not wait for this job's 60 s tick (entries._poll_unknown)."""
    age = _age_s(r)
    if r["status"] == "placing":
        if age < GRACE_S:
            return None   # its own call may still be waiting for the answer
        if not desk._settle(r["id"], "placing", status="unknown", abandon=1, reason="placing call never returned"):
            return None
        r = {**r, "status": "unknown", "abandon": 1}
        if r.get("tranche"):   # a split entry is one unit: the dead call's acked, never-pinned legs go too
            legs = desk._rows("tranche=? AND status='pending'", (r["tranche"],))
            async with desk.tr.symbol_lock(r["symbol"], f"entry #{r['id']}: roll back a dead call's split"):
                await desk._rollback([_unit(x) for x in legs if "ladder_live" not in x["plan"]])
    t = desk.tr.t
    o = await clientid.lookup(t, r["symbol"], r["client_id"])
    if o is None:
        if age < GRACE_S:
            return None   # an ambiguous send can still reach the book
        return await _done(desk, r, "rejected", f"not on the exchange {age:.0f} s after the send: never placed")
    filled = float(o.get("filled") or 0)
    if not filled and o.get("status") in ("canceled", "expired", "rejected"):
        return await _done(desk, r, "expired" if o["status"] == "expired" else "cancelled",
                           f"found {o['status']} and unfilled", order_id=o["id"])
    if r["abandon"] and not filled:
        from .entries import withdraw
        async with desk.tr.symbol_lock(r["symbol"], f"entry #{r['id']}: withdraw an abandoned intent"):
            got = await withdraw(t, {**r, "order_id": o["id"]})
        if got == "unknown":
            return None   # the cancel failed: look again next tick
        if got == "unfilled":
            return await _done(desk, r, "cancelled", "withdrawn intent found resting: cancelled", order_id=o["id"])
    return await _adopt(desk, r, o["id"], "abandoned intent had FILLED" if r["abandon"] else "found resting")


async def _adopt(desk, r: dict[str, Any], oid: str, why: str) -> dict[str, Any] | None:
    """From here on an ordinary pending entry: the poll adopts its fill and protects it with the plan's stop."""
    if r.get("tranche") and "ladder_live" not in r["plan"]:
        from . import tranches
        tranches.finalize(desk.j.db, [_unit(r)])   # its split was rolled back: pinned to its own size, like _rollback
    return await _done(desk, r, "pending", f"{why}: adopted", urgent=bool(r["abandon"]) or r["status"] != "unknown",
                       order_id=oid)


async def _done(desk, r: dict[str, Any], status: str, why: str, urgent: bool = False, **f: Any) -> dict | None:
    if not desk._settle(r["id"], r["status"], status=status, reason=why, **f):
        return None   # resolved elsewhere first
    desk.j.log_decision("entry_intent", f"entry #{r['id']} {r['status']} -> {status}: {why}", r["symbol"],
                        snapshot={"entry_id": r["id"], "client_id": r["client_id"], "status": status, **f})
    await desk.tr.alert("entry_intent", f"entry #{r['id']} ({r['client_id']}) {r['status']} -> {status}: {why}",
                        r["symbol"], urgent)
    return {"entry_id": r["id"], "event": status, "why": why}


async def _resolve_prearms(desk) -> list[dict[str, Any]]:
    rows = [r for r in desk._rows("prearm_cid IS NOT NULL AND prearm_id IS NULL")
            if _age_s(r) >= GRACE_S and (r["status"] == "pending" or _age_s(r) < PREARM_CHASE_S)]
    t, events = desk.tr.t, []
    for sym in sorted({r["symbol"] for r in rows}):
        by_cid = {o.get("clientOrderId"): o for o in await t.fetch_open_orders(sym, params={"trigger": True})}
        for r in (x for x in rows if x["symbol"] == sym):
            o = by_cid.get(r["prearm_cid"])
            if o is None:   # "not resting" and "id not reported" look the same here: alert, never guess
                if ("prearm", r["id"]) not in desk._flagged:
                    desk._flagged.add(("prearm", r["id"]))
                    await desk.tr.alert("prearm_unresolved", f"entry #{r['id']} ({r['status']}): pre-armed stop "
                                        f"{r['prearm_cid']} not among the open stops; check Binance", sym, False)
                continue
            if r["status"] != "pending":   # its entry is gone, so its stop must go too
                try:
                    async with desk.tr.symbol_lock(sym, f"entry #{r['id']}: cancel a dead entry's pre-arm"):
                        await t.cancel_order(o["id"], sym, params={"trigger": True})
                except Exception:  # noqa: BLE001 - retried next tick
                    continue
            desk._update(r["id"], prearm_id=o["id"])
            events.append({"entry_id": r["id"], "event": "prearm_resolved", "cancelled": r["status"] != "pending"})
    return events


async def _sweep(desk) -> list[dict[str, Any]]:
    """Every open order on the account must belong to a live journal row. Conditional orders are not swept (desk
    stops carry 'sb-s' client ids since #9, but nothing sweeps them yet). Raw endpoint: ccxt's fetch_open_orders refuses to run without a symbol
    unless an option is changed (request weight 40 either way)."""
    if time.time() - desk._swept_at < SWEEP_EVERY_S:
        return []
    desk._swept_at = time.time()
    rows = desk._rows("order_id IS NOT NULL OR client_id IS NOT NULL")
    by_oid = {str(r["order_id"]): r for r in rows if r["order_id"]}
    by_cid = {r["client_id"]: r for r in rows if r["client_id"]}
    orders = await desk.tr.t.fapiPrivateGetOpenOrders()
    await _externals_left(desk, rows, {str(o["orderId"]) for o in orders})
    events = []
    for o in orders:
        oid, cid, sym = str(o["orderId"]), o.get("clientOrderId"), to_symbol(o["symbol"])
        if targets.is_target(cid):
            continue   # a resting plan target: targets_sync keeps those (#65)
        r = by_cid.get(cid) or by_oid.get(oid)
        try:
            if r is None:
                events.append(await _external(desk, o, oid, cid, sym))
            elif r["status"] not in ("pending", "placing", "unknown", "external"):
                events.append(await _late(desk, r, o, oid, cid, sym))
        except Exception as e:  # noqa: BLE001 - next order; the next sweep retries
            log.warning("sweep %s %s: %s: %s", sym, oid, type(e).__name__, e)
    return [e for e in events if e]


async def _externals_left(desk, rows: list[dict], resting: set[str]) -> None:
    """An external order off the book is no longer resting exposure. If it filled, the position it made is explained
    (and already alerted): 'external_filled' while that position is open, so it doesn't trip the kill switch's
    untracked-position check; otherwise, and once that position is gone, 'external_gone'."""
    t = desk.tr.t
    for r in rows:
        if r["status"] == "external" and str(r["order_id"]) not in resting:
            try:
                o = await t.fetch_order(r["order_id"], r["symbol"])
            except ccxt.OrderNotFound:   # Query Order forgets only unfilled CANCELED/EXPIRED orders (after ~3 days)
                o = {"filled": 0, "status": "forgotten by Binance: unfilled"}
            except Exception as e:  # noqa: BLE001 - stays 'external' (counted): the next sweep asks again
                log.warning("external order %s: %s: %s", r["order_id"], type(e).__name__, e)
                continue
            st = "external_filled" if float(o.get("filled") or 0) > 0 else "external_gone"
            desk._settle(r["id"], "external", status=st, reason=f"{r['reason']}; left the book ({o.get('status')})")
    if any(r["status"] == "external_filled" for r in rows):
        held = {p["symbol"] for p in await desk.tr._raw_positions()}
        for r in rows:
            if r["status"] == "external_filled" and r["symbol"] not in held:
                desk._settle(r["id"], "external_filled", status="external_gone")


def explained_symbols(desk) -> set[str]:
    """Symbols where a position the journal doesn't know (yet) is expected: an order of ours that is or may be on the
    book (the fill poll or the reconciler journals it), a send rejected in the last LATE_S (it may still have run:
    the sweep adopts its fill), or an external order that filled (already alerted)."""
    since = datetime.fromtimestamp(time.time() - LATE_S, timezone.utc).isoformat(timespec="seconds")
    return {r["symbol"] for r in desk._rows("status IN ('pending','placing','unknown','external','external_filled')"
                                            " OR (status='rejected' AND created_at >= ?)", (since,))}


async def _external(desk, o: dict, oid: str, cid: str | None, sym: str) -> dict[str, Any]:
    why = ("a shortbot client id without a journal row (restored journal? another DB?)" if clientid.ours(cid)
           else "not placed by shortbot")
    desk.j.db.execute(
        "INSERT INTO entries (group_id, symbol, status, created_at, expires_ms, limit_price, amount, order_id, reason,"
        " mode) VALUES ('external', ?, 'external', ?, ?, ?, ?, ?, ?, ?)",
        (sym, now_iso(), int(o.get("goodTillDate") or 0), float(o.get("price") or 0), float(o.get("origQty") or 0),
         oid, f"{o.get('side')} {o.get('type')} {cid}: {why}", desk.j.mode))
    desk.j.db.commit()
    await desk.tr.alert("external_order", f"open {o.get('side')} {o.get('type')} {o.get('origQty')} @ {o.get('price')}"
                        f" ({cid}) is {why}: the desk doesn't manage it. Cancel it or journal it.", sym, True)
    return {"event": "external", "order_id": oid, "symbol": sym}


async def _late(desk, r: dict, o: dict, oid: str, cid: str | None, sym: str) -> dict[str, Any] | None:
    """An order rests although its row is dead: a send that reached the book after the grace, or a lost cancel."""
    if not (clientid.ours(cid) and cid == r["client_id"]):
        if ("late", oid) not in desk._flagged:
            desk._flagged.add(("late", oid))
            await desk.tr.alert("zombie_order", f"entry #{r['id']} is {r['status']} in the journal but its order "
                                f"{oid} still rests; check Binance", sym, True)
        return None
    from .entries import withdraw
    async with desk.tr.symbol_lock(sym, f"entry #{r['id']}: cancel a late order"):   # cancel first; a fill is kept
        got = await withdraw(desk.tr.t, {**r, "order_id": oid})
    if got == "unknown":
        return None   # the next sweep retries
    if got == "filled" and r["status"] != "filled":
        return await _adopt(desk, r, oid, f"{r['status']} intent turned up with a fill")
    await desk.tr.alert("entry_intent", f"entry #{r['id']} ({cid}) is {r['status']} but its order rested: cancelled",
                        sym, True)
    return {"entry_id": r["id"], "event": "late_order_cancelled"}
