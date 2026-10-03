"""Market order primitives (mixed into Trader): the reduce-only market BUY that closes and the market SELL that opens or
adds, each under its own client id (clientid.py, #9), and the settling of an answer that was lost. Every order that
comes back with an exchange id is journalled on the symbol's trade (order_ids.py, #76): its position is then matched in
trade history by id, not by time.

trading.py imports this module, so `DEAD`, `_f` and `_sleep` are imported inside each method at call time: a
module-level import would be circular, and would bind `_sleep` before the tests patch `shortbot.trading._sleep`.
"""

from __future__ import annotations

from . import clientid, errors, order_ids


class OrderFlow:
    """Uses Trader's exchange client, journal and position()."""

    async def _market_close(self, symbol: str, amount: float, tries: int = 3, whole: bool = True) -> dict:
        """Reduce-only market BUY of `amount`, each send under its own client id. A definite refusal is retried (nothing
        ran), except ReduceOnlyRejected (-2022) on a flat position: the close is done ({"flat": True, "filled": 0.0,
        "rejected": why}: a stop or target closed it first; after a send of unknown outcome, the flat answer below). An
        ambiguous answer is settled, never blindly re-sent: the order found by its id is the answer; a flat position
        leaves nothing to buy back ({"flat": True, "filled": None}); otherwise only a `whole` close (amount = the
        position the caller saw) is sent again, as reduce-only can't buy back more than the position. A partial close
        (whole=False) is never re-sent while in doubt: clientid.Unknown once its tries are spent. The order that ran is
        journalled as an exit of the symbol's trade."""
        order = await self._close_sends(symbol, amount, tries, whole)
        order_ids.note(self.j, symbol, "exit", order)
        return order

    async def _close_sends(self, symbol: str, amount: float, tries: int, whole: bool) -> dict:
        from .trading import _sleep
        doubt: list[str] = []
        last: Exception | None = None
        for i in range(tries):
            if whole or not doubt:
                cid = clientid.new("close", clientid.tag(symbol))
                try:
                    return await self.t.create_order(symbol, "market", "buy", amount, None,
                                                     {"reduceOnly": True, "clientOrderId": cid})
                except Exception as e:  # noqa: BLE001 - this is the last line of defence: classified, then retried
                    last, kind = e, errors.classify(e)
                    if kind is errors.ClockDrift:
                        await self.ex.resync_time()
                    if kind.unknown:
                        doubt.append(cid)
                    elif kind is errors.ReduceOnlyRejected and (flat := await self._flat_first(symbol, cid, e, doubt)):
                        return flat
            await _sleep(2 ** i)
            if doubt and (got := await self._close_outcome(symbol, doubt)) is not None:
                return got
        if doubt and not whole:
            raise clientid.Unknown(doubt[-1], last or RuntimeError("no answer"))
        unsure = f" (outcome unknown: {', '.join(doubt)})" if doubt else ""
        raise RuntimeError(f"market close failed after {tries} tries{unsure}: {last}")

    async def flat_now(self, symbol: str) -> bool:
        """After errors.ReduceOnlyRejected (-2022, -4509): True only when the position reads flat now."""
        try:
            return await self.position(symbol) is None
        except Exception:  # noqa: BLE001 - unreadable counts as not flat: the caller's refusal stands
            return False

    async def _flat_first(self, symbol: str, cid: str, e: Exception, doubt: list[str]) -> dict | None:
        """-2022: the reduce-only close would reduce nothing. Flat now: closed before it landed, so done: by a stop or a
        target ("rejected": nothing of ours filled), or by an earlier send of ours whose answer was lost (`doubt`: the
        unknown-outcome flat answer). Not flat, or unreadable: None, the refusal stands."""
        if not await self.flat_now(symbol):
            return None
        if doubt:
            return {"id": None, "clientOrderId": doubt[-1], "flat": True, "status": None, "filled": None,
                    "average": None}
        return {"id": None, "clientOrderId": cid, "flat": True, "status": "rejected", "filled": 0.0, "average": None,
                "rejected": errors.describe(e)}

    async def _close_outcome(self, symbol: str, doubt: list[str]) -> dict | None:
        """Settle close sends of unknown outcome: the newest one found with a fill; one found dead and unfilled leaves
        `doubt`; {"flat": True} when the position reads flat; None while still in doubt."""
        from .trading import DEAD, _f
        for cid in reversed(list(doubt)):
            try:
                o = await clientid.lookup(self.t, symbol, cid)
            except Exception:  # noqa: BLE001 - still unknown
                continue
            if o and _f(o.get("filled")) > 0:
                return o
            if o and o.get("status") in DEAD:
                doubt.remove(cid)
        try:
            pos = await self.position(symbol)
        except Exception:  # noqa: BLE001 - still unknown
            return None
        if pos is None:
            return {"id": None, "clientOrderId": doubt[-1] if doubt else None, "flat": True, "status": None,
                    "filled": None, "average": None}
        return None

    async def _market_sell(self, symbol: str, amount: float, cid: str, lookups: int = 3) -> dict:
        """A market SELL that opens or adds to the short, sent ONCE under client id `cid` (journalled by the caller).
        Returns the order as acked, or as found settled by `cid` after an ambiguous answer, journalled as an entry of
        the symbol's trade. Raises a definite refusal as is, or clientid.Unknown when `cid` stays unsettled: it may
        still fill, so it is never re-sent."""
        order = await self._sell_once(symbol, amount, cid, lookups)
        order_ids.note(self.j, symbol, "entry", order)
        return order

    async def _sell_once(self, symbol: str, amount: float, cid: str, lookups: int) -> dict:
        from .trading import DEAD, _f, _sleep
        try:
            return await self.t.create_order(symbol, "market", "sell", amount, None, {"clientOrderId": cid})
        except Exception as e:  # noqa: BLE001 - classified: a definite refusal raises as is, else looked up by its id
            kind = errors.classify(e)
            if not kind.unknown:
                raise
            if kind is errors.ClockDrift:
                await self.ex.resync_time()
            for i in range(lookups):
                await _sleep(1 + i)
                try:
                    o = await clientid.lookup(self.t, symbol, cid)
                except Exception:  # noqa: BLE001 - look again
                    continue
                if o is not None and (_f(o.get("filled")) > 0 or o.get("status") in DEAD):
                    return o
            raise clientid.Unknown(cid, e) from e
