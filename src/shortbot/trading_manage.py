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

from decimal import ROUND_FLOOR, Decimal

import ccxt.async_support as ccxt

from . import clientid, close_intent, fill_quality, margin, reconcile, stops, targets_sync, tranches
from .audit import refusals
from .exchange import to_symbol
from .stops import is_bridge, is_protective_stop

TRIG = {"trigger": True}


def _whole(o: dict) -> bool:
    """A closePosition order: on a flat symbol it protects nothing (one per symbol and side, -4130)."""
    return str((o.get("info") or {}).get("closePosition", "")).lower() == "true"


class ManageFlow:
    """Uses Trader's exchange, journal, symbol_lock, position/stop queries and order primitives."""

    @refusals("close")
    async def close(self, symbol: str, reason: str, lesson: str = "", fraction: float = 1.0,
                    expect_contracts: float | None = None, of_original: bool = False) -> dict:
        """fraction: of the CURRENT size, or with of_original the cumulative share of the original size. A partial
        close is checked against the caller's view inside the symbol lock (close_intent.py)."""
        from .trading import _f, _sleep
        symbol = to_symbol(symbol)
        if not 0 < fraction <= 1:
            return {"ok": False, "error": "fraction must be in (0, 1]"}
        async with self.symbol_lock(symbol, f"close {fraction:g}{' of original' if of_original else ''}: {reason}"):
            await self.ex.trade_markets(symbol)
            pos = await self.position(symbol)
            if not pos:
                return {"ok": False, "error": f"no open position in {symbol}"}
            size, trade = _f(pos["contracts"]), self.j.active_trade(symbol)
            amount = close_intent.sized(self, symbol, size, trade, fraction, expect_contracts, of_original)
            if isinstance(amount, dict):
                return amount   # refused: nothing sent
            # a resting target filling with a partial close would close more than asked: they go first (#65)
            if amount < size and await targets_sync.pull_safe(self, symbol, f"partial close: {reason}"):
                pos = await self.position(symbol)   # one filled meanwhile: decide on what is left
                if not pos:
                    return {"ok": False, "error": f"no open position in {symbol}: its targets filled"}
                size = _f(pos["contracts"])
                amount = close_intent.sized(self, symbol, size, trade, fraction, expect_contracts, of_original)
                if isinstance(amount, dict):
                    await targets_sync.after(self, symbol, "a refused partial close")
                    return amount
            if amount < size:
                res = await self._close_part(symbol, trade, pos, size, amount, reason, lesson)
                await targets_sync.after(self, symbol, "partial close")
                return res
            order = await self._market_close(symbol, amount)
            fill_quality.record(self, trade and trade["id"], "exit", "market", order)   # mainnet ref, background (#35)
            exit_px = _f(order.get("average"), _f(pos.get("markPrice")))
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
            await targets_sync.pull_safe(self, symbol, "flat after a full close")   # whatever Binance does (#65)
            if listed is None:
                await self.alert("orphan_stop", f"closed but could not list stops: {err}", symbol, False)
            else:   # the limits of "unknown" legs may still rest and fill: their pre-armed stops stay
                own = tranches.own_entries(self.j, trade["id"]) if trade else []
                await self._strip_stops(symbol, listed, own + legs.get("unfilled", []) + legs.get("filled", []))
            res = await reconcile.finalize(self, trade, reason, lesson) if trade else {"note": "not in journal"}
            return {"ok": True, "symbol": symbol, "closed_amount": amount, "exit": exit_px, "result": res}

    async def _close_part(self, symbol: str, trade: dict | None, pos: dict, size: float, amount: float, reason: str,
                          lesson: str) -> dict:
        """A partial close: never re-sent while its outcome is unknown (clientid.Unknown); reports what FILLED, not
        what was asked (a market order can expire part-filled). The trade's stops stay as they are."""
        from .trading import _f
        try:
            order = await self._market_close(symbol, amount, whole=False)
        except clientid.Unknown as e:
            msg = f"partial close of {amount:g}: {e}. Not re-sent: re-read the position before closing again"
            await self.alert("close_unknown", msg, symbol, True)
            self.j.log_decision("close_unknown", reason, symbol, snapshot={
                "trade_id": trade and trade["id"], "amount": amount, "client_id": e.cid, "lesson": lesson})
            return {"ok": False, "unknown": True, "symbol": symbol, "client_id": e.cid, "error": msg}
        fill_quality.record(self, trade and trade["id"], "exit", "market", order)
        exit_px = _f(order.get("average"), _f(pos.get("markPrice")))
        if order.get("filled") is None:   # flat meanwhile, or an answer without it: the position says what is left
            try:
                left = _f(((await self.position(symbol)) or {}).get("contracts"))
            except Exception:  # noqa: BLE001 - reported as unknown
                left = None
            closed = None if left is None else round(size - left, 8)
        else:
            closed = round(_f(order["filled"]), 8)
            left = round(size - closed, 8)
        self.j.log_decision("partial_close", reason, symbol, snapshot={
            "trade_id": trade and trade["id"], "exit": exit_px, "amount": closed, "asked": amount, "lesson": lesson})
        out = {"ok": bool(closed), "symbol": symbol, "closed_amount": closed, "exit": exit_px,
               **({"remaining": left} if left is not None else {})}
        if closed is None or closed < amount:
            out["warning"] = f"the close filled {'an unknown amount' if closed is None else f'{closed:g}'} of {amount:g}"
        return out

    async def _strip_stops(self, symbol: str, listed: list[dict], own: list[dict]) -> None:
        """Flat: cancel the closePosition stop, any stop-swap bridge (stops.py) and the pre-armed stops of this trade's own
        entries, matched by id or, for a pre-arm whose send answer was lost (prearm_id unset), by its client id."""
        ids = {str(e["prearm_id"]) for e in own if e.get("prearm_id")}
        cids = {e["prearm_cid"] for e in own if e.get("prearm_cid")}
        for o in listed:
            if not (_whole(o) or is_bridge(o) or str(o.get("id")) in ids or o.get("clientOrderId") in cids):
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

    @refusals("move_stop")
    async def move_stop(self, symbol: str, new_stop: float, reason: str) -> dict:
        """Tighten-only, make before break (stops.set_stop). Only the position's closePosition stop moves: a resting
        entry's pre-armed stop is that entry's protection."""
        from .trading import _f
        symbol = to_symbol(symbol)
        async with self.symbol_lock(symbol, f"move stop to {new_stop:g}: {reason}"):
            await self.ex.trade_markets(symbol)
            pos = await self.position(symbol)
            if not pos:
                return {"ok": False, "error": f"no open position in {symbol}"}
            old = await self.stop_orders(symbol, close_position_only=True)
            mark = _f(pos.get("markPrice"))
            current = min((s["trigger"] for s in old), default=None)
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
            res = await stops.set_stop(self, symbol, new_stop, _f(pos["contracts"]), old)
            if not res["ok"] or res.get("closed"):
                return res
            placed = res["placed"]
            trade = self.j.active_trade(symbol)
            if trade:
                self.j.update_trade(trade["id"], stop_price=new_stop)
            self.j.log_decision("move_stop", reason, symbol, snapshot={"from": current, "to": new_stop})
            await targets_sync.after(self, symbol, "stop move")   # #65: in case its bridge took a target with it
            return {"ok": True, "symbol": symbol, "old_stop": current, "new_stop": new_stop,
                    "order_id": placed.get("id"), **({"warnings": res["warnings"]} if res.get("warnings") else {})}

    async def ensure_stop(self, symbol: str, fallback_pct: float) -> dict | None:
        """Guardian safety net: restore a missing closePosition stop (journal stop, else entry+fallback%), or close the
        position if price is already beyond the intended stop. Either sits inside the position's real liquidation price
        minus liq_buffer_pct (a stop past it never fires: at 10x a short is liquidated ~9% up); a capped plan stop is an
        urgent alert. Pre-armed stops and bridges don't count (they cover a fixed size); a bridge left by a dead swap
        goes once a closePosition stop rests (stops.set_stop)."""
        from .trading import _f
        async with self.symbol_lock(symbol, "guardian: restore a missing stop"):
            await self.ex.trade_markets(symbol)   # a perp listed since start is known before anything is sent on it
            close, bridges = await stops.listed(self, symbol)
            if close:
                if not bridges:
                    return None
                await stops.drop_bridges(self, symbol, bridges)
                return {"symbol": symbol, "action": "bridges_swept", "bridges": [b["id"] for b in bridges]}
            pos = await self.position(symbol)
            if not pos:
                return None
            # an 'opening' row is protected too: under this lock no entry of this host is in flight (open_short holds
            # it until its stop rests), so the row is a crashed entry or one of unknown outcome that filled late (#9)
            jt = self.j.active_trade(symbol) or {}
            mark, liq, plan = _f(pos.get("markPrice")), _f(pos.get("liquidationPrice")), _f(jt.get("stop_price"))
            stop = plan or _f(pos.get("entryPrice")) * (1 + fallback_pct / 100)
            cap = self._liq_cap(symbol, liq)
            if cap is not None and stop > cap:
                if plan:
                    await self.alert("plan_stop_unreachable", f"plan stop {plan:g} is past liquidation {liq:g} minus "
                                     f"{self.ex.limits.liq_buffer_pct:g}%: it would never fire. Restored at {cap:g} "
                                     "instead; fix the plan's stop", symbol, True)
                stop = cap
            stop = float(self.t.price_to_precision(symbol, stop))
            if stop <= mark:
                await self._close_past(symbol, pos, jt, f"guardian: unprotected and past stop {stop}")
                return {"symbol": symbol, "action": "closed", "mark": mark, "stop": stop}
            res = await stops.set_stop(self, symbol, stop, _f(pos["contracts"]))
            if res.get("closed"):
                return {"symbol": symbol, "action": "closed", "mark": mark, "stop": stop, "why": res.get("reason")}
            if not res.get("ok"):
                if res.get("present"):
                    return None   # one rests after all (placed meanwhile, or the listing lagged)
                raise RuntimeError(res.get("error") or "stop not placed")
            self.j.log_alert("stop_restored", f"placed missing stop at {stop}", symbol)
            return {"symbol": symbol, "action": "stop_placed", "stop": stop, **({"liquidation": liq} if liq else {})}

    def _liq_cap(self, symbol: str, liq: float) -> float | None:
        """The highest stop that still fires before this short's liquidation (margin.stop_cap: liq minus
        liq_buffer_pct), rounded DOWN to the tick: never past the cap, so a stop placed there is never moved again."""
        cap = margin.stop_cap(liq, self.ex.limits.liq_buffer_pct)
        tick = self.t.market(symbol)["precision"]["price"]
        if cap is None or not tick:
            return cap
        t = Decimal(str(tick))
        return float((Decimal(str(cap)) / t).to_integral_value(ROUND_FLOOR) * t)

    async def _close_past(self, symbol: str, pos: dict, jt: dict, reason: str) -> None:
        """Price is already past the stop this short needs: close it now, as that stop would have (then journal it)."""
        from .trading import _f, _sleep
        await self._market_close(symbol, _f(pos["contracts"]))
        if jt:
            await _sleep(1)
            await reconcile.finalize(self, jt, reason)

    async def cap_stop(self, symbol: str) -> dict | None:
        """A resting closePosition stop past liquidation minus liq_buffer_pct never fires: the isolated short is
        liquidated first and pays the liquidation fee. Tighten it to _liq_cap (the level ensure_stop restores at), make
        before break (stops.set_stop), under the symbol lock; price already past that level closes the short, as
        ensure_stop does. Tighten only, by a tick or more; the journal's plan stop stays (the guardian's alert asks for
        a fix). None when nothing is due."""
        from .trading import _f
        async with self.symbol_lock(symbol, "guardian: stop inside liquidation"):
            await self.ex.trade_markets(symbol)   # its tick: a perp listed since start is known first
            close, _ = await stops.listed(self, symbol)
            pos = await self.position(symbol)
            if not close or not pos:
                return None   # no closePosition stop: ensure_stop's; flat: nothing to protect
            cur, liq = min(stops.trigger_of(o) for o in close), _f(pos.get("liquidationPrice"))
            cap = self._liq_cap(symbol, liq)
            tick = _f(self.t.market(symbol)["precision"]["price"])
            if cap is None or cur - cap < tick / 2:
                return None   # inside the cap (never loosened), or less than a tick past it
            out = {"symbol": symbol, "from": cur, "to": cap, "liquidation": liq, "mark": _f(pos.get("markPrice"))}
            if cap <= out["mark"]:
                await self._close_past(symbol, pos, self.j.active_trade(symbol) or {},
                                       f"guardian: stop {cur:g} past liquidation {liq:g}, price past {cap:g}")
                return {**out, "action": "closed"}
            res = await stops.set_stop(self, symbol, cap, _f(pos["contracts"]),
                                       [{"id": o["id"], "trigger": stops.trigger_of(o)} for o in close])
            return {**out, "action": "closed" if res.get("closed") else "tightened" if res.get("ok") else "failed",
                    **({} if res.get("ok") else {"error": res.get("error")})}
