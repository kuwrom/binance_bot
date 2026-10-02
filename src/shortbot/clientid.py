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
ExchangeError('binanceusdm ' + body) (binance.py handle_errors), e.g. -4130 and -4120.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

import ccxt.async_support as ccxt

PREFIX = "sb"
KINDS = {"entry": "e", "prearm": "p", "open": "o", "close": "c", "stop": "s", "add": "a",   # one letter each in the id
         "bridge": "b",   # stops.py: the reduce-only stop that covers a stop swap
         "target": "t",   # targets.py: a plan target resting as a reduce-only limit, ref = the trade id
         "probe": "x"}    # probes.py: a demo exchange-behaviour probe's order (#55), never the desk's
VALID = re.compile(r"^[\.A-Z\:/a-z0-9_-]{1,36}$")
# definite refusals of a closePosition stop that ccxt raises as a bare ExchangeError (or may): one already rests on the
# side (-4130), wrong endpoint (-4120), it would trigger at once (-2021). Nothing was placed: no lookup needed.
STOP_REFUSALS = (-4130, -4120, -2021)


class Unknown(Exception):
    """An order's outcome stays unknown after looking it up by its client id: it may have run, or still run. Never
    re-send it blind; re-read the position first."""

    def __init__(self, cid: str, cause: BaseException):
        super().__init__(f"send status unknown, {cid} not found ({type(cause).__name__}: {cause})")
        self.cid, self.cause = cid, cause


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


def code(e: BaseException) -> int | None:
    """The Binance error code in a ccxt error ('binanceusdm {"code":-4130,...}'), if the answer carried one."""
    m = re.search(r'"code"\s*:\s*(-?\d+)', str(e))
    return int(m.group(1)) if m else None


def ours(cid: Any) -> bool:
    return str(cid or "").startswith(PREFIX + "-")


def send_unknown(e: BaseException, refusals: tuple[int, ...] = ()) -> bool:
    """True when an order request may have executed although it raised: timeouts, transport errors, HTTP 5xx,
    -1000/-1006/-1007, a -1021 answered after the order executed (seen on cancels, ONDO #5), an error code ccxt doesn't
    map. False for a definite refusal: an ExchangeError subclass (price filter, min notional, margin...) or a code in
    `refusals` (e.g. STOP_REFUSALS)."""
    if refusals and code(e) in refusals:
        return False
    return not (isinstance(e, ccxt.ExchangeError) and type(e) is not ccxt.ExchangeError)


async def lookup(t, symbol: str, cid: str, trigger: bool = False) -> dict[str, Any] | None:
    """The order behind a client id (trigger: a conditional order, by clientAlgoId), or None if Binance doesn't know it
    (-2013). Other errors propagate: the outcome stays unknown. A conditional order's not-found answer is unverified
    (demo probe #55): callers also check the open conditional orders."""
    try:
        return await t.fetch_order(None, symbol, {"clientOrderId": cid, **({"trigger": True} if trigger else {})})
    except ccxt.OrderNotFound:
        return None
