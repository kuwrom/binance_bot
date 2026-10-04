"""Order primitives (mixed into Trader). The market ones: the reduce-only market BUY that closes and the market SELL
that opens or adds, each under its own client id (clientid.py, #9), and the settling of an answer that was lost. Every
market order that comes back with an exchange id is journalled on the symbol's trade (order_ids.py, #76): its position
is then matched in trade history by id, not by time.

The resting ones (cancel, place_limit, edit_limit, place_reduce_stop, place_reduce_limit) send ONE request and return
ccxt's answer, or raise its error, as is: the flows that call them (entries_*, stops, targets_sync, orphans) journal
first, classify the error and resolve an ambiguous answer by its client id. They are the only way any other module
writes to the exchange (#36 gateway step 0; GATEWAY.md §7 makes them side-aware later). Each one tells the intent it
runs in what it sends (#36 steps 2-3): an order under a client id is linked just before the send and settled with
what Binance answered (intents.send / settle); a cancel or an edit is recorded against the order it targets
(intent_changes.change, #86).

trading.py imports this module, so `_f` and `_sleep` are imported inside each method at call time: a module-level
import would be circular, and would bind `_sleep` before the tests patch `shortbot.trading._sleep`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from . import clientid, errors, intent_changes, intents, order_ids

if TYPE_CHECKING:
    from .exchange import Exchanges
    from .journal import Journal

Order = dict[str, Any]   # ccxt's unified order structure


class Client(Protocol):
    """The ccxt binanceusdm calls these primitives make, as ccxt 4.5.84 declares them (ccxt ships no type information,
    so to mypy the real client is Any: this is the boundary its answers cross)."""

    async def create_order(self, symbol: str, type: str, side: str, amount: float, price: float | None = None,
                           params: dict[str, Any] = ...) -> Order: ...

    async def cancel_order(self, id: str, symbol: str | None = None, params: dict[str, Any] = ...) -> Order: ...

    async def edit_order(self, id: str, symbol: str, type: str, side: str, amount: float | None = None,
                         price: float | None = None, params: dict[str, Any] = ...) -> Order: ...

    # id: None to look it up by the clientOrderId / clientAlgoId in params (clientid.lookup), as binance.py reads it
    async def fetch_order(self, id: str | None, symbol: str | None = None, params: dict[str, Any] = ...) -> Order: ...

    async def fetch_my_trades(self, symbol: str | None = None, since: int | None = None, limit: int | None = None,
                              params: dict[str, Any] = ...) -> list[dict[str, Any]]: ...

    def price_to_precision(self, symbol: str, price: object) -> str: ...

    async def fapiPrivatePostListenKey(self, params: dict[str, Any] = ...) -> dict[str, Any] | None: ...

    async def fapiPrivatePutListenKey(self, params: dict[str, Any] = ...) -> dict[str, Any] | None: ...


class OrderFlow:
    """Uses Trader's exchange client, journal and position()."""
    ex: Exchanges
    j: Journal

    if TYPE_CHECKING:   # Trader's
        @property
        def t(self) -> Client: ...

        async def position(self, symbol: str) -> dict[str, Any] | None: ...

    async def close_market(self, symbol: str, amount: float, tries: int = 3, whole: bool = True) -> Order:
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

    async def _close_sends(self, symbol: str, amount: float, tries: int, whole: bool) -> Order:
        from .trading import _sleep
        doubt: list[str] = []
        last: Exception | None = None
        for i in range(tries):
            if whole or not doubt:
                cid = clientid.new("close", clientid.tag(symbol))
                try:
                    return await intents.send(self.j, cid, symbol, self.t.create_order(
                        symbol, "market", "buy", amount, None, {"reduceOnly": True, "clientOrderId": cid}))
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

    async def _flat_first(self, symbol: str, cid: str, e: Exception, doubt: list[str]) -> Order | None:
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

    async def _close_outcome(self, symbol: str, doubt: list[str]) -> Order | None:
        """Settle close sends of unknown outcome: the newest one found with a fill; one found dead and unfilled leaves
        `doubt`; {"flat": True} when the position reads flat; None while still in doubt."""
        from .trading import _f
        for cid in reversed(list(doubt)):
            try:
                o = await clientid.lookup(self.t, symbol, cid)
            except Exception:  # noqa: BLE001 - still unknown
                continue
            if o and _f(o.get("filled")) > 0:
                intents.settle(self.j, cid, clientid.outcome(cid, o))
                return o
            if o and o.get("status") in clientid.DEAD:
                intents.settle(self.j, cid, "unfilled")
                doubt.remove(cid)
        try:
            pos = await self.position(symbol)
        except Exception:  # noqa: BLE001 - still unknown
            return None
        if pos is None:
            return {"id": None, "clientOrderId": doubt[-1] if doubt else None, "flat": True, "status": None,
                    "filled": None, "average": None}
        return None

    async def open_market(self, symbol: str, amount: float, cid: str, lookups: int = 3) -> Order:
        """A market SELL that opens or adds to the short, sent ONCE under client id `cid` (journalled by the caller).
        Returns the order as acked, or as found settled by `cid` after an ambiguous answer, journalled as an entry of
        the symbol's trade. Raises a definite refusal as is, or clientid.Unknown when `cid` stays unsettled: it may
        still fill, so it is never re-sent."""
        order = await self._sell_once(symbol, amount, cid, lookups)
        order_ids.note(self.j, symbol, "entry", order)
        return order

    async def _sell_once(self, symbol: str, amount: float, cid: str, lookups: int) -> Order:
        from .trading import _f, _sleep
        try:
            return await intents.send(self.j, cid, symbol, self.t.create_order(symbol, "market", "sell", amount,
                                                                                None, {"clientOrderId": cid}))
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
                if o is not None and (_f(o.get("filled")) > 0 or o.get("status") in clientid.DEAD):
                    intents.settle(self.j, cid, clientid.outcome(cid, o))
                    return o
            raise clientid.Unknown(cid, e) from e

    async def cancel(self, symbol: str, order_id: str, trigger: bool = False,
                     note: dict[str, Any] | None = None) -> Order:
        """Cancel one order: a conditional one (trigger=True: a stop, bridge or pre-arm) or a limit (entry, target).
        -2011 (ccxt OrderNotFound) means it is off the book: filled, triggered or cancelled before. Recorded against
        the order (intent_changes.py: a cancel has no id of its own); `note`: what it is sent for."""
        req = (self.t.cancel_order(order_id, symbol, params={"trigger": True}) if trigger
               else self.t.cancel_order(order_id, symbol))
        return await intent_changes.change(self.j, "cancel", symbol, order_id, req, trigger=trigger, note=note)

    async def place_limit(self, symbol: str, amount: float, price: float, expires_ms: int, cid: str) -> Order:
        """A GTD limit SELL that opens a short when it fills (an entry, entries_place.py), resting to `expires_ms`."""
        return await intents.send(self.j, cid, symbol, self.t.create_order(
            symbol, "limit", "sell", amount, price,
            {"timeInForce": "GTD", "goodTillDate": expires_ms, "clientOrderId": cid}))

    async def edit_limit(self, symbol: str, order_id: str, amount: float, price: float) -> Order:
        """Move a resting entry limit SELL to `price` (Modify Order: same order id), recorded against the order."""
        return await intent_changes.change(self.j, "edit", symbol, order_id,
                                           self.t.edit_order(order_id, symbol, "limit", "sell", amount, price),
                                           want=price)

    async def place_reduce_stop(self, symbol: str, amount: float, trigger: float, cid: str) -> Order:
        """A reduce-only STOP_MARKET BUY of `amount` (mark price): the bridge that covers a stop swap (stops.py) or an
        entry's pre-armed stop (entries_place.py). Unlike place_close_stop it has a size; a side may hold several."""
        return await intents.send(self.j, cid, symbol, self.t.create_order(
            symbol, "STOP_MARKET", "buy", amount, None, {
                "triggerPrice": float(self.t.price_to_precision(symbol, trigger)), "reduceOnly": True,
                "workingType": "MARK_PRICE", "clientOrderId": cid}))

    async def place_reduce_limit(self, symbol: str, qty: float, price: float, cid: str) -> Order:
        """A reduce-only GTC limit BUY: a plan target resting on the book (targets_sync.py)."""
        return await intents.send(self.j, cid, symbol, self.t.create_order(
            symbol, "limit", "buy", qty, price, {"reduceOnly": True, "timeInForce": "GTC", "clientOrderId": cid}))

    async def listen_key(self) -> str:
        """POST /fapi/v1/listenKey (weight 1): the account's user-data stream key (user_stream.py, #54). "If the
        account has an active listenKey, that listenKey will be returned and its validity will be extended for 60
        minutes", else a new one (binance-connector-python user_data_streams_api.py). One key per account, shared by
        every process on it: there is deliberately no primitive that DELETEs it (that would end the others' streams)."""
        key = (await self.t.fapiPrivatePostListenKey() or {}).get("listenKey")
        if not key:
            raise RuntimeError("POST /fapi/v1/listenKey answered no listenKey")
        return str(key)

    async def keep_listen_key(self) -> str:
        """PUT /fapi/v1/listenKey (weight 1, no key sent): extends the account's active key 60 min and returns it, so a
        caller holding another key knows it was replaced. None active: -1125 (ccxt AuthenticationError)."""
        key = (await self.t.fapiPrivatePutListenKey() or {}).get("listenKey")
        if not key:
            raise RuntimeError("PUT /fapi/v1/listenKey answered no listenKey")
        return str(key)
