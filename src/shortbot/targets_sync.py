"""Resting plan targets on the exchange (#65; the book and the journal: targets.py). Every function here sends orders
inside the symbol's lock (taken here; reentrant for a caller that holds it, symlock.py).

- sync: settle what the book shows (fills, orders that left it), cancel what doesn't match the trade's book (a
  resize, a target hit or crossed, a plan change, flat, another trade's or an unjournalled `sb-t` order), then place
  what is missing: a reduce-only GTC BUY LIMIT under a client id journalled first (#9, #13). An ambiguous send is
  resolved by that id, never re-sent: 'unknown' until found, 'lost' once not found GRACE_S after the send. A definite
  refusal ('rejected') isn't retried at the same price and size for REJECT_HOLD_S: the guardian's market fallback
  covers that target, as before #65.
- pull: cancel every target on the symbol and journal what they filled. Before a partial close or a size cut (a
  target filling at the same moment would close more than the plan wants), and once flat, whatever Binance does on
  its own (it expires reduce-only orders at flat, staff answer dev.binance.vision/t/802; the desk doesn't rely on it).
- settle_flat (guardian, before reconcile): symbols with live target rows but no position: pulled, so the fills that
  closed the trade are journalled as targets before reconcile finalizes it.
- clear_stale: the re-entry guard. A new short on a symbol cancels any target of another trade first.
- at_entry: sync right after an entry's stop rests (open_short, a limit-entry fill), never failing the entry (#71).
- due: the guardian's lock-free check, so its 60 s pass takes the lock only when something is to settle or change.
Binance behaviour the docs leave open (whether an untriggered bridge or pre-arm takes part in the reduce-only
competition; what a non-order position cut, e.g. a partial ADL, does to them) is covered by re-listing every pass."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import ccxt.async_support as ccxt

from . import clientid, errors, targets
from .steps import floor_amount
from .symlock import SymbolBusy

log = logging.getLogger("shortbot.targets")
GRACE_S = 90           # entry_intents.GRACE_S: longer than any send takes; not found by then = never placed
REJECT_HOLD_S = 3600
FINAL = {"closed": "filled", "canceled": "cancelled", "expired": "expired", "rejected": "rejected"}


async def listed(tr, symbol: str) -> dict[str, dict]:
    """Our targets open on the symbol (regular open orders, weight 1), by client id."""
    return {o["clientOrderId"]: o for o in await tr.t.fetch_open_orders(symbol)
            if targets.is_target(o.get("clientOrderId"))}


def _age(row: dict) -> float:
    return time.time() - datetime.fromisoformat(row["created_at"]).timestamp()


def _book(tr, symbol: str, trade: dict | None, pos: dict | None) -> list[dict]:
    """The trade's wanted book, with exchange prices; [] when flat or the trade isn't open."""
    if not trade or trade.get("status") != "open" or not trade.get("entry_price") or not pos:
        return []
    want = targets.desired(trade, pos, lambda x: floor_amount(tr.t, symbol, x))
    return [{**w, "price": float(tr.t.price_to_precision(symbol, w["target"]))} for w in want]


def _plan(tr, symbol: str, trade: dict | None, pos: dict | None, on_book: dict, rows: list[dict]):
    """(orders to cancel [(order, row or None, why)], targets to place)."""
    want, tid = _book(tr, symbol, trade, pos), (trade or {}).get("id")
    by_cid = {r["client_id"]: r for r in targets.rows(tr.j, "symbol=?", (symbol,))}
    kept, cancel = set(), []
    for cid, o in on_book.items():
        r = by_cid.get(cid)
        rem = float(o.get("amount") or 0) - float(o.get("filled") or 0)
        w = next((w for w in want if id(w) not in kept and r and r["trade_id"] == tid and w["target"] == r["target"]
                  and w["price"] == float(o.get("price") or 0) and abs(w["qty"] - rem) < 1e-9), None)
        if w is not None:
            kept.add(id(w))
            continue
        why = ("no journal row for it" if r is None else "flat: no position" if not pos else
               f"trade #{r['trade_id']} is not the open trade" if r["trade_id"] != tid else
               f"resize/reprice: {rem:g} @ {o.get('price')} no longer the plan's"
               if any(w["target"] == r["target"] for w in want) else
               "target no longer rests (hit, crossed or plan changed)")
        cancel.append((o, r, why))
    waiting = {r["target"] for r in rows if r["status"] in ("placing", "unknown") and r["trade_id"] == tid}
    held = {(r["target"], r["price"], r["qty"]) for r in targets.rows(
        tr.j, "symbol=? AND trade_id=? AND status='rejected' AND updated_at >= ?",
        (symbol, tid, datetime.fromtimestamp(time.time() - REJECT_HOLD_S, timezone.utc).isoformat(timespec="seconds")))}
    place = [w for w in want if id(w) not in kept and w["target"] not in waiting
             and (w["target"], w["price"], w["qty"]) not in held]
    return cancel, place


async def _found(tr, symbol: str, row: dict) -> dict | None:
    """The row's order as Binance answers now (by order id, else client id); None when Binance doesn't know it."""
    try:
        if row.get("order_id"):
            return await tr.t.fetch_order(row["order_id"], symbol)
        return await clientid.lookup(tr.t, symbol, row["client_id"])
    except ccxt.OrderNotFound:
        return None


async def _landed(tr, row: dict, o: dict, why: str | None = None) -> None:
    """Journal what the exchange says about the row's order: on the book (fills so far), or final. Binance caps a
    reduce-only order larger than the position at it instead of refusing it (demo, probe_result 393): a target sent
    as the position shrank lands smaller, and its row takes the size it has."""
    st, amount = o.get("status"), float(o.get("amount") or 0)
    if row["status"] in ("placing", "unknown") and 0 < amount < float(row["qty"]) - 1e-12:
        targets.move(tr.j, row, row["status"], f"capped at the position by Binance: {amount:g} of {row['qty']:g}",
                     qty=amount)
    if st == "open":
        if row["status"] != "resting":
            targets.move(tr.j, row, "resting", "on the book", order_id=str(o["id"]))
        targets.note_fill(tr, row, o, final=False)
        return
    targets.note_fill(tr, row, o, final=True)
    new = FINAL.get(st, "gone")
    targets.move(tr.j, row, new, why or f"left the book: {st}", ex=st, order_id=str(o.get("id") or row.get("order_id")))
    if why is None and new in ("cancelled", "expired"):   # Binance took it away: the drill wants to see each one
        tr.j.log_decision("target_gone", f"target {row['target']:g} left the book: {st}", row["symbol"], snapshot={
            "trade_id": row["trade_id"], "target": row["target"], "order": row.get("order_id"), "status": st,
            "filled": row.get("filled"), "qty": row["qty"]})


async def _settle(tr, symbol: str, on_book: dict) -> None:
    for row in targets.rows(tr.j, f"symbol=? AND status NOT IN ({','.join('?' * len(targets.LIVE))})",
                            (symbol, *targets.LIVE)):
        if row["client_id"] in on_book:   # written off ('lost', 'gone'), yet it landed late: tracked again
            await _landed(tr, row, on_book[row["client_id"]])
    for row in targets.live(tr.j, symbol):
        o = on_book.get(row["client_id"])
        if o is None:
            o = await _found(tr, symbol, row)
        if o is not None:
            await _landed(tr, row, o)
        elif row["status"] == "resting":
            targets.move(tr.j, row, "gone", "off the book and unknown to Binance (unfilled, forgotten)")
        elif _age(row) > GRACE_S:
            targets.move(tr.j, row, "lost", "never reached the book: not found by its client id")


async def _cancel(tr, symbol: str, o: dict, row: dict | None, why: str) -> bool:
    """Cancel one target; journal its fills and why it left. False when the cancel is in doubt (it may still rest)."""
    try:
        resp = await tr.cancel(symbol, o["id"])
    except ccxt.OrderNotFound:   # filled or gone meanwhile: what Binance says now decides
        resp = None
    except Exception as e:  # noqa: BLE001 - in doubt: the next listing decides
        if errors.classify(e) is errors.ClockDrift:
            await tr.ex.resync_time()
        log.warning("target %s on %s: cancel in doubt: %s: %s", o.get("id"), symbol, type(e).__name__, e)
        return False
    if row is None:
        await tr.alert("target_orphan_cancelled", f"target order {o.get('clientOrderId')} ({o.get('amount')} @ "
                       f"{o.get('price')}) had no journal row: cancelled ({why})", symbol, False)
        return True
    if resp is None:
        got = await _found(tr, symbol, row)
        if got is not None:
            await _landed(tr, row, got)
        return True
    final = {**o, **{k: v for k, v in resp.items() if v is not None}, "status": resp.get("status") or "canceled"}
    await _landed(tr, row, final, why)
    return True


async def _place(tr, symbol: str, trade: dict, w: dict) -> None:
    cid = clientid.new("target", trade["id"])
    rid = targets.add(tr.j, trade["id"], symbol, w["target"], w["price"], w["qty"], cid)
    row = targets.rows(tr.j, "id=?", (rid,))[0]
    try:
        o = await tr.place_reduce_limit(symbol, w["qty"], w["price"], cid)
    except Exception as e:  # noqa: BLE001 - classified: refused is journalled and told, unknown found by its id
        kind = errors.classify(e)
        if not kind.unknown:
            targets.move(tr.j, row, "rejected", f"refused: {errors.describe(e)}")
            await tr.alert("target_rejected", f"target {w['target']:g} ({w['qty']:g} @ {w['price']:g}) refused: "
                           f"{errors.describe(e)}. Not retried at this size for an hour; the guardian's market "
                           "fallback covers it", symbol, False)
            return
        if kind is errors.ClockDrift:
            await tr.ex.resync_time()
        try:
            o = await clientid.lookup(tr.t, symbol, cid)
        except Exception:  # noqa: BLE001 - still unknown
            o = None
        if o is None:
            targets.move(tr.j, row, "unknown", f"send status unknown ({type(e).__name__}): resolved by client id")
            return
    await _landed(tr, row, o)


async def sync(tr, symbol: str, why: str = "sync") -> dict[str, Any]:
    async with tr.symbol_lock(symbol, f"targets: {why}"):
        pos = await tr.position(symbol)
        on_book = await listed(tr, symbol)
        await _settle(tr, symbol, on_book)
        trade = tr.j.active_trade(symbol)   # after the settle: targets_hit is current
        view = {"mark": float(pos["markPrice"]), "contracts": float(pos["contracts"])} if pos else None
        cancel, place = _plan(tr, symbol, trade, view, on_book, targets.live(tr.j, symbol))
        doubt = [o["id"] for o, r, w in cancel if not await _cancel(tr, symbol, o, r, w)]
        if doubt:   # placing more could exceed the position: Binance would cancel the lower-priority ones
            await tr.alert("target_cancel_failed", f"target order(s) {doubt}: cancel in doubt ({why}); nothing placed "
                           "this time, the next pass re-lists", symbol, not pos)
            return {"cancelled": len(cancel) - len(doubt), "placed": 0, "doubt": doubt}
        for w in place:
            await _place(tr, symbol, trade, w)
        return {"cancelled": len(cancel), "placed": len(place)}


async def settle(tr, symbol: str) -> None:
    """Journal the symbol's target fills and departures (no order sent)."""
    async with tr.symbol_lock(symbol, "targets: settle"):
        await _settle(tr, symbol, await listed(tr, symbol))


async def pull(tr, symbol: str, why: str) -> float:
    """Cancel every target on the symbol; returns what they filled that wasn't journalled before (re-read the
    position when > 0)."""
    async with tr.symbol_lock(symbol, f"targets: pull ({why})"):
        before = sum(float(r.get("filled") or 0) for r in targets.live(tr.j, symbol))
        ids = [r["id"] for r in targets.live(tr.j, symbol)]
        on_book = await listed(tr, symbol)
        by_cid = {r["client_id"]: r for r in targets.rows(tr.j, "symbol=?", (symbol,))}
        doubt = [o["id"] for c, o in on_book.items() if not await _cancel(tr, symbol, o, by_cid.get(c), why)]
        await _settle(tr, symbol, {c: o for c, o in on_book.items() if o["id"] in doubt})
        if doubt:
            await tr.alert("target_cancel_failed", f"target order(s) {doubt}: cancel in doubt ({why}); they may still "
                           "fill. The guardian re-lists every pass", symbol, True)
        after = sum(float(r.get("filled") or 0) for r in targets.rows(tr.j, f"id IN ({','.join('?' * len(ids))})",
                                                                       tuple(ids))) if ids else 0.0
        return round(max(0.0, after - before), 12)


async def pull_safe(tr, symbol: str, why: str) -> float:
    """pull, never raising: a failure is an urgent alert (the targets may still rest; the guardian re-lists)."""
    try:
        return await pull(tr, symbol, why)
    except Exception as e:  # noqa: BLE001 - the caller's change goes ahead; reduce-only orders can't flip it
        log.warning("targets on %s not pulled (%s): %s: %s", symbol, why, type(e).__name__, e)
        await tr.alert("target_cancel_failed", f"targets not cancelled ({why}): {type(e).__name__}: {e}. They may "
                       "still fill; the guardian re-lists them within a minute", symbol, True)
        return 0.0


async def clear_stale(tr, symbol: str, keep: int | None = None) -> int:
    """The re-entry guard: cancel targets on the symbol that aren't trade `keep`'s. Never raises (Binance expires
    them at flat too; the guardian sweeps every pass)."""
    try:
        async with tr.symbol_lock(symbol, "targets: re-entry guard"):
            by_cid = {r["client_id"]: r for r in targets.rows(tr.j, "symbol=?", (symbol,))}
            stale = [(o, by_cid.get(c)) for c, o in (await listed(tr, symbol)).items() if targets.trade_ref(c) != keep]
            for o, r in stale:
                await _cancel(tr, symbol, o, r, "re-entry guard: a new short on the symbol")
            return len(stale)
    except Exception as e:  # noqa: BLE001 - never raises (the guardian sweeps every pass); reported unless transient
        if isinstance(e, SymbolBusy) or errors.transient(e):
            log.warning("re-entry guard on %s: %s", symbol, errors.describe(e))
        else:
            await errors.report(tr.alert, "targets_sync.clear_stale", e, symbol)
        return 0


async def settle_flat(tr, live_positions: list[dict]) -> list[str]:
    """Pull the targets of symbols with no position left (guardian, before reconcile). Returns the symbols done."""
    held = {p["symbol"] for p in live_positions}
    done = []
    for sym in sorted({r["symbol"] for r in targets.live(tr.j)} - held):
        try:
            async with tr.symbol_lock(sym, "targets: flat sweep"):
                if await tr.position(sym):   # re-read under the lock: an empty answer alone proves nothing
                    continue
                await pull(tr, sym, "flat: the position is closed")
                done.append(sym)
        except SymbolBusy:   # a session is on it (its close pulls them too): next pass
            continue
    return done


async def due(tr, trade: dict, p: dict) -> bool:
    """Lock-free: anything to settle or change on this trade's symbol?"""
    sym = trade["symbol"]
    on_book, rows = await listed(tr, sym), targets.live(tr.j, sym)
    if any(r["client_id"] not in on_book or float(on_book[r["client_id"]].get("filled") or 0) > float(
            r.get("filled") or 0) for r in rows):
        return True
    cancel, place = _plan(tr, sym, trade, {"mark": p["mark"], "contracts": p["contracts"]}, on_book, rows)
    return bool(cancel or place)


async def after(tr, symbol: str, why: str) -> dict[str, Any]:
    """sync after a size, stop or plan change, best effort: a failure is alerted; the guardian's pass syncs again."""
    try:
        return await sync(tr, symbol, why)
    except Exception as e:  # noqa: BLE001 - targets never cost the change that called this
        log.warning("targets on %s after %s: %s: %s", symbol, why, type(e).__name__, e)
        try:
            await tr.alert("target_sync_error", f"targets not synced after {why} ({type(e).__name__}: {e}); the "
                           "guardian retries within a minute", symbol, False)
        except Exception:  # noqa: BLE001 - logged above; an alert outage must not cost the change either
            pass
        return {"error": f"{type(e).__name__}: {e}"}


async def at_entry(tr, symbol: str, why: str) -> dict[str, Any] | None:
    """A fill's targets right after its stop rests, under the caller's symbol lock (#71): they rest from the entry, not
    at the guardian's next pass (up to 60 s later). None when the open trade's plan doesn't rest targets (nothing
    listed). Never raises (after()): the guardian's pass retries a failure."""
    try:
        plan = (tr.j.active_trade(symbol) or {}).get("plan") or {}
    except Exception as e:  # noqa: BLE001 - the entry stands; the guardian's pass reads it again
        log.warning("targets on %s at %s: %s: %s", symbol, why, type(e).__name__, e)
        return {"error": f"{type(e).__name__}: {e}"}
    if not (plan.get("auto") or {}).get("targets"):
        return None
    return await after(tr, symbol, why)
