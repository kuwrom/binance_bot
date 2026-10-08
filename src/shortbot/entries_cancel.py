"""Withdrawing limit entries (mixed into EntryDesk, entries.py): `withdraw` pulls one order and believes only what
filled; `_cancel_entry` does it for one journal row inside the symbol lock, compare-and-set; `cancel_group` for every
order of an OCO group that is or may be on the book; `_cancel_by_rule` counts a failing sibling-retry cancel. The
kill switch and entry_gates withdraw every resting row through `_cancel_entry` too (`cancel_resting`).
`cancel_external` is the one path to an order the desk did NOT place: a command with a reason, one order by its id,
never a sweep (the automatic paths above leave external orders alone: they may be the owner's).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import ccxt.async_support as ccxt

from . import clientid, entry_intents, errors, intent_changes, targets
from . import entries_partial as partial
from .audit import refusals
from .entry_gates import RESTING
from .entry_intents import GRACE_S, located
from .exchange import to_symbol
from .symlock import SymbolBusy

if TYPE_CHECKING:
    from collections.abc import Callable

    from .entries import EntryDesk
    from .journal import Journal
    from .trading import Alert, Trader

log = logging.getLogger("shortbot.entries")

CANCEL_FAILS_ALERT = 3   # sibling-retry cancels failed in a row before a human is told (once per streak)
LIVE = ("pending", "placing", "unknown")   # the desk's rows that are or may be on the book (RESTING)


async def drop_prearm(tr: Trader, symbol: str, prearm_id: Any, alert: Alert | None = None,
                      why: str = "") -> bool:
    """Cancel an entry's pre-armed stop. True when it is off the book: cancelled now, or Gone (-2011: triggered or
    cancelled before). Anything else leaves it maybe resting, a reduce-only BUY stop that would cut the next short on
    the symbol: told (`alert`: Trader.alert's shape), and the orphan sweep (orphans.py) cancels it once its entry is
    final. Callers hold the symbol lock."""
    try:
        await tr.cancel(symbol, prearm_id, trigger=True)
        return True
    except Exception as e:  # noqa: BLE001 - classified: Gone is done; anything else is told below
        if errors.classify(e) is errors.Gone:
            return True
        log.warning("pre-armed stop %s on %s not cancelled (%s): %s", prearm_id, symbol, why, errors.describe(e))
        if alert is not None:
            await alert("prearm_left", f"pre-armed stop {prearm_id} not cancelled ({why}): {errors.describe(e)}. It "
                        "may still rest and cut a later short here; the orphan sweep cancels it within minutes",
                        symbol, False)
        return False


async def withdraw(tr: Trader, e: dict[str, Any], alert: Alert | None = None,
                   note: dict[str, Any] | None = None) -> str:
    """Cancel a pending entry's limit FIRST, then decide from what actually filled:
    - "unfilled": the limit is dead with nothing filled, so its pre-armed stop is cancelled too
    - "filled": it (partly) filled before the cancel landed: the stop and the pending row stay, the poll adopts it
    - "unknown": the cancel failed (network, clock, a send not found yet): nothing changes, a later pass retries
    Binance answers -2011 (ccxt OrderNotFound) for ANY order no longer in the book, filled or dead, so fetch_order
    decides (MOVR 2026-10-01: a swallowed -2011 stripped a filled order's stop and left a naked short).
    A row with no order id (a send of unknown outcome) is first looked up by its client id (entry_intents.located).
    `alert`: told when the pre-armed stop of an unfilled limit can't be cancelled (drop_prearm). `note`: the status and
    reason the limit's cancel is sent for, recorded with it (intent_changes.py: the poll labels the row by it when the
    cancel's answer was lost)."""
    if not e.get("order_id"):
        found = await located(tr.t, e)
        if isinstance(found, str):
            return found
        e = found
    try:
        o = await tr.cancel(e["symbol"], e["order_id"], note=note) or {}
    except ccxt.OrderNotFound:
        try:
            o = await tr.t.fetch_order(e["order_id"], e["symbol"])
        except Exception:  # noqa: BLE001 - unread: "unknown", the caller retries next pass (and streak-alerts)
            return "unknown"
        if not float(o.get("filled") or 0) and o.get("status") not in clientid.DEAD:
            return "unknown"
    except Exception:  # noqa: BLE001 - not sent, or sent with the outcome unknown: the order may still rest
        return "unknown"
    if float(o.get("filled") or 0) > 0:
        return "filled"
    if e.get("prearm_id"):
        await drop_prearm(tr, e["symbol"], e["prearm_id"], alert, f"entry #{e.get('id')} withdrawn unfilled")
    return "unfilled"


def dead_label(j: Journal, e: dict[str, Any], o: dict[str, Any]) -> tuple[str, str]:
    """(status, reason) for a pending row whose order Binance shows dead, or past its TTL (#86). Canceled: by the cancel
    of ours recorded against the order (intent_changes: an answer lost, so the row stayed pending), labelled with the
    status and reason it was sent for; else by a hand on the exchange. Expired, or still open past its TTL: expired."""
    st = o.get("status")
    if st != "canceled":
        return "expired", f"exchange status {st}"
    mine = intent_changes.latest(j, e["symbol"], e["order_id"])
    if mine is None:
        return "cancelled", "exchange status canceled: cancelled on the exchange, not by the desk"
    note = mine["note"] or {"status": "cancelled", "reason": "cancelled"}
    return note["status"], (f"{note['reason']} (its cancel's answer was lost: intent {mine['intent_id']}; Binance "
                            "shows it canceled)")


def amended_on_the_book(j: Journal, e: dict[str, Any], o: dict[str, Any]) -> dict[str, Any] | None:
    """The row's update when the book shows its order at the price an edit of ours asked for, though the row doesn't
    (the amend's answer was lost, #86): that price, replaced (the amend-once rule holds), and why. None otherwise."""
    px = o.get("price")
    if px is None or abs(float(px) - float(e["limit_price"])) < 1e-12:
        return None
    mine = intent_changes.latest(j, e["symbol"], e["order_id"], "edit")
    if mine is None or mine["want"] is None or abs(float(px) - mine["want"]) >= 1e-12:
        return None
    why = (mine["note"] or {}).get("reason") or "amend"
    return {"limit_price": float(px), "replaced": 1,
            "reason": f"amended: {why} (its answer was lost: intent {mine['intent_id']}; the book shows {float(px):g})"}


async def cancel_resting(desk: EntryDesk, reason: str,
                         keep: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, list[int]]:
    """HALTED: withdraw every resting limit entry (the guardian repeats this every cycle while HALTED; entry_gates
    reuses it for a governor halt or a market pause, keeping the rows `keep(row)` spares). Only an
    "unfilled" withdraw (limit dead, nothing filled, pre-armed stop gone with it) closes the row; "filled" and
    "unknown" keep the row and its stop, so the guardian's fill poll protects and journals the position.
    The desk's own rows only, incl. sends of unknown outcome (placing/unknown: found by client id, and 'rejected' once
    GRACE_S passed without it reaching the book: desk._cancel_entry, re-read and compare-and-set inside the symbol
    lock); never an 'external' order, which may be the owner's (the guardian alerts those instead). Always inside an
    intent: a command's (halt_trading, flatten_all), or the withdraw_entries intent of a sweep or the watchdog."""
    out: dict[str, list[int]] = {"cancelled": [], "left_for_fill_poll": []}
    for e in [e for e in desk._rows(RESTING) if not (keep and keep(e))]:
        try:
            got = await desk._cancel_entry(e, "cancelled", reason)
        except SymbolBusy:   # a session is acting on the symbol: the guardian's HALTED sweep retries every pass
            out.setdefault("busy", []).append(e["id"])
            continue
        out["cancelled" if got == "unfilled" else "left_for_fill_poll"].append(e["id"])
    return out


class EntryCancel:
    """Uses EntryDesk's trader, journal and entry-row storage (_rows, _settle)."""
    tr: Trader
    j: Journal
    _cancel_fails: dict[int, int]

    if TYPE_CHECKING:   # EntryDesk's
        async def act(self, kind: str, e: dict[str, Any], **args: Any) -> Any: ...

        def _rows(self, where: str = "1=1", args: tuple[Any, ...] = ()) -> list[dict[str, Any]]: ...

        async def _settle(self, eid: int, frm: str, **f: Any) -> bool: ...

    async def _cancel_entry(self, e: dict[str, Any], status: str, reason: str) -> str:
        """Withdraw one live row; returns withdraw's outcome. The row is re-read inside the symbol lock: one resolved
        meanwhile is left as it is ("filled" if it filled, else "unfilled"). A send of unknown outcome is first found by
        its client id; not on the book GRACE_S after the send, it is 'rejected', as the reconciler would label it. Only
        a limit that died unfilled is relabelled, compare-and-set against the status read here: a resolution another
        process wrote meanwhile is never overwritten."""
        async with self.tr.symbol_lock(e["symbol"], f"cancel entry #{e['id']}: {reason}"):
            now = self._rows("id=?", (e["id"],))
            if not now or now[0]["status"] not in LIVE:
                return "filled" if now and now[0]["status"] in ("filled", "error") else "unfilled"
            e, frm = now[0], now[0]["status"]
            if not e.get("order_id"):   # placing (its call died: a live one holds this lock) or unknown
                found = await located(self.tr.t, e)
                if found == "unfilled":
                    await self._settle(e["id"], frm, status="rejected",
                                 reason=f"{reason}; not on the exchange {GRACE_S} s after the send: never placed")
                    return "unfilled"
                if isinstance(found, str):
                    return found
                e = found
            got = await withdraw(self.tr, e, self.tr.alert, {"status": status, "reason": reason})
            if got == "unfilled":
                await self._settle(e["id"], frm, status=status, reason=reason, order_id=e["order_id"])
            elif got == "filled" and e.get("trade_id") is not None:   # a remainder: the poll settles the row
                await self.j.write(lambda j: partial.mark_pulled(j, e, status, reason))
        return got

    @refusals("cancel_external")
    async def cancel_external(self, symbol: str, order_id: str, reason: str) -> dict[str, Any]:
        """Cancel ONE resting order the desk did not place (a hand limit the owner forgot), by its exchange order id.
        Never the desk's own order or target (cancel-group, the plan) and never a conditional order (a stop someone
        set is protection: only regular orders are looked up). Believes only what filled (withdraw); outcome:
        cancelled (nothing filled), partly_filled (the rest is off the book) or filled_first (it filled whole before
        the cancel landed). An order with fills stays 'external_filled' and explains its position, as after a sweep;
        one the sweep had NOT recorded gets no such pass: its untracked position trips REDUCING here, with the
        guardian's alert (trading_adopt.trip_untracked). Demo first (trading_adopt.demo_first)."""
        from .trading_adopt import demo_first, trip_untracked  # lazy: the trading mixins import the entries modules
        symbol, oid, reason = to_symbol(symbol), str(order_id).strip(), (reason or "").strip()
        if why := demo_first(self.tr.ex, "cancel-external"):
            return {"ok": False, "error": why}
        if not reason or not oid:
            return {"ok": False, "error": "cancel-external needs the order id and a reason (it may be the owner's)"}
        async with self.tr.symbol_lock(symbol, f"cancel external order {oid}: {reason}"):
            await self.tr.ex.trade_markets(symbol)
            rows = self._rows("symbol=? AND order_id=?", (symbol, oid))
            if own := [r for r in rows if r["group_id"] != "external"]:
                return {"ok": False, "error": f"order {oid} is the desk's own entry #{own[0]['id']} (group "
                        f"{own[0]['group_id']}): cancel-group withdraws it"}
            if rows and rows[-1]["status"] != "external":
                return {"ok": False, "error": f"order {oid} is off the book already ({rows[-1]['status']})"}
            try:
                o = await self.tr.t.fetch_order(oid, symbol)
            except ccxt.OrderNotFound:
                return {"ok": False, "error": f"no regular order {oid} on {symbol} (a conditional order, a stop, is "
                        "never cancelled here)"}
            cid, swept = o.get("clientOrderId"), bool(rows)
            if clientid.ours(cid) or targets.is_target(cid):
                return {"ok": False, "error": f"order {oid} ({cid}) carries a shortbot client id: it is the desk's, "
                        "not an external order"}
            if not swept:   # journalled (and alerted) as the sweep would, then withdrawn
                if o.get("status") != "open":
                    return {"ok": False, "error": f"order {oid} is not resting ({o.get('status')})"}
                await entry_intents._external(self, {"side": str(o.get("side") or "").upper(), "price": o.get("price"),
                                                     "type": str(o.get("type") or "").upper(),
                                                     "origQty": o.get("amount")}, oid, cid, symbol)
                rows = self._rows("symbol=? AND order_id=? AND status='external'", (symbol, oid))
            e = rows[-1]
            got = await withdraw(self.tr, {**e, "prearm_id": None}, self.tr.alert,
                                 {"status": "external_gone", "reason": reason})
            if got == "unknown":
                return {"ok": False, "order_id": oid, "error": f"cancel of external order {oid} not confirmed: it may "
                        "still rest. Call again"}
            outcome, what = await self._external_outcome(symbol, oid, got)
            status = "external_gone" if got == "unfilled" else "external_filled"
            await self._settle(e["id"], "external", status=status, reason=f"{e['reason']}; {what}: {reason}")
            self.j.log_decision("cancel_external", reason, symbol, snapshot={
                "order_id": oid, "client_id": cid, "side": o.get("side"), "price": o.get("price"),
                "amount": o.get("amount"), "outcome": outcome, "swept": swept})
            if got == "filled" and not swept and not self.j.active_trade(symbol):
                try:   # the position its fill made has no journal row and the guardian will now find it "explained"
                    held = await self.tr.position(symbol)
                    size = float(held["contracts"]) if held else 0.0
                except Exception:  # noqa: BLE001 - unreadable: tripped as if it were there
                    size = float(o.get("amount") or 0)
                if size:
                    await trip_untracked(self.tr, symbol, size, "cancel_external", f"hand order {oid} had filled "
                                         "before the sweep recorded it. Close it, or journal it (adopt)")
            await self.tr.alert("external_cancelled", f"order {oid} ({o.get('side')} {o.get('amount')} @ "
                                f"{o.get('price')}, not placed by shortbot) {what} ({reason})" + (
                                    "" if got == "unfilled" else ": the position it made has the guardian's stop and "
                                    "no plan; adopt it or close it"), symbol, True)
            return {"ok": True, "symbol": symbol, "order_id": oid, "status": status, "outcome": outcome}

    async def _external_outcome(self, symbol: str, oid: str, got: str) -> tuple[str, str]:
        """(outcome, what happened) of a withdrawn external order: read back once when it has fills, to tell a part
        fill whose rest is off the book from an order that filled whole before the cancel landed."""
        if got == "unfilled":
            return "cancelled", "cancelled by the desk"
        try:
            o = await self.tr.t.fetch_order(oid, symbol)
        except Exception:  # noqa: BLE001 - it has fills either way; how many stays unread
            return "filled_first", "has fills (how much is unread); nothing of it rests"
        filled, amount = float(o.get("filled") or 0), float(o.get("amount") or 0)
        if o.get("status") in clientid.DEAD and filled < amount:
            return "partly_filled", f"had filled {filled:g} of {amount:g}; the rest is cancelled by the desk"
        return "filled_first", f"filled whole ({filled:g}) before the desk's cancel landed"

    def _filled_sibling(self, e: dict[str, Any]) -> int | None:
        """Another order of this OCO group that already filled ('error' = a fill was reported; pending with a trade =
        filled in part, entries_partial.py); a split entry's own legs don't count, they are one unit."""
        rows = self._rows("group_id=? AND id!=? AND (status IN ('filled','error') OR (status='pending' AND trade_id IS "
                          "NOT NULL))"
                          " AND (tranche IS NULL OR tranche IS NOT ?)", (e["group_id"], e["id"], e.get("tranche")))
        return rows[0]["id"] if rows else None

    async def cancel_group(self, group_id: str, reason: str, keep: int | None = None,
                           keep_tranche: str | None = None) -> dict[str, Any]:
        """Every order of the group that is or may be on the book, sends of unknown outcome too: an OCO sibling whose
        answer was lost must not fill after the group's first fill (#60).
        filled_first: filled before the cancel landed, so they keep their stops and the guardian's poll adopts them.
        cancel_failed: (maybe) still live; call again (after a fill, the poll's sibling rule retries it). A symbol busy
        with a session's action is one of those: the fill that called this must still be journalled and announced.
        remainders_cancelled (only when there are some): rows filled in part whose resting rest is now cancelled; what
        filled stays in its trade, the poll settles the row."""
        got: dict[str, list[int]] = {"unfilled": [], "filled": [], "unknown": [], "rest": []}
        for e in self._rows(RESTING + " AND group_id=?", (group_id,)):
            if e["id"] != keep and not (keep_tranche and e.get("tranche") == keep_tranche):
                try:
                    out = await self._cancel_entry(e, "cancelled", reason)
                except SymbolBusy as x:
                    log.info("entry #%s not cancelled: %s", e["id"], x)
                    out = "unknown"
                got["rest" if out == "filled" and e.get("trade_id") else out].append(e["id"])
        return {"ok": not got["unknown"], "cancelled": len(got["unfilled"]), "filled_first": got["filled"],
                "cancel_failed": got["unknown"], **({"remainders_cancelled": got["rest"]} if got["rest"] else {})}

    async def _cancel_by_rule(self, e: dict[str, Any], why: str) -> str:
        """A cancel rule fired. When the OCO retry keeps failing, a second order stays live in a group that already
        filled: tell a human once per streak of CANCEL_FAILS_ALERT failures in a row. The guardian's poll decides it,
        the cancel is an intent of its job (entries_acts.cancel_entry)."""
        got: str = await self.act("cancel_entry", e, status="cancelled", reason=why)   # cancel_entry's answer
        if got != "unknown" or not why.startswith("sibling #"):
            self._cancel_fails.pop(e["id"], None)
            return got
        n = self._cancel_fails[e["id"]] = self._cancel_fails.get(e["id"], 0) + 1
        if n == CANCEL_FAILS_ALERT:
            await self.tr.alert("entry_cancel_failing", f"entry #{e['id']}: cancel failing ({why}), order still live",
                                e["symbol"], True)
        return got
