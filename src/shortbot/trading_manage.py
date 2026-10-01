"""Managing an open short (mixed into Trader): close (full or partial, then finalize from exchange income),
tighten-only stop moves, and the guardian's missing-stop safety net.

trading.py imports this module, so `_f` and `_sleep` are imported inside each method at call time: a module-level
import would be circular, and would bind `_sleep` before the tests patch `shortbot.trading._sleep`.
"""

from __future__ import annotations

from . import reconcile
from .exchange import to_symbol
from .stops import swap_stop


class ManageFlow:
    """Uses Trader's exchange, journal, lock, position/stop queries and order primitives."""

    async def close(self, symbol: str, reason: str, lesson: str = "", fraction: float = 1.0) -> dict:
        from .trading import _f, _sleep
        symbol = to_symbol(symbol)
        if not 0 < fraction <= 1:
            return {"ok": False, "error": "fraction must be in (0, 1]"}
        async with self.lock:
            await self.ex.trade_markets()
            pos = await self.position(symbol)
            if not pos:
                return {"ok": False, "error": f"no open position in {symbol}"}
            size = _f(pos["contracts"])
            amount = size if fraction == 1 else float(self.t.amount_to_precision(symbol, size * fraction))
            if amount <= 0:
                return {"ok": False, "error": "fraction too small for the exchange step size"}
            order = await self._market_close(symbol, amount)
            exit_px = _f(order.get("average"), _f(pos.get("markPrice")))
            trade = self.j.active_trade(symbol)
            if fraction < 1 or amount < size:
                self.j.log_decision("partial_close", reason, symbol, snapshot={
                    "trade_id": trade and trade["id"], "exit": exit_px, "amount": amount, "lesson": lesson})
                return {"ok": True, "symbol": symbol, "closed_amount": amount, "exit": exit_px,
                        "remaining": round(size - amount, 8)}
            from .tranches import cancel_legs
            await cancel_legs(self, symbol, "trade closed")
            try:
                await self.t.cancel_all_orders(symbol, params={"trigger": True})
            except Exception as e:  # noqa: BLE001 - position is flat; an orphan stop is harmless but noted
                await self.alert("orphan_stop", f"closed but could not cancel stops: {e}", symbol, False)
            await _sleep(1)  # let income records land
            res = await reconcile.finalize(self, trade, reason, lesson) if trade else {"note": "not in journal"}
            return {"ok": True, "symbol": symbol, "closed_amount": amount, "exit": exit_px, "result": res}

    async def move_stop(self, symbol: str, new_stop: float, reason: str) -> dict:
        """Tighten-only. Swap = cancel old, place new; on failure the old stop is restored."""
        from .trading import _f
        symbol = to_symbol(symbol)
        async with self.lock:
            await self.ex.trade_markets()
            pos = await self.position(symbol)
            if not pos:
                return {"ok": False, "error": f"no open position in {symbol}"}
            stops = await self.stop_orders(symbol)
            mark = _f(pos.get("markPrice"))
            current = min((s["trigger"] for s in stops), default=None)
            new_stop = float(self.t.price_to_precision(symbol, new_stop))   # compare what the exchange would get
            if current is not None and new_stop >= current:   # also a same-tick "move": never swap for nothing
                return {"ok": False, "noop": new_stop == current,
                        "error": f"stops only tighten: new {new_stop} >= current {current}"}
            if new_stop <= mark:
                return {"ok": False, "error": f"new stop {new_stop} must be above mark {mark} (else close instead)"}
            if current is None:
                dist = (new_stop - _f(pos["entryPrice"])) / _f(pos["entryPrice"]) * 100
                if dist > self.ex.limits.max_stop_distance_pct:
                    return {"ok": False, "error": f"no stop yet and {dist:.1f}% from entry exceeds max stop distance"}
            res = await swap_stop(self, symbol, stops, current, new_stop, _f(pos["contracts"]))
            if not res["ok"] or res.get("closed"):
                return res
            placed = res["placed"]
            trade = self.j.active_trade(symbol)
            if trade:
                self.j.update_trade(trade["id"], stop_price=new_stop)
            self.j.log_decision("move_stop", reason, symbol, snapshot={"from": current, "to": new_stop})
            return {"ok": True, "symbol": symbol, "old_stop": current, "new_stop": new_stop,
                    "order_id": placed.get("id")}

    async def ensure_stop(self, symbol: str, fallback_pct: float) -> dict | None:
        """Guardian safety net: restore a missing stop (journal stop, else entry+fallback%), or close the
        position if price is already beyond the intended stop."""
        from .trading import _f, _sleep
        async with self.lock:
            if await self.stop_orders(symbol, close_position_only=True):
                return None
            pos = await self.position(symbol)
            if not pos:
                return None
            jt = self.j.active_trade(symbol) or {}
            if jt.get("status") == "opening":
                return {"symbol": symbol, "action": "skipped: entry in progress"}
            mark = _f(pos.get("markPrice"))
            stop = _f(jt.get("stop_price")) or _f(pos.get("entryPrice")) * (1 + fallback_pct / 100)
            if stop <= mark:
                await self._market_close(symbol, _f(pos["contracts"]))
                if jt:
                    await _sleep(1)
                    await reconcile.finalize(self, jt, f"guardian: unprotected and past stop {stop}")
                return {"symbol": symbol, "action": "closed", "mark": mark, "stop": stop}
            await self._place_stop(symbol, stop, tries=3)
            self.j.log_alert("stop_restored", f"placed missing stop at {stop}", symbol)
            return {"symbol": symbol, "action": "stop_placed", "stop": stop}
