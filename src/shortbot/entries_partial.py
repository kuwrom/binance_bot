"""A limit entry that fills in part keeps its remainder resting (owner decision 2026-10-05). Before, the fill poll
pulled the rest at the first fill: BEAMX entry #43 filled 3,827 of 33,791 at a touch, protect_fill cancelled the
other 29,964 (intent 01M44CHP17FBY9QT6HH77CV3J5) and the trade ran at ~5% of its planned size.

- first fill, order still open (PARTIALLY_FILLED), position live: the trade opens, or joins its group's open trade,
  for what filled, protected as any fill: the closePosition stop covers the whole position, so each later fill is
  protected the moment it lands (the pre-arm goes as usual). The row stays 'pending' with trade_id and filled_qty
  (PARTIAL): still on the book for the poll, the sweeps, open_short's one-trade-per-symbol refusal and the exposure
  caps, which count its remainder only (remaining).
- each later fill of the order: added to the SAME trade (entry price, peak amount and notional from the position), the
  stop checked against the plan's like any fill, targets re-synced, an `entry_increment` decision (a size event) and
  an action_fill alert per increment (more).
- the remainder dies by the usual rules (GTD expiry, TTL, cancel_below, the OCO sibling rule, a kill-switch / governor
  / market-pause withdrawal, cancel_entry_group) or with its trade: Trader.close pulls it before buying back
  (pull_for), and the poll cancels it within 5 s of any other close (stop, target, liquidation). It also goes the
  moment the trade shrinks or widens (any close, a target, widen_stop, the position below its peak) or its stop
  reaches its limit (breakeven, a trail, move_stop): for a short a remainder at or above the stop fills only after a
  stop-out, as a fresh position (must_go: hooked in Trader.close, move_stop and widen_stop, and checked by the poll
  every pass). Once the order is dead the row is settled 'filled' with what filled. The cascade rule doesn't apply: price is at the level already and
  the trade's stop bounds it. Never moved (amend refuses), and no new entry is placed on its symbol meanwhile.
- the rest is still pulled at the first fill (pull_reason): no live position (a round trip), a sibling of the group
  filled too (over the single-bet budget), or a split leg whose trade already closed.
- a remainder that filled after its trade closed is a fresh position nobody planned: protected at the plan stop and
  journalled as a new trade, alerted urgent (as a split leg that fills after its trade closed).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .entries import EntryDesk
    from .journal import Journal

log = logging.getLogger("shortbot.entries")
PARTIAL = "status='pending' AND trade_id IS NOT NULL"   # filled in part, journalled into its trade, the rest resting
EPS = 1e-9
REDUCED = ("partial_close", "close_incomplete", "target_fill", "widen_stop")   # size events that shrink or widen
_UNREAD: dict[str, Any] = {"unread": True}   # must_go: the position wasn't read (decide on the journal alone)
Done = tuple[dict[str, Any], tuple[str, bool] | None]   # (event, (alert, urgent) or None): fills.announce's input


def is_partial(e: dict[str, Any]) -> bool:
    return e.get("status") == "pending" and e.get("trade_id") is not None


def remaining(e: dict[str, Any]) -> float:
    """What can still fill: the whole amount, or a partial row's remainder (exposure, the governor's resize)."""
    amt = float(e.get("amount") or 0)
    return max(0.0, amt - float(e.get("filled_qty") or 0)) if is_partial(e) else amt


def pull_reason(desk: EntryDesk, e: dict[str, Any]) -> str | None:
    """Why the remainder of this entry's first fill must be pulled after all; None: it keeps resting."""
    if sib := desk._filled_sibling(e):
        return f"sibling #{sib} filled too"
    if e.get("tranche"):
        from .tranches import parent_closed
        if parent_closed(desk, e):
            return "its split entry's trade already closed"
    return None


def claim(o: dict[str, Any]) -> dict[str, Any]:
    """A first fill's row fields when its remainder rests: still pending, with what the ORDER filled so far (each later
    fill is counted against it)."""
    return {"status": "pending", "filled_qty": float(o.get("filled") or 0),
            **({"fill_price": float(o["average"])} if o.get("average") else {})}


def cas_first(desk: EntryDesk, eid: int, frm: str, **f: Any) -> bool:
    """claim()'s compare-and-set (EntryDesk._cas_row's shape): a pending row no fill was journalled into yet."""
    cur = desk.j.db.execute(f"UPDATE entries SET {', '.join(k + '=?' for k in f)} WHERE id=? AND status=? AND "
                            "trade_id IS NULL", (*f.values(), eid, frm))
    return cur.rowcount == 1


def _cas_more(j: Journal, e: dict[str, Any], **f: Any) -> bool:
    """A partial row's update, compare-and-set on the fill count it was read with."""
    cur = j.db.execute(f"UPDATE entries SET {', '.join(k + '=?' for k in f)} WHERE id=? AND status='pending' AND "
                       "trade_id=? AND filled_qty IS ?", (*f.values(), e["id"], e["trade_id"], e["filled_qty"]))
    return cur.rowcount == 1


def note(e: dict[str, Any], o: dict[str, Any]) -> str:
    """An action_fill's tail for a first fill whose remainder rests."""
    left = float(e["amount"]) - float(o.get("filled") or 0)
    until = datetime.fromtimestamp(e["expires_ms"] / 1000, UTC).isoformat(timespec="minutes")
    return (f"; filled {float(o.get('filled') or 0):g} of {e['amount']:g}: the remaining {left:g} rests at "
            f"{e['limit_price']} until {until} (each further fill joins this trade)")


def due(desk: EntryDesk, e: dict[str, Any], o: dict[str, Any]) -> bool:
    """A partial row needs its fill handler (the poll): more filled, its order ended, or its trade isn't open."""
    return (float(o.get("filled") or 0) > float(e.get("filled_qty") or 0) + EPS or o.get("status") != "open"
            or (desk.j.get_trade(e["trade_id"]) or {}).get("status") != "open")


def must_go(j: Journal, e: dict[str, Any], trade: dict[str, Any],
            pos: dict[str, Any] | None = _UNREAD) -> str | None:
    """Why an open trade's resting remainder must be cancelled now; None: it keeps resting.
    - the trade's stop is at or below the remainder's limit: for a short, price reaches the limit only through the
      stop, so the remainder could fill only after a stop-out, as a fresh position (breakeven, a trail, move_stop)
    - the trade got smaller or wider (REDUCED decisions: a target, a partial close, a widen and its size cut), or the
      position (`pos`, when given; None = flat) is below the trade's peak: a refill would rebuild what we started to
      take off, or grow the risk past 1R"""
    stop, limit = float(trade.get("stop_price") or 0), float(e["limit_price"])
    if 0 < stop <= limit * (1 + 1e-9):
        return f"its stop {stop:g} is at or below the remainder's limit {limit:g} (it could fill only after a stop-out)"
    rows = j._rows(f"SELECT action, ts FROM decisions WHERE symbol=? AND ts >= ? AND COALESCE(mode, 'demo')=? AND "
                   f"action IN ({','.join('?' * len(REDUCED))}) ORDER BY id DESC LIMIT 1",
                   (e["symbol"], trade.get("opened_at") or "", j.mode, *REDUCED))
    if rows:
        return f"the trade was reduced or widened ({rows[0]['action']} at {rows[0]['ts']})"
    peak, now = float(trade.get("amount") or 0), float((pos or {}).get("contracts") or 0)
    if pos is not _UNREAD and now < peak * (1 - 1e-9):
        return f"the position shrank to {now:g} from its peak {peak:g}"
    return None


async def must_go_now(desk: EntryDesk, e: dict[str, Any]) -> bool:
    """The poll's check, each pass, for a remainder resting beside its open trade (must_go, the position read best
    effort: unread, it decides on the journal alone)."""
    trade = desk.j.get_trade(e["trade_id"]) or {}
    if trade.get("status") != "open":
        return False   # due() has it
    if must_go(desk.j, e, trade, _UNREAD):
        return True
    try:
        pos = await desk.tr.position(e["symbol"])
    except Exception as x:  # noqa: BLE001 - the next pass reads it again
        log.warning("entry #%s: position unread for its remainder check: %s", e["id"], x)
        return False
    return must_go(desk.j, e, trade, pos) is not None


PULLED = "remainder "   # a partial row's reason once the desk cancelled its rest (mark_pulled): its end's label


def mark_pulled(j: Journal, e: dict[str, Any], status: str, why: str) -> None:
    """The desk cancelled a partial row's remainder (a unit's statement): why, kept for the row's settling (end_reason).
    The row itself stays pending: the poll settles it with whatever filled until the cancel landed."""
    j.db.execute("UPDATE entries SET reason=? WHERE id=? AND status='pending' AND trade_id IS NOT NULL",
                 (f"{PULLED}{status}: {why}", e["id"]))


def end_reason(j: Journal, e: dict[str, Any], o: dict[str, Any], filled: float) -> str:
    """A settled partial row's reason: filled in full, or what filled and how the rest ended (the desk's own cancel,
    mark_pulled; else the order's status as dead_label reads it)."""
    if o.get("status") == "closed" or filled >= float(e["amount"]) - EPS:
        return f"filled in full, in parts ({filled:g})"
    if str(e.get("reason") or "").startswith(PULLED):
        return f"filled {filled:g} of {e['amount']:g}; the {e['reason']}"
    from .entries_cancel import dead_label
    st, why = dead_label(j, e, o)
    return f"filled {filled:g} of {e['amount']:g}; the rest {st}: {why}"


async def more(desk: EntryDesk, e: dict[str, Any], o: dict[str, Any]) -> Done:
    """entries_acts.on_fill for a partial row, inside its symbol lock, the row re-read: a later fill of the order, the
    order's end, or its trade's close (module doc)."""
    from . import targets_sync
    from .entries_acts import _protect
    from .entries_fills import plan_stop
    tr, sym = desk.tr, e["symbol"]
    trade = desk.j.get_trade(e["trade_id"]) or {}
    if trade.get("status") != "open":
        return await _orphaned(desk, e, o)
    pos = await tr.verified_position(sym)
    if o.get("status") == "open" and (why := must_go(desk.j, e, trade, pos)):   # the rest goes first (must_go)
        if await _pull(desk, e, why) == "unknown":
            return {"entry_id": e["id"], "event": "fill_unsettled"}, None
        e = {**e, "reason": f"{PULLED}cancelled: {why}"}
        o = await tr.t.fetch_order(e["order_id"], sym)   # what filled until the cancel landed
        pos = await tr.verified_position(sym)
    if float(o.get("filled") or 0) <= float(e["filled_qty"] or 0) + EPS:
        return await _ended(desk, e, o)
    if not pos:
        return await _gone(desk, e, o)
    await plan_stop(desk, e, await _protect(desk, e))
    done: Done = await desk.j.write(lambda _j: _journal(desk, e, o, pos))
    if done[1] is not None:
        await targets_sync.at_entry(tr, sym, f"entry #{e['id']} filled more")
    return done


def _journal(desk: EntryDesk, e: dict[str, Any], o: dict[str, Any], pos: dict[str, Any]) -> Done:
    """An increment, one unit inside the symbol lock: the row's fill count, the trade's size, a size-event decision."""
    j, tid = desk.j, e["trade_id"]
    before, filled = float(e["filled_qty"] or 0), float(o.get("filled") or 0)
    f: dict[str, Any] = {"filled_qty": filled, **({"fill_price": float(o["average"])} if o.get("average") else {})}
    ended = o.get("status") != "open"
    if ended:
        f.update(status="filled", reason=end_reason(j, e, o, filled))
    if not _cas_more(j, e, **f):
        return {"entry_id": e["id"], "event": "resolved_elsewhere"}, None
    fill, qty = float(pos["entryPrice"]), float(pos["contracts"])
    peak = max(float((j.get_trade(tid) or {}).get("amount") or 0), qty)   # amount = peak size (as tranches.join)
    j.update_trade(tid, entry_price=fill, amount=peak, notional=round(fill * qty, 2))
    add = round(filled - before, 10)
    j.log_decision("entry_increment", f"entry #{e['id']} filled {add:g} more ({filled:g} of {e['amount']:g}): "
                   f"joined trade #{tid}", e["symbol"], snapshot={"trade_id": tid, "entry_id": e["id"], "added": add,
                                                                   "filled": filled, "amount": e["amount"],
                                                                   "avg_entry": fill, "contracts": qty})
    tail = f["reason"] if ended else f"the remaining {float(e['amount']) - filled:g} still rests"
    return ({"entry_id": e["id"], "event": "increment", "trade_id": tid, "added": add, "filled": filled, "qty": qty,
             "avg": fill},
            (f"entry #{e['id']} filled {add:g} more @ {e['limit_price']} ({filled:g} of {e['amount']:g}): trade "
             f"#{tid} is {qty:g} @ avg {fill:.6g}, its stop covers the whole position; {tail}", False))


async def _ended(desk: EntryDesk, e: dict[str, Any], o: dict[str, Any]) -> Done:
    """No new fill: still resting (nothing to do), or the order ended (expired, cancelled): the row is settled. The
    event is the guardian's entry_cancelled / entry_expired notice, as for an unfilled entry."""
    if o.get("status") == "open":
        return {"entry_id": e["id"], "event": "resting"}, None
    why = end_reason(desk.j, e, o, float(e["filled_qty"] or 0))
    ok = await desk.j.write(lambda j: _cas_more(j, e, status="filled", reason=why))
    ev = "expired" if o.get("status") == "expired" else "cancelled"
    return {"entry_id": e["id"], "event": ev if ok else "resolved_elsewhere", "trade_id": e["trade_id"],
            "why": f"remainder (trade #{e['trade_id']}): {why}"}, None


async def _pull(desk: EntryDesk, e: dict[str, Any], why: str) -> str:
    """Cancel the remainder (withdraw: what filled is believed; the pre-arm is not this path's). A cancel that keeps
    failing is told once per streak: the remainder may still fill."""
    from .entries_cancel import CANCEL_FAILS_ALERT, withdraw
    got = await withdraw(desk.tr, {**e, "prearm_id": None}, desk.tr.alert, {"status": "cancelled", "reason": why})
    if got != "unknown":
        desk._cancel_fails.pop(e["id"], None)
        await desk.j.write(lambda j: mark_pulled(j, e, "cancelled", why))
        return got
    n = desk._cancel_fails[e["id"]] = desk._cancel_fails.get(e["id"], 0) + 1
    if n == CANCEL_FAILS_ALERT:
        await desk.tr.alert("entry_cancel_failing", f"entry #{e['id']}: {why}, but its remainder won't cancel (order "
                            "still live, retried every poll): it may still fill", e["symbol"], True)
    return got


async def _gone(desk: EntryDesk, e: dict[str, Any], o: dict[str, Any]) -> Done:
    """More filled, but the trade's position is already flat (closed on the exchange between polls): the fills are the
    trade's (own_entries), reconcile closes it with them; the rest goes."""
    tid, filled = e["trade_id"], float(o.get("filled") or 0)
    why = f"trade #{tid}'s position closed"
    ended = o.get("status") != "open" or await _pull(desk, e, why) != "unknown"
    f: dict[str, Any] = {"filled_qty": filled}
    if ended:
        f.update(status="filled", reason=f"filled {filled:g} of {e['amount']:g}; the rest cancelled: {why}")
    if not await desk.j.write(lambda j: _cas_more(j, e, **f)):
        return {"entry_id": e["id"], "event": "resolved_elsewhere"}, None
    return ({"entry_id": e["id"], "event": "fill_linked", "trade_id": tid},
            (f"entry #{e['id']} filled {filled - float(e['filled_qty'] or 0):g} more into trade #{tid}, whose position "
             "closed on the exchange before the poll: reconcile closes it with these fills in it", True))


async def _orphaned(desk: EntryDesk, e: dict[str, Any], o: dict[str, Any]) -> Done:
    """Its trade closed: the remainder is cancelled and the row settled. One that filled after the close made a fresh
    position nobody planned: protected at the plan stop, journalled as a new trade, alerted urgent."""
    from . import entries_fills as fills
    from . import targets_sync
    from .entries_acts import _protect
    tr, sym, tid = desk.tr, e["symbol"], e["trade_id"]
    why = f"trade #{tid} closed"
    if o.get("status") == "open":
        if await _pull(desk, e, why) == "unknown":
            return {"entry_id": e["id"], "event": "fill_unsettled"}, None
        e = {**e, "reason": f"{PULLED}cancelled: {why}"}
        o = await tr.t.fetch_order(e["order_id"], sym)   # what filled until the cancel landed
    filled = float(o.get("filled") or 0)
    if filled > float(e["filled_qty"] or 0) + EPS and (pos := await tr.verified_position(sym)):
        await fills.plan_stop(desk, e, await _protect(desk, e))
        trades = await fills.history(tr.t, e, o)
        ev, alert = await desk.j.write(lambda _j: fills.adopt(desk, e, o, pos, trades))
        if alert is not None:
            await targets_sync.clear_stale(tr, sym, (desk.j.active_trade(sym) or {}).get("id"))
            await targets_sync.at_entry(tr, sym, f"entry #{e['id']}'s remainder filled")
            alert = (f"entry #{e['id']}'s remainder filled after its trade #{tid} closed: a fresh position. "
                     + alert[0], True)
        return ev, alert
    reason = end_reason(desk.j, e, o, filled)
    ok = await desk.j.write(lambda j: _cas_more(j, e, status="filled", filled_qty=filled, reason=reason))
    return {"entry_id": e["id"], "event": "cancelled" if ok else "resolved_elsewhere", "trade_id": tid,
            "why": f"remainder (trade #{tid}): {reason}"}, None


def remainders(j: Journal, trade_id: int) -> list[dict[str, Any]]:
    from .tranches import _entries
    return _entries(j, f"{PARTIAL} AND trade_id=?", (trade_id,))


async def pull_for(tr: Any, symbol: str, reason: str, stop: float | None = None) -> list[int]:
    """The symbol's open trade's resting remainders cancelled at once, inside the caller's symbol lock (tr: the Trader,
    seen from a mixin): Trader.close before any close buys back (a full close: before the position is read, so none
    refills it; a partial one: the trade is shrinking), widen_stop, and move_stop with `stop` (only the remainders
    whose limit is at or above the new stop: must_go). Returns the ids whose cancel failed: the poll retries them
    (must_go), and after a full close tranches.cancel_legs tries again. The poll settles the rows."""
    from .entries_cancel import withdraw
    trade = tr.j.active_trade(symbol)
    failed = []
    for e in remainders(tr.j, trade["id"]) if trade else []:
        if stop is not None and float(e["limit_price"]) * (1 + 1e-9) < stop:
            continue   # its stop still sits above the remainder's limit: it may rest
        got = await withdraw(tr, {**e, "prearm_id": None}, tr.alert, {"status": "cancelled", "reason": reason})
        if got == "unknown":
            failed.append(e["id"])
        else:
            await tr.j.write(lambda j, e=e: mark_pulled(j, e, "cancelled", reason))
    return failed
