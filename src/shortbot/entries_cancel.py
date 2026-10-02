"""Withdrawing limit entries (mixed into EntryDesk, entries.py): `withdraw` pulls one order and believes only what
filled; `_cancel_entry` does it for one journal row inside the symbol lock, compare-and-set; `cancel_group` for every
order of an OCO group that is or may be on the book; `_cancel_by_rule` counts a failing sibling-retry cancel. The
kill switch and entry_gates withdraw resting rows through `_cancel_entry` too (killswitch_ops.cancel_resting).
"""

from __future__ import annotations

import logging
from typing import Any

import ccxt.async_support as ccxt

from . import errors
from .entry_gates import RESTING
from .entry_intents import GRACE_S, located
from .symlock import SymbolBusy

log = logging.getLogger("shortbot.entries")

DEAD = ("canceled", "expired", "rejected")
CANCEL_FAILS_ALERT = 3   # sibling-retry cancels failed in a row before a human is told (once per streak)
LIVE = ("pending", "placing", "unknown")   # the desk's rows that are or may be on the book (RESTING)


async def drop_prearm(t, symbol: str, prearm_id: Any, alert=None, why: str = "") -> bool:
    """Cancel an entry's pre-armed stop. True when it is off the book: cancelled now, or Gone (-2011: triggered or
    cancelled before). Anything else leaves it maybe resting, a reduce-only BUY stop that would cut the next short on
    the symbol: told (`alert`: Trader.alert's shape), and the orphan sweep (orphans.py) cancels it once its entry is
    final. Callers hold the symbol lock."""
    try:
        await t.cancel_order(prearm_id, symbol, params={"trigger": True})
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


async def withdraw(t, e: dict[str, Any], alert=None) -> str:
    """Cancel a pending entry's limit FIRST, then decide from what actually filled:
    - "unfilled": the limit is dead with nothing filled, so its pre-armed stop is cancelled too
    - "filled": it (partly) filled before the cancel landed: the stop and the pending row stay, the poll adopts it
    - "unknown": the cancel failed (network, clock, a send not found yet): nothing changes, a later pass retries
    Binance answers -2011 (ccxt OrderNotFound) for ANY order no longer in the book, filled or dead, so fetch_order
    decides (MOVR 2026-10-01: a swallowed -2011 stripped a filled order's stop and left a naked short).
    A row with no order id (a send of unknown outcome) is first looked up by its client id (entry_intents.located).
    `alert`: told when the pre-armed stop of an unfilled limit can't be cancelled (drop_prearm)."""
    if not e.get("order_id"):
        e = await located(t, e)
        if isinstance(e, str):
            return e
    try:
        o = await t.cancel_order(e["order_id"], e["symbol"]) or {}
    except ccxt.OrderNotFound:
        try:
            o = await t.fetch_order(e["order_id"], e["symbol"])
        except Exception:  # noqa: BLE001 - unread: "unknown", the caller retries next pass (and streak-alerts)
            return "unknown"
        if not float(o.get("filled") or 0) and o.get("status") not in DEAD:
            return "unknown"
    except Exception:  # noqa: BLE001 - not sent, or sent with the outcome unknown: the order may still rest
        return "unknown"
    if float(o.get("filled") or 0) > 0:
        return "filled"
    if e.get("prearm_id"):
        await drop_prearm(t, e["symbol"], e["prearm_id"], alert, f"entry #{e.get('id')} withdrawn unfilled")
    return "unfilled"


class EntryCancel:
    """Uses EntryDesk's trader, journal and entry-row storage (_rows, _settle)."""

    async def _cancel_entry(self, e: dict, status: str, reason: str) -> str:
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
                    self._settle(e["id"], frm, status="rejected",
                                 reason=f"{reason}; not on the exchange {GRACE_S} s after the send: never placed")
                    return "unfilled"
                if isinstance(found, str):
                    return found
                e = found
            got = await withdraw(self.tr.t, e, self.tr.alert)
            if got == "unfilled":
                self._settle(e["id"], frm, status=status, reason=reason, order_id=e["order_id"])
        return got

    def _filled_sibling(self, e: dict) -> int | None:
        """Another order of this OCO group that already filled ('error' = a fill was reported); a split entry's own
        legs don't count, they are one unit."""
        rows = self._rows("group_id=? AND id!=? AND status IN ('filled','error')"
                          " AND (tranche IS NULL OR tranche IS NOT ?)", (e["group_id"], e["id"], e.get("tranche")))
        return rows[0]["id"] if rows else None

    async def cancel_group(self, group_id: str, reason: str, keep: int | None = None,
                           keep_tranche: str | None = None) -> dict[str, Any]:
        """Every order of the group that is or may be on the book, sends of unknown outcome too: an OCO sibling whose
        answer was lost must not fill after the group's first fill (#60).
        filled_first: filled before the cancel landed, so they keep their stops and the guardian's poll adopts them.
        cancel_failed: (maybe) still live; call again (after a fill, the poll's sibling rule retries it). A symbol busy
        with a session's action is one of those: the fill that called this must still be journalled and announced."""
        got: dict[str, list[int]] = {"unfilled": [], "filled": [], "unknown": []}
        for e in self._rows(RESTING + " AND group_id=?", (group_id,)):
            if e["id"] != keep and not (keep_tranche and e.get("tranche") == keep_tranche):
                try:
                    got[await self._cancel_entry(e, "cancelled", reason)].append(e["id"])
                except SymbolBusy as x:
                    log.info("entry #%s not cancelled: %s", e["id"], x)
                    got["unknown"].append(e["id"])
        return {"ok": not got["unknown"], "cancelled": len(got["unfilled"]), "filled_first": got["filled"],
                "cancel_failed": got["unknown"]}

    async def _cancel_by_rule(self, e: dict, why: str) -> str:
        """A cancel rule fired. When the OCO retry keeps failing, a second order stays live in a group that already
        filled: tell a human once per streak of CANCEL_FAILS_ALERT failures in a row."""
        got = await self._cancel_entry(e, "cancelled", why)
        if got != "unknown" or not why.startswith("sibling #"):
            self._cancel_fails.pop(e["id"], None)
            return got
        n = self._cancel_fails[e["id"]] = self._cancel_fails.get(e["id"], 0) + 1
        if n == CANCEL_FAILS_ALERT:
            await self.tr.alert("entry_cancel_failing", f"entry #{e['id']}: cancel failing ({why}), order still live",
                                e["symbol"], True)
        return got
