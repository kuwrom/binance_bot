"""Managing an open short (mixed into Trader): close (full or partial, then finalize from exchange income),
tighten-only stop moves, and the guardian's missing-stop safety net.

A full close touches only this trade's own orders (#56): its resting split legs (withdrawn: a leg's pre-armed stop
goes only if its limit died unfilled), then, once the position is re-read FLAT, the closePosition stop and the
pre-armed stops of its own entries. Another group's resting entry keeps its pre-armed stop. A remainder (the close
filled short, or an order filled mid-close) keeps every stop and gets a closePosition stop at once if it has none.

trading.py imports this module, so `_f` and `_sleep` are imported inside each method at call time: a module-level
import would be circular, and would bind `_sleep` before the tests patch `shortbot.trading._sleep`.
"""

from __future__ import annotations

import ccxt.async_support as ccxt

from . import reconcile, tranches
from .exchange import to_symbol
from .stops import is_protective_stop, swap_stop

TRIG = {"trigger": True}


def _whole(o: dict) -> bool:
    """A closePosition order: on a flat symbol it protects nothing (one per symbol and side, -4130)."""
    return str((o.get("info") or {}).get("closePosition", "")).lower() == "true"


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
            amount = min(amount, size)   # a fraction that rounds to the whole size is a full close
            order = await self._market_close(symbol, amount)
            exit_px = _f(order.get("average"), _f(pos.get("markPrice")))
            trade = self.j.active_trade(symbol)
            if amount < size:
                self.j.log_decision("partial_close", reason, symbol, snapshot={
                    "trade_id": trade and trade["id"], "exit": exit_px, "amount": amount, "lesson": lesson})
                return {"ok": True, "symbol": symbol, "closed_amount": amount, "exit": exit_px,
                        "remaining": round(size - amount, 8)}
            legs = await tranches.cancel_legs(self, trade["id"], "trade closed") if trade else {}
            await _sleep(1)  # let the position settle and income records land
            try:
                listed = await self.t.fetch_open_orders(symbol, params=TRIG)
            except Exception as e:  # noqa: BLE001 - if flat, an orphan stop is noted, never guessed at
                listed, err = None, e
            # read AFTER the listing: a stop placed for a fill that lands from here on is never in it
            try:
                left = await self.position(symbol)
            except Exception as e:  # noqa: BLE001 - flat or not is unknown: cancel nothing
                return await self._close_left(symbol, trade, order, amount, None, exit_px, reason, lesson, e)
            if left:
                return await self._close_left(symbol, trade, order, amount, left, exit_px, reason, lesson)
            if listed is None:
                await self.alert("orphan_stop", f"closed but could not list stops: {err}", symbol, False)
            else:   # the limits of "unknown" legs may still rest and fill: their pre-armed stops stay
                own = tranches.own_entries(self.j, trade["id"]) if trade else []
                await self._strip_stops(symbol, listed, own + legs.get("unfilled", []) + legs.get("filled", []))
            res = await reconcile.finalize(self, trade, reason, lesson) if trade else {"note": "not in journal"}
            return {"ok": True, "symbol": symbol, "closed_amount": amount, "exit": exit_px, "result": res}

    async def _strip_stops(self, symbol: str, listed: list[dict], own: list[dict]) -> None:
        """Flat: cancel the closePosition stop and the pre-armed stops of this trade's own entries, matched by id or,
        for a pre-arm whose send answer was lost (prearm_id unset), by its client id."""
        ids = {str(e["prearm_id"]) for e in own if e.get("prearm_id")}
        cids = {e["prearm_cid"] for e in own if e.get("prearm_cid")}
        for o in listed:
            if not (_whole(o) or str(o.get("id")) in ids or o.get("clientOrderId") in cids):
                continue   # another group's pre-armed stop: its limit may still fill
            try:
                await self.t.cancel_order(o["id"], symbol, params=TRIG)
            except ccxt.OrderNotFound:
                pass   # gone meanwhile (a closePosition stop leaves with its position)
            except Exception as e:  # noqa: BLE001 - flat: an orphan stop is harmless now, but noted
                await self.alert("orphan_stop", f"closed but could not cancel stop {o['id']}: {e}", symbol, False)

    async def _close_left(self, symbol: str, trade: dict | None, order: dict, amount: float, pos: dict | None,
                          exit_px: float, reason: str, lesson: str, unread: Exception | None = None) -> dict:
        """Not flat after a full close (or the re-read failed): our order filled short (Binance FAQ "Summary of
        Failed Orders": a market order can expire part-filled), or an order filled mid-close. Nothing is cancelled.
        If our whole size was bought back the trade is finalized and the fill poll adopts the new fill; else the
        trade stays open."""
        from .trading import _f
        filled = _f(order.get("filled"))   # no `filled`: assume nothing filled
        qty = None if pos is None else _f(pos["contracts"])
        short = filled < amount
        why = f"the close filled {filled:g} of {amount:g}" if short else "an order filled during the close"
        if pos is None:
            head, why, cover = "could not re-read the position after a full close", f"{unread}", "every stop kept"
        else:
            head, cover = f"{qty:g} still open after a full close", await self._cover(symbol, trade, qty)
        stays = short or not trade
        nxt = ("not in the journal: close it again" if not trade else "trade stays open: close it again" if short
               else "trade closed, the fill poll adopts the rest")
        msg = f"{head} ({why}): {cover}; no stop cancelled; {nxt}"
        await self.alert("close_incomplete", msg, symbol, True)
        self.j.log_decision("close_incomplete", reason, symbol, snapshot={
            "trade_id": trade and trade["id"], "amount": amount, "filled": filled, "remaining": qty,
            "exit": exit_px, "stop": cover, "lesson": lesson})
        out = {"symbol": symbol, "closed_amount": filled if short else amount, "exit": exit_px, "remaining": qty}
        if stays:
            return {"ok": False, "error": msg, **out}
        return {"ok": True, **out, "warning": msg, "result": await reconcile.finalize(self, trade, reason, lesson)}

    async def _cover(self, symbol: str, trade: dict | None, qty: float) -> str:
        """A remainder never waits for the guardian: unless a closePosition stop, or pre-armed stops as big as the
        remainder (the order that filled mid-close brings its own), still protect it, it gets a closePosition stop at
        the journal stop now. If even that fails, a human is told now."""
        from .trading import _f
        try:
            held = [o for o in await self.t.fetch_open_orders(symbol, params=TRIG) if is_protective_stop(o)]
        except Exception:  # noqa: BLE001 - can't see: place one; -4130 means a closePosition stop is there
            held = []
        trig = [_f(o.get("triggerPrice") or o.get("stopPrice") or (o.get("info") or {}).get("triggerPrice"))
                for o in held]
        if any(_whole(o) for o in held):
            return f"closePosition stop kept at {min(p for o, p in zip(held, trig) if _whole(o)):g}"
        if held and sum(_f(o.get("amount")) for o in held) >= qty:
            return f"pre-armed stop(s) at {sorted(trig)} cover it; the fill poll adds the closePosition stop"
        stop = _f((trade or {}).get("stop_price"))
        try:
            if not stop:
                raise ValueError("no journal stop for this position")
            await self._place_stop(symbol, stop, tries=3)
            return f"closePosition stop placed at {stop:g}"
        except Exception as e:  # noqa: BLE001
            if "-4130" in str(e):
                return "closePosition stop already in place"
            await self.alert("NAKED_POSITION", f"{qty:g} left after a full close and placing a closePosition stop at "
                             f"{stop or 'n/a'} failed ({e}). Pre-armed stops: {sorted(trig) or 'none'}. Set a stop "
                             "or close it NOW.", symbol, True)
            return f"NO closePosition stop ({e})"

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
