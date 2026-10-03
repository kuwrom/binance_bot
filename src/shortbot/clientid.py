"""Client order ids for write-ahead order intents: journal the id, then send, then resolve the outcome by the id.

Binance USDⓈ-M (developers.binance.com, checked 2026-10-01):
- newClientOrderId (orders) and clientAlgoId (conditional orders, Algo Service) match ^[\\.A-Z\\:/a-z0-9_-]{1,36}$ and
  are "unique among open orders" only: a lookup key, NOT a dedup key. A filled order frees its id, so resending an
  intent whose outcome is unknown can trade twice. Unknown outcomes are resolved by querying, never by resending.
- Query Order takes origClientOrderId. An unfilled CANCELED/EXPIRED order disappears ~3 days after creation (any
  order after 90 days).
- -1006 / -1007 and HTTP 503 "Unknown error" mean the execution status is unknown.
- Query Algo Order takes algoId or clientAlgoId; its answer for an unknown id is undocumented (checked 2026-10-02).
ccxt 4.5.84: params['clientOrderId'] -> newClientOrderId, or clientAlgoId for a linear-swap trigger order (without it
ccxt sends its own 'x-xcKtGhcu' + uuid22); fetch_order / cancel_order map it to origClientOrderId, or with
params['trigger'] to clientAlgoId (GET/DELETE /fapi/v1/algoOrder). A code missing from ccxt's maps raises a bare
ExchangeError('binanceusdm ' + body) (binance.py handle_errors), e.g. -4130 and -4120: errors.py classifies by the code.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

import ccxt.async_support as ccxt

from . import errors

PREFIX = "sb"
KINDS = {"entry": "e", "prearm": "p", "open": "o", "close": "c", "stop": "s", "add": "a",   # one letter each in the id
         "bridge": "b",   # stops.py: the reduce-only stop that covers a stop swap
         "target": "t",   # targets.py: a plan target resting as a reduce-only limit, ref = the trade id
         "probe": "x"}    # probes.py: a demo exchange-behaviour probe's order (#55), never the desk's
VALID = re.compile(r"^[\.A-Z\:/a-z0-9_-]{1,36}$")
DEAD = ("canceled", "expired", "rejected")   # final: an order found unfilled in one of these never fills
MARKET = ("open", "add", "close")            # kinds sent as market orders: their answer says what filled
# What a send did (intent_orders.outcome, #36 step 3): Binance took it (acked: a market order whose answer doesn't say
# yet; placed: a resting or conditional order reached the book), filled, part_filled (the rest expired), unfilled,
# rejected (a definite refusal), not_found (the resolver: unknown to Binance GRACE_S after its send). In doubt: none.
FULL = ("acked", "placed", "filled")
RAN = (*FULL, "part_filled")


class Unknown(errors.OutcomeUnknown):
    """An order's outcome stays unknown after looking it up by its client id: it may have run, or still run. Never
    re-send it blind; re-read the position first."""

    def __init__(self, cid: str, cause: BaseException):
        super().__init__(cause, f"send status unknown, {cid} not found ({type(cause).__name__}: {cause})")
        self.cid = cid


def new(kind: str, ref: int | str) -> str:
    """'sb-e8166c604-1f2e3d4c': one id per intent, generated once and journalled BEFORE the order is sent. `ref` (a
    group, entry or trade id) makes it traceable on the exchange; the nonce keeps it unique if a journal is restored."""
    cid = f"{PREFIX}-{KINDS[kind]}{ref}-{secrets.token_hex(4)}"
    if not VALID.match(cid):
        raise ValueError(f"client order id {cid!r} breaks Binance's format (36 chars of [.A-Z:/a-z0-9_-])")
    return cid


def tag(symbol: str) -> str:
    """An id `ref` from a symbol: its base's ASCII letters and digits ('1000PEPE/USDT:USDT' -> '1000PEPE'; a non-ASCII
    base gives '', so the id stays valid)."""
    return re.sub(r"[^A-Za-z0-9]", "", symbol.split("/")[0])[:12]


def kind_of(cid: str) -> str:
    """The kind an id of ours was made for ('sb-c1000PEPE-1f2e3d4c' -> 'close'); 'other' for any other id."""
    letter = cid[len(PREFIX) + 1:len(PREFIX) + 2] if ours(cid) else ""
    return next((k for k, v in KINDS.items() if v == letter), "other")


def ours(cid: Any) -> bool:
    return str(cid or "").startswith(PREFIX + "-")


def outcome(cid: str, o: dict[str, Any]) -> str:
    """What the order sent under `cid` did, from its answer or as found by the id: a market order filled, part-filled
    or unfilled once its status is final (ccxt sends newOrderRespType=RESULT for swaps), else acked; any other order
    placed (it reached the book; what happened to it later is its own row's: entries, targets, stops)."""
    if kind_of(cid) not in MARKET:
        return "placed"
    if o.get("status") == "closed":
        return "filled"
    if o.get("status") in DEAD:
        return "part_filled" if float(o.get("filled") or 0) > 0 else "unfilled"
    return "acked"


async def lookup(t, symbol: str, cid: str, trigger: bool = False) -> dict[str, Any] | None:
    """The order behind a client id (trigger: a conditional order, by clientAlgoId), or None if Binance doesn't know it
    (-2013). Other errors propagate: the outcome stays unknown. A conditional order's not-found answer is unverified
    (demo probe #55): callers also check the open conditional orders."""
    try:
        return await t.fetch_order(None, symbol, {"clientOrderId": cid, **({"trigger": True} if trigger else {})})
    except ccxt.OrderNotFound:
        return None
