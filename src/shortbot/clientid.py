"""Client order ids for write-ahead order intents: journal the id, then send, then resolve the outcome by the id.

Binance USDⓈ-M (developers.binance.com, checked 2026-10-01):
- newClientOrderId (orders) and clientAlgoId (conditional orders, Algo Service) match ^[\\.A-Z\\:/a-z0-9_-]{1,36}$ and
  are "unique among open orders" only: a lookup key, NOT a dedup key. A filled order frees its id, so resending an
  intent whose outcome is unknown can trade twice. Unknown outcomes are resolved by querying, never by resending.
- Query Order takes origClientOrderId. An unfilled CANCELED/EXPIRED order disappears ~3 days after creation (any
  order after 90 days).
- -1006 / -1007 and HTTP 503 "Unknown error" mean the execution status is unknown.
ccxt 4.5.84: params['clientOrderId'] -> newClientOrderId, or clientAlgoId for a linear-swap trigger order (without it
ccxt sends its own 'x-xcKtGhcu' + uuid22); fetch_order / cancel_order map it to origClientOrderId.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

import ccxt.async_support as ccxt

PREFIX = "sb"
KINDS = {"entry": "e", "prearm": "p", "open": "o", "close": "c", "stop": "s"}   # one letter each in the id
VALID = re.compile(r"^[\.A-Z\:/a-z0-9_-]{1,36}$")


def new(kind: str, ref: int | str) -> str:
    """'sb-e8166c604-1f2e3d4c': one id per intent, generated once and journalled BEFORE the order is sent. `ref` (a
    group, entry or trade id) makes it traceable on the exchange; the nonce keeps it unique if a journal is restored."""
    cid = f"{PREFIX}-{KINDS[kind]}{ref}-{secrets.token_hex(4)}"
    if not VALID.match(cid):
        raise ValueError(f"client order id {cid!r} breaks Binance's format (36 chars of [.A-Z:/a-z0-9_-])")
    return cid


def ours(cid: Any) -> bool:
    return str(cid or "").startswith(PREFIX + "-")


def send_unknown(e: BaseException) -> bool:
    """True when an order request may have executed although it raised: timeouts, transport errors, HTTP 5xx,
    -1000/-1006/-1007, a -1021 answered after the order executed (seen on cancels, ONDO #5), an error code ccxt doesn't
    map. False only for a definite refusal (an ExchangeError subclass: price filter, min notional, margin...)."""
    return not (isinstance(e, ccxt.ExchangeError) and type(e) is not ccxt.ExchangeError)


async def lookup(t, symbol: str, cid: str) -> dict[str, Any] | None:
    """The (non-conditional) order behind a client id, or None if Binance doesn't know it (-2013). Other errors
    propagate: the outcome stays unknown. A conditional order's not-found answer is unverified (demo probe #55)."""
    try:
        return await t.fetch_order(None, symbol, {"clientOrderId": cid})
    except ccxt.OrderNotFound:
        return None
