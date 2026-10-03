"""Stop changes that never leave a position without a stop (task #14; lesson: ONDO #5, 2026-09-28 07:10 UTC, where a
-1021 answered after the cancel had executed left the short without a stop for ~30 s).

Binance USDⓈ-M has no atomic stop modify (research 2026-10-01): Modify Order (PUT /fapi/v1/order) is LIMIT-only, an
untriggered conditional (algo) order can't be modified, and a closePosition stop takes neither a quantity nor
reduceOnly. A side holds one closePosition stop (-4130; the code has left the error-code page, testnet probes still
see it), so the new one can't rest before the old one goes. Make before break instead, inside the symbol lock:
1. a BRIDGE: a reduce-only STOP_MARKET for the position's size at the tighter of the two levels, confirmed resting
   (its ack, or its client id `sb-b…`). Tighter, so a widen's uncut size never rides the wider stop meanwhile
2. cancel this position's closePosition stop(s): only those. A resting entry's pre-armed stop (a split leg, another
   group) is that entry's protection, never moved here
3. place the new closePosition stop. Refused: the listing decides, not the error's code. An old stop still listed (a
   cancel that never ran) is cancelled again and the place retried once; none listed: restore the old level
4. cancel the bridge, only once a closePosition stop is known to rest
5. re-read the position: flat (a stop triggered meanwhile) leaves nothing resting; a LONG alarms (sign flip)
A swap interrupted by a cancel (guardian job timeout, Ctrl-C) settles first, then re-raises: it ends with a
closePosition stop, preferring the old level (the journal's), and bridges go only once one rests. A process killed
outright (SIGTERM has no handler; SIGKILL) leaves a stop by construction; its bridge is swept by the next stop change,
the guardian's restore or a full close."""

from __future__ import annotations

import asyncio
from typing import Any

import ccxt.async_support as ccxt

from . import clientid, errors

TRIG = {"trigger": True}
STOP_TYPES = {"stop_market", "stop"}
BRIDGE = f"{clientid.PREFIX}-{clientid.KINDS['bridge']}"   # client id prefix of a bridge stop
SETTLE_S = 20.0   # an interrupted swap gets this long to end with a closePosition stop


def is_protective_stop(o: dict) -> bool:
    """Buy-side stop that closes/reduces the short. Take-profit orders don't count as protection."""
    info = o.get("info", {}) or {}
    # ccxt reports algo STOP_MARKET orders as type 'market' + triggerPrice; the real type is info.orderType
    otype = (info.get("orderType") or info.get("type") or o.get("type") or "").lower()
    closes = str(info.get("closePosition", "")).lower() == "true" or bool(o.get("reduceOnly"))
    return o.get("side") == "buy" and otype in STOP_TYPES and closes


def is_close_position(o: dict) -> bool:
    return str((o.get("info") or {}).get("closePosition", "")).lower() == "true"


def is_bridge(o: dict) -> bool:
    return str(o.get("clientOrderId") or "").startswith(BRIDGE)


def trigger_of(o: dict) -> float:
    try:
        return float(o.get("triggerPrice") or o.get("stopPrice") or (o.get("info") or {}).get("triggerPrice"))
    except (TypeError, ValueError):
        return 0.0


async def listed(tr, symbol: str) -> tuple[list[dict], list[dict]]:
    """(closePosition stops, bridges) resting on the symbol now."""
    orders = await tr.t.fetch_open_orders(symbol, params=TRIG)
    return [o for o in orders if is_close_position(o)], [o for o in orders if is_bridge(o)]


async def _cancel(tr, symbol: str, ids) -> set[str]:
    """Cancel these conditional orders; returns the ids whose cancel is in doubt (a refusal, -1021, a timeout...).
    -2011 (OrderNotFound) means gone: triggered or cancelled already."""
    doubt = set()
    for i in ids:
        try:
            await tr.cancel(symbol, i, trigger=True)
        except Exception as e:  # noqa: BLE001 - Gone is done; anything else: the listing decides what still rests
            kind = errors.classify(e)
            if kind is errors.Gone:   # -2011: triggered or cancelled already
                continue
            doubt.add(str(i))
            if kind is errors.ClockDrift:
                await tr.ex.resync_time()
    return doubt


async def _bridge(tr, symbol: str, stop: float, contracts: float) -> dict:
    """Step 1: a reduce-only STOP_MARKET for `contracts`, confirmed resting (its ack, or found by its client id after an
    ambiguous answer). Raises a refusal as is, or clientid.Unknown (a late landing is swept by the next change)."""
    from .trading import _sleep
    cid = clientid.new("bridge", clientid.tag(symbol))
    try:
        o = await tr.place_reduce_stop(symbol, float(tr.t.amount_to_precision(symbol, contracts)), stop, cid)
        if o.get("status") in (None, "open"):
            return o
        raise ccxt.InvalidOrder(f"bridge stop {cid} answered {o.get('status')}")
    except Exception as e:  # noqa: BLE001 - classified: a definite refusal raises as is, else found by its id
        kind = errors.classify(e)
        if not kind.unknown:
            raise
        if kind is errors.ClockDrift:
            await tr.ex.resync_time()
        await _sleep(1)
        if found := await tr.find_stop(symbol, cid):
            return found
        raise clientid.Unknown(cid, e) from e


async def set_stop(tr, symbol: str, new_stop: float, contracts: float, old: list[dict] | None = None) -> dict[str, Any]:
    """Put this position's stop at new_stop. Run it inside tr.symbol_lock(symbol). `old`: the position's closePosition
    stops ({id, trigger}: Trader.stop_orders(close_position_only=True)); with none the stop is only placed (the
    guardian's restore, a first stop). Returns {"ok": True, "placed"}, {"ok": True, "closed": True} (price reached the
    stop meanwhile), or {"ok": False, "error"}; "warnings" carries what a human was told."""
    old = list(old or [])
    ids = {str(s["id"]) for s in old}
    cur = min((s["trigger"] for s in old), default=None)
    try:
        res = await (_swap(tr, symbol, ids, cur, new_stop, contracts) if old else _first(tr, symbol, new_stop))
    except asyncio.CancelledError:   # a job timeout or shutdown mid-swap: end with a stop first
        try:
            async with asyncio.timeout(SETTLE_S):
                await _settle(tr, symbol, [p for p in (cur, new_stop) if p], ids, placed=False, contracts=contracts)
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 - the bridge-first order already left a stop
            pass
        raise
    return res if res.get("closed") else await _after(tr, symbol, res, ids)


async def _first(tr, symbol: str, stop: float) -> dict[str, Any]:
    """No closePosition stop rests: place one, then sweep leftover bridges. A refusal is decided by the listing: a
    closePosition stop listed rests already (-4130, with or without its code)."""
    try:
        placed = await tr.place_close_stop(symbol, stop, tries=3)
    except Exception as e:  # noqa: BLE001 - classified; anything not settled here is re-raised
        if errors.classify(e) is errors.WouldTrigger:
            return await _crossed(tr, symbol, stop, set())
        if errors.classify(e) is errors.ReduceOnlyRejected and await tr.flat_now(symbol):   # -4509: flat first
            await strip_flat(tr, symbol)
            return {"ok": True, "closed": True, "flat": True, "reason": "the position closed before its stop landed"}
        close, _ = await listed(tr, symbol)
        if not close:
            raise
        return {"ok": False, "present": close[0], "error": f"a closePosition stop already rests at "
                                                          f"{trigger_of(close[0]):g} ({e}): re-read the position"}
    await _settle(tr, symbol, [], set(), placed=True)
    return {"ok": True, "placed": placed}


async def _swap(tr, symbol: str, ids: set[str], cur: float, new_stop: float, contracts: float) -> dict[str, Any]:
    _, leftover = await listed(tr, symbol)   # a dead swap's bridges: a closePosition stop rests, so they can go
    await _cancel(tr, symbol, [o["id"] for o in leftover])
    try:
        bridge = await _bridge(tr, symbol, min(cur, new_stop), contracts)
    except Exception as e:  # noqa: BLE001 - nothing was cancelled: the old stop stays
        if errors.classify(e) is errors.WouldTrigger:
            return await _crossed(tr, symbol, min(cur, new_stop), ids)
        return {"ok": False, "error": f"stop not moved: its bridge stop was not placed ({e}); {cur} stays"}
    placed, err, doubt = None, None, set()
    for _ in range(2):   # a second round only while an old stop is still listed (its cancel never ran)
        doubt = await _cancel(tr, symbol, ids)
        try:
            placed = await tr.place_close_stop(symbol, new_stop, tries=3)
            break
        except Exception as e:  # noqa: BLE001 - the listing decides, not the code (-4130 is undocumented now)
            err = e
            if errors.classify(e) is errors.WouldTrigger:
                return await _crossed(tr, symbol, new_stop, ids)
            try:
                close, _ = await listed(tr, symbol)
            except Exception:  # noqa: BLE001 - unlisted: no second round; _settle restores and alerts what rests
                break
            if not any(str(o["id"]) in ids for o in close):
                break
    if placed is not None:
        await _settle(tr, symbol, [], doubt, placed=True, bridge=bridge)
        return {"ok": True, "placed": placed}
    kept = await _settle(tr, symbol, [cur], ids, placed=False, bridge=bridge, contracts=contracts)
    if kept == "old":
        return {"ok": False, "error": f"stop move failed ({err}); old stop {cur} is still in place"}
    if kept == "restored":
        await tr.alert("stop_restored", f"stop move failed ({err}); restored {cur}", symbol, False)
        return {"ok": False, "error": f"stop move failed ({err}); old stop {cur} restored"}
    return {"ok": False, "error": f"stop move failed ({err}) and restoring {cur} failed: {kept}", "unprotected": True}


async def _settle(tr, symbol: str, levels: list[float], stale: set[str], placed: bool, bridge: dict | None = None,
                  contracts: float = 0.0) -> str:
    """End state: a closePosition stop rests (`placed`, else one listed, else placed at the first of `levels` that
    works), one of them only (two, where Binance no longer refuses a second: the new one stays when it was placed, else
    the old `stale` one), and bridges go only once a closePosition stop rests.
    Returns "new" | "old" (an old one is still listed) | "restored" | why nothing rests (the bridge is kept)."""
    if placed and not stale and bridge is not None:   # the common path: nothing to look up
        await drop_bridges(tr, symbol, [bridge])
        return "new"
    close, bridges = await listed(tr, symbol)
    if bridge is not None and not any(str(b["id"]) == str(bridge["id"]) for b in bridges):
        bridges.append(bridge)   # the listing may lag the ack
    got = "new" if placed else "old" if close else ""
    why = "no level to restore"
    for px in levels if not (close or placed) else []:
        try:
            close, got = [await tr.place_close_stop(symbol, px, tries=2)], "restored"
            break
        except Exception as e:  # noqa: BLE001 - the next level is tried; none left: _naked alerts with this why
            why = f"{e}"
    if not (close or placed):
        return await _naked(tr, symbol, bridges, why, contracts)
    if {str(o["id"]) in stale for o in close} == {True, False}:
        await _cancel(tr, symbol, [o["id"] for o in close if (str(o["id"]) in stale) == placed])
    await drop_bridges(tr, symbol, bridges)
    return got


async def drop_bridges(tr, symbol: str, bridges: list[dict]) -> None:
    """Cancel bridges once a closePosition stop rests. One left behind would buy back its size at its own level: before
    a widened stop, or on the next short once flat. A cancel in doubt is checked against the listing and sent again;
    one still resting after that is a human's to see (the guardian's pass sweeps it too)."""
    doubt = await _cancel(tr, symbol, [b["id"] for b in bridges])
    if not doubt:
        return
    try:
        doubt = await _cancel(tr, symbol, [b["id"] for b in (await listed(tr, symbol))[1] if str(b["id"]) in doubt])
    except Exception:  # noqa: BLE001 - reported below
        pass
    if doubt:
        await tr.alert("bridge_left", f"bridge stop(s) {sorted(doubt)} could not be cancelled: they buy back their size "
                       "at their own level, before the closePosition stop. The guardian sweeps them; check Binance.",
                       symbol, True)


async def _naked(tr, symbol: str, bridges: list[dict], why: str, contracts: float) -> str:
    if bridges:
        await tr.alert("NAKED_POSITION", f"no closePosition stop ({why}): only the bridge stop at "
                       f"{min(trigger_of(b) for b in bridges):g} for {contracts:g} protects it (reduce-only: an add is "
                       "not covered). The guardian restores the stop within a minute; check Binance.", symbol, True)
        return f"only the bridge stop protects it ({why})"
    if not await tr.position(symbol):
        return "flat"   # a stop triggered meanwhile: nothing left to protect
    await tr.alert("NAKED_POSITION", f"no closePosition stop and no bridge ({why}). UNPROTECTED: set a stop or close "
                   "it NOW.", symbol, True)
    raise RuntimeError(f"no stop rests on {symbol}: {why}")


async def _crossed(tr, symbol: str, stop: float, stale: set[str]) -> dict[str, Any]:
    """Price is already at or past the stop being set: close now, as the stop would have."""
    pos = await tr.position(symbol)
    if pos:
        await tr.close_market(symbol, float(pos["contracts"]))
    await strip_flat(tr, symbol)
    await tr.alert("stop_hit_on_move", f"price crossed {stop} during the move; closed", symbol, True)
    return {"ok": True, "closed": True, "reason": "price crossed the new stop while moving it"}


async def strip_flat(tr, symbol: str) -> None:
    """Flat: a closePosition stop or bridge left now would block (-4130) or cut the next short on the symbol. Resting
    entries' pre-arms stay: their limits may still fill. One that can't be listed or cancelled is told (the orphan
    sweep, orphans.py, cancels a bridge on a flat symbol; replace_stale swaps a stale closePosition stop)."""
    try:
        close, bridges = await listed(tr, symbol)
        doubt = await _cancel(tr, symbol, [o["id"] for o in close + bridges])
    except Exception as e:  # noqa: BLE001 - the change itself is done; told below
        doubt = {f"unlisted ({errors.describe(e)})"}
    if doubt:
        await tr.alert("stop_left", f"flat, but stop(s) {sorted(doubt)} may still rest: a bridge would cut the next "
                       "short on the symbol. The orphan sweep cancels bridges within minutes; check Binance.", symbol,
                       False)


async def _after(tr, symbol: str, res: dict[str, Any], stale: set[str]) -> dict[str, Any]:
    """Re-read the position after a stop change: flat means a stop triggered meanwhile; LONG is a sign flip."""
    try:
        pos = await tr.position(symbol)
    except Exception as e:  # noqa: BLE001 - the change itself is done
        return {**res, "warnings": [*res.get("warnings", []), f"position not re-read after the stop change ({e})"]}
    if pos is None:
        await strip_flat(tr, symbol)
        return {"ok": True, "closed": True, "reason": "the position closed during the stop change (a stop triggered)"}
    if (pos.get("side") or "short") != "short":
        msg = (f"position is {pos.get('side')} {pos.get('contracts')} after a stop change: a short-only desk. Close it "
               "now; check what flipped it")
        await tr.alert("position_flip", msg, symbol, True)
        return {**res, "warnings": [*res.get("warnings", []), msg]}
    return res


async def replace_stale(tr, symbol: str, stop: float, contracts: float, e: Exception) -> dict:
    """An entry's first stop was refused. The symbol was flat when the entry was sent (checked under the lock), so a
    closePosition stop listed now is stale, left by an earlier position, and a second is refused (-4130; the listing
    decides, not the code). It is swapped, make before break, to this entry's stop: one retry. Re-raises `e` when the
    refusal wasn't that, or the swap failed (the caller closes the position)."""
    kind = errors.classify(e)
    if kind.unknown or kind is errors.WouldTrigger:
        raise e
    close, _ = await listed(tr, symbol)
    if not close:
        raise e
    px = float(tr.t.price_to_precision(symbol, stop))
    if same := next((o for o in close if trigger_of(o) == px), None):
        return same   # it protects at exactly this level
    res = await set_stop(tr, symbol, px, contracts, [{"id": o["id"], "trigger": trigger_of(o)} for o in close])
    if not (res.get("ok") and res.get("placed")):
        raise RuntimeError(f"{e}; replacing the stale stop failed: {res.get('error') or res.get('reason')}") from e
    await tr.alert("stale_stop_replaced", f"a stale closePosition stop at {[trigger_of(o) for o in close]} refused "
                   f"this entry's stop ({e}): replaced by {px:g}", symbol, False)
    return res["placed"]
