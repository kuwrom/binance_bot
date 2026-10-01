"""Stop swaps that can never leave a position naked (lesson: ONDO #5, 2026-09-28 07:10 UTC, where a -1021 clock-drift
error during the cancel step left the short without a stop for ~30 s until the guardian restored it).

Binance allows one closePosition stop per side (-4130), so a swap must cancel first and place second. Any failure
in either step ends with a restore: resync the clock if it drifted, then place the old stop again. If the old
stop is in fact still there, Binance answers -4130, which means we're still protected."""

from __future__ import annotations

from typing import Any

import ccxt.async_support as ccxt


def is_clock_drift(e: BaseException) -> bool:
    return isinstance(e, ccxt.InvalidNonce) or "-1021" in str(e)


async def swap_stop(tr, symbol: str, stops: list[dict], current: float | None, new_stop: float,
                    contracts: float) -> dict[str, Any]:
    try:
        for s in stops:
            await tr.t.cancel_order(s["id"], symbol, params={"trigger": True})
        return {"ok": True, "placed": await tr._place_stop(symbol, new_stop)}
    except ccxt.OrderImmediatelyFillable:
        await tr._market_close(symbol, contracts)
        await tr.alert("stop_hit_on_move", f"price crossed {new_stop} during the move; closed", symbol, True)
        return {"ok": True, "closed": True, "reason": "price crossed the new stop while moving it"}
    except Exception as e:  # noqa: BLE001 - whatever failed, end with a stop on the exchange
        if is_clock_drift(e):
            await tr.ex.resync_time()
        if current is None:
            return {"ok": False, "error": f"new stop rejected ({e}); there was no previous stop to restore"}
        try:
            await tr._place_stop(symbol, current, tries=3)
        except Exception as e2:  # noqa: BLE001
            if "-4130" in str(e2):   # old closePosition stop still in place: protected
                return {"ok": False, "error": f"stop move failed ({e}); old stop {current} is still in place"}
            await tr.alert("NAKED_POSITION", f"stop move failed ({e}) and restoring {current} failed ({e2}). "
                           "UNPROTECTED.", symbol, True)
            raise
        await tr.alert("stop_restored", f"stop move failed ({e}); restored {current}", symbol, False)
        return {"ok": False, "error": f"stop move failed ({e}); old stop {current} restored"}


STOP_TYPES = {"stop_market", "stop"}


def is_protective_stop(o: dict) -> bool:
    """Buy-side stop that closes/reduces the short. Take-profit orders don't count as protection."""
    info = o.get("info", {}) or {}
    # ccxt reports algo STOP_MARKET orders as type 'market' + triggerPrice; the real type is info.orderType
    otype = (info.get("orderType") or info.get("type") or o.get("type") or "").lower()
    closes = str(info.get("closePosition", "")).lower() == "true" or bool(o.get("reduceOnly"))
    return o.get("side") == "buy" and otype in STOP_TYPES and closes
