"""The guardian's entry-row sends as intents (#36 gateway step 2b, docs/GATEWAY.md §4.2). The fill poll (entries.poll),
the withdrawal sweeps (entry_gates) and the entry reconciler (entry_intents) decide from reads; these handlers send,
each re-reading its row or position under the symbol lock as before, and answer in today's shapes. ctx = the EntryDesk
(submitted through EntryDesk.gw); a handler's memory (the fill-remainder and round-trip streaks) is that desk's.

protect_fill, cancel_entry and withdraw_entries are late_ok (gateway.LATE_OK, where each is justified); the rest
journal first: a forgotten entry's pre-arm, an abandoned or late order and a dead call's split (the poll protects any
fill they make), a dead entry's pre-arm, and orphaned stops (orphans.py)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from . import entries_fills as fills
from . import errors, targets_sync
from .entries_cancel import CANCEL_FAILS_ALERT, cancel_resting, drop_prearm, withdraw
from .entry_intents import unit
from .gateway import handler

if TYPE_CHECKING:
    from .entries import EntryDesk

log = logging.getLogger("shortbot.entries")


def _row(desk: EntryDesk, entry_id: int) -> dict[str, Any]:
    return desk._rows("id=?", (entry_id,))[0]


# ------------------------------------------------------------ the fill poll
@handler("protect_fill", late_ok=True)
async def protect_fill(desk: EntryDesk, entry_id: int, order: dict[str, Any]) -> dict[str, Any]:
    """ProtectFill: the fill the poll found (`order`, as it fetched it) protected and journalled, then its OCO siblings
    cancelled (fills.announce and the round-trip paths: one symbol lock at a time)."""
    rows = desk._rows("id=?", (entry_id,))
    return await on_fill(desk, rows[0], order) if rows else {"entry_id": entry_id, "event": "resolved_elsewhere"}


async def on_fill(desk: EntryDesk, e: dict[str, Any], o: dict[str, Any]) -> dict[str, Any]:
    """Protect, then journal, both inside the symbol lock: a session's close() between the two would find no trade,
    strip the new stop and finalize nothing, and the fill would then be journalled as an open trade on a flat
    symbol (#60). The row is re-read inside the lock. The remainder of a partial fill is pulled first (withdraw:
    what filled is believed, the pre-arm is not touched here). Make before break: the pre-armed stop goes only once
    the whole-position stop rests; if that stop can't be placed the pre-arm stays and the row stays pending
    (StopNotPlaced). After the lock (siblings may rest on other symbols): the round trip, the OCO cancel, alerts."""
    tr, sym = desk.tr, e["symbol"]
    async with tr.symbol_lock(sym, f"entry #{e['id']} filled: protect and journal it"):
        now = desk._rows("id=? AND status='pending'", (e["id"],))
        if not now:
            return {"entry_id": e["id"], "event": "resolved_elsewhere"}
        e = now[0]
        rest = await withdraw(tr, {**e, "prearm_id": None}) if o.get("status") == "open" else "filled"
        pos = await tr.verified_position(sym)
        try:
            cps = await _protect(desk, e) if pos else []
        except fills.StopNotPlaced as x:   # -4509 on a flat position: closed again before its stop landed
            if errors.classify(x.__cause__ or x) is not errors.ReduceOnlyRejected or not await tr.flat_now(sym):
                raise
            pos, cps = None, []   # the round trip below journals it
        if rest != "unknown":
            desk._cancel_fails.pop(e["id"], None)
            await fills.plan_stop(desk, e, cps)
            trades = await fills.history(tr.t, e, o)
            done = await desk.j.write(lambda _j: fills.adopt(desk, e, o, pos, trades)) if pos else None
            if pos:   # another trade's targets never meet this fill (#65); its own rest at once (#71)
                await targets_sync.clear_stale(tr, sym, (desk.j.active_trade(sym) or {}).get("id"))
                await targets_sync.at_entry(tr, sym, f"entry #{e['id']} filled")
    if rest == "unknown":
        return await _rest_unknown(desk, e)
    if not pos or done is None:   # filled and closed again before this poll (its pre-armed stop): from history
        return await fills.round_trip(desk, e, o, trades)
    return await fills.announce(desk, e, *done)


async def _protect(desk: EntryDesk, e: dict[str, Any]) -> list[dict[str, Any]]:
    """The whole-position stop (a pre-armed reduce-only stop of another leg or order is NOT that), then this
    entry's pre-arm goes. Returns the closePosition stops that already rested."""
    tr, sym = desk.tr, e["symbol"]
    cps = await tr.stop_orders(sym, close_position_only=True)
    if not cps:
        try:
            await tr.place_close_stop(sym, e["stop_price"], tries=3)
        except Exception as x:  # noqa: BLE001 - re-raised typed: the poll alerts at once, the pre-arm stays
            raise fills.StopNotPlaced(e, x) from x
    if e["prearm_id"]:   # the whole-position stop rests: the pre-arm goes (one left behind: drop_prearm tells)
        await drop_prearm(tr, sym, e["prearm_id"], tr.alert, f"entry #{e['id']} filled, its stop is on")
    return cps


async def _rest_unknown(desk: EntryDesk, e: dict[str, Any]) -> dict[str, Any]:
    """The fill is protected but its remainder's cancel failed: it may still rest and fill more, so the row stays
    pending (the next pass retries the cancel, then journals all that filled). A streak alerts once."""
    n = desk._cancel_fails[e["id"]] = desk._cancel_fails.get(e["id"], 0) + 1
    if n == CANCEL_FAILS_ALERT:
        await desk.tr.alert("entry_cancel_failing", f"entry #{e['id']}: filled in part, the rest won't cancel "
                            "(order still live); position protected, journalling waits", e["symbol"], True)
    return {"entry_id": e["id"], "event": "fill_unsettled"}


@handler("forget_entry")
async def forget_entry(desk: EntryDesk, entry_id: int) -> dict[str, Any] | None:
    """Not found past its GTD: Binance forgets an unfilled CANCELED/EXPIRED order 3 days after its creation and
    keeps any order with a fill for 90 days (Query-Order doc), so it expired unfilled. Its pre-armed stop goes."""
    e = _row(desk, entry_id)
    if e.get("prearm_id"):
        async with desk.tr.symbol_lock(e["symbol"], f"entry #{e['id']}: drop a forgotten entry's pre-arm"):
            await drop_prearm(desk.tr, e["symbol"], e["prearm_id"], desk.tr.alert,
                              f"entry #{e['id']} expired unfilled")
    why = "order forgotten by Binance (-2013) past its GTD: it expired unfilled"
    return {"entry_id": e["id"], "event": "expired", "why": why} if await desk._settle(
        e["id"], "pending", status="expired", reason=why) else None


@handler("cancel_entry", late_ok=True)
async def cancel_entry(desk: EntryDesk, entry_id: int, status: str, reason: str) -> str:
    """One live row withdrawn (EntryCancel._cancel_entry: re-read under the lock, compare-and-set): expiry, a cancel
    rule, the OCO sibling retry, a closed split's leg. Returns withdraw's outcome."""
    return await desk._cancel_entry(_row(desk, entry_id), status, reason)


@handler("withdraw_entries", late_ok=True)
async def withdraw_entries(desk: EntryDesk, reason: str, ids: list[int] | None = None) -> dict[str, list[int]]:
    """CancelEntries: the resting rows `ids` (None: every one resting now) withdrawn (entries_cancel.cancel_resting)."""
    if ids is None:
        return await cancel_resting(desk, reason)
    listed = set(ids)
    return await cancel_resting(desk, reason, lambda e: e["id"] not in listed)   # the rest are kept


# ------------------------------------------------------------ the entry reconciler (entry_intents.py)
@handler("withdraw_order")
async def withdraw_order(desk: EntryDesk, entry_id: int, order_id: str) -> str:
    """An order of row `entry_id` found resting although nobody tracks it there (an abandoned send, a late order of
    a dead row): cancelled first, what filled believed (withdraw). The reconciler journals the outcome."""
    r = _row(desk, entry_id)
    async with desk.tr.symbol_lock(r["symbol"], f"entry #{entry_id}: withdraw an untracked order"):
        return await withdraw(desk.tr, {**r, "order_id": order_id}, desk.tr.alert)


@handler("rollback_split")
async def rollback_split(desk: EntryDesk, entry_id: int, tranche: str) -> None:
    """A dead call's split (row `entry_id` was placing past GRACE_S): one unit, so its acked, never-pinned legs go."""
    legs = desk._rows("tranche=? AND status='pending'", (tranche,))
    async with desk.tr.symbol_lock(_row(desk, entry_id)["symbol"], f"entry #{entry_id}: roll back a dead call's split"):
        await desk._rollback([unit(x) for x in legs if "ladder_live" not in x["plan"]])


@handler("cancel_prearm")
async def cancel_prearm(desk: EntryDesk, entry_id: int, order_id: str) -> None:
    """A dead entry's pre-armed stop, matched by client id among the open stops: cancelled (Gone: off the book)."""
    sym = _row(desk, entry_id)["symbol"]
    try:
        async with desk.tr.symbol_lock(sym, f"entry #{entry_id}: cancel a dead entry's pre-arm"):
            await desk.tr.cancel(sym, order_id, trigger=True)
    except Exception as e:  # noqa: BLE001 - Gone: off the book already; anything else is raised to the reconciler
        if errors.classify(e) is not errors.Gone:
            raise
