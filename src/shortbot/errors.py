"""The order path's error vocabulary (#37): every outcome of an exchange call that the order code (and the order
gateway, #36) must tell apart is one type here. Side-agnostic. A leaf module: ccxt and the stdlib only (weight.py is
stdlib-only too).

An error maps to its type by the Binance code in the answer first ('binanceusdm {"code":-2022,...}'), then by the ccxt
class, then by the Python type: ccxt 4.5.84 matches the message before the code, folds several codes into one class and
raises a bare ExchangeError for a code missing from its maps (binance.py handle_errors). Each type carries two flags:
- unknown: the request may have executed. Resolve it by its client id and the position; never re-send it blind (#9, #13)
- transient: the same call may succeed later unchanged: retry next pass, nobody needs to act

Sources, checked 2026-10-02: the USDⓈ-M error codes
(https://developers.binance.com/docs/derivatives/usds-margined-futures/error-code) and HTTP codes (.../general-info: 429
rate limit; 418 IP ban; 5XX on Binance's side; a 503 "Unknown error", -1006 or -1007 leave the execution status
UNKNOWN; -1008 throttling spares reduce-only and close-position orders). -4130
left the code page (message: dev.binance.vision/t/16847; testnet probes still see it, tests/fake_errors.py). ccxt's
classes: binance.py handle_errors (418/429 -> DDoSProtection before anything else) and base/exchange.py httpExceptions
(5xx -> ExchangeNotAvailable, 408/504 -> RequestTimeout). tests/test_errors.py runs ccxt's own handler on every code.
"""

from __future__ import annotations

import logging
import math
import re
import time
from typing import ClassVar

import ccxt.async_support as ccxt

from .weight import BudgetWait

log = logging.getLogger("shortbot.errors")


class OrderError(Exception):
    """One outcome of an exchange call. `cause`: the original error; `code`: its Binance code, if the answer had one;
    `sent` (docs/GATEWAY.md §6): None = unknown, it may have run; True = Binance answered, nothing ran; False = it
    never left this process (ccxt's own checks: a definite type without a code). to_dict / from_dict: over a wire."""
    unknown: ClassVar[bool] = False
    transient: ClassVar[bool] = False

    def __init__(self, cause: BaseException | None = None, msg: str | None = None):
        super().__init__(msg or (str(cause) if cause is not None else type(self).__name__))
        self.cause = cause
        self.code = code(cause) if cause is not None else None
        self.sent: bool | None = None if self.unknown else self.code is not None

    @property
    def retryable(self) -> bool:
        return self.transient

    def to_dict(self) -> dict:
        return {"type": type(self).__name__, "code": self.code, "message": str(self), "sent": self.sent,
                "unknown": self.unknown, "retryable": self.transient}

    @staticmethod
    def from_dict(d: dict) -> OrderError:
        e = next((k for k in TYPES if k.__name__ == d.get("type")), Unexpected)(None, d.get("message"))
        e.code, e.sent = d.get("code"), d.get("sent", e.sent)
        return e


class Gone(OrderError):
    """-2011 (cancel) / -2013 (query): not on the book. Filled, cancelled, expired or never there: Binance answers the
    same for a FILLED order (MOVR 2026-10-01), so query it when the difference matters."""


class WouldTrigger(OrderError):
    """-2021 / -4142: a stop or take-profit would trigger at once; price is already past it. Nothing was placed."""


class StopExists(OrderError):
    """-4130: a closePosition stop already rests on that side. Nothing was placed; the listing says which one rests."""


class ReduceOnlyRejected(OrderError):
    """The position is gone, or smaller than the order: -2022 / -4118 / -2024, a reduce-only order that would reduce
    nothing; -4509, a closePosition stop placed while flat (demo probe #55, 2026-10-02: off the code page, unmapped by
    ccxt). Nothing ran: re-read the position (flat = a stop, a target or a fill closed it first: done, not failed).
    Demo caps an oversized reduce-only order at the position (#55), so -2022 means flat or a conflicting order."""


class ClockDrift(OrderError):
    """-1021: the request's timestamp is outside recvWindow. Resync the clock. Unknown: Binance has answered -1021 after
    a cancel had executed (ONDO #5, 2026-09-28)."""
    unknown = transient = True


class OutcomeUnknown(OrderError):
    """-1000 / -1001 / -1006 / -1007 / -1008, HTTP 5xx / 408, a timeout or a dropped connection: it may have run."""
    unknown = transient = True


class RateLimited(OrderError):
    """HTTP 429 / 418 (IP ban), -1003 / -1015 too many requests, or our own weight guard (weight.BudgetWait). Back off.
    Binance documents no guarantee that a 429'd order did not run: unknown, resolved by its client id like a timeout."""
    unknown = transient = True


class InsufficientMargin(OrderError):
    """-2019 margin / -2018 balance insufficient. Nothing ran: size down or free margin."""


class Precision(OrderError):
    """The symbol's filters: -1111 precision, -1013 filter failure, -4003 / -4005 quantity, -4013 / -4014 price tick,
    -4023 step, -4164 min notional; and ccxt's own amount_to_precision under one step. Nothing was sent or ran."""


class MarketClosed(OrderError):
    """The symbol (or the account) takes no opening order: closed or settling (-4141, -1122, -4140), unknown or delisted
    (-1121, ccxt BadSymbol), reduce-only only (-4087, -4088, -4400, -4401). Nothing ran; closes may still be allowed."""


class Refused(OrderError):
    """Any other definite refusal (an ExchangeError subclass ccxt mapped, or -4120 wrong endpoint). Nothing ran."""


class Unexpected(OrderError):
    """Anything else: a code ccxt doesn't know, or a bug. Unknown for a send (it may have run); a site that carries on
    after it tells a human (report)."""
    unknown = True


TYPES: tuple[type[OrderError], ...] = (Gone, WouldTrigger, StopExists, ReduceOnlyRejected, ClockDrift, OutcomeUnknown,
                                       RateLimited, InsufficientMargin, Precision, MarketClosed, Refused, Unexpected)

_CODES: dict[int, type[OrderError]] = {
    **dict.fromkeys((-2011, -2013), Gone), **dict.fromkeys((-2021, -4142), WouldTrigger), -4130: StopExists,
    **dict.fromkeys((-2022, -4118, -2024, -4509), ReduceOnlyRejected), -1021: ClockDrift,
    **dict.fromkeys((-1000, -1001, -1006, -1007, -1008), OutcomeUnknown), **dict.fromkeys((-1003, -1015), RateLimited),
    **dict.fromkeys((-2018, -2019), InsufficientMargin),
    **dict.fromkeys((-1111, -1013, -4003, -4005, -4013, -4014, -4023, -4164), Precision),
    **dict.fromkeys((-4141, -1122, -4140, -1121, -4087, -4088, -4400, -4401), MarketClosed), -4120: Refused}
_CLASSES: tuple[tuple[type[BaseException], type[OrderError]], ...] = (   # without a code: the first match wins
    (ccxt.OrderNotFound, Gone), (ccxt.OrderImmediatelyFillable, WouldTrigger), (ccxt.InvalidNonce, ClockDrift),
    (ccxt.DDoSProtection, RateLimited), (ccxt.RateLimitExceeded, RateLimited), (BudgetWait, RateLimited),
    (ccxt.InsufficientFunds, InsufficientMargin), (ccxt.MarketClosed, MarketClosed), (ccxt.BadSymbol, MarketClosed),
    (ccxt.ContractUnavailable, MarketClosed),
    (ccxt.OperationFailed, OutcomeUnknown),   # NetworkError, RequestTimeout, ExchangeNotAvailable, BadResponse...
    (TimeoutError, OutcomeUnknown), (ConnectionError, OutcomeUnknown))
_CODE = re.compile(r'"code"\s*:\s*(-?\d+)')


def code(e: BaseException | None) -> int | None:
    """The Binance error code in a ccxt error ('binanceusdm {"code":-4130,...}'), if the answer carried one."""
    m = _CODE.search(str(e)) if e is not None else None
    return int(m.group(1)) if m else None


def classify(e: BaseException, refusals: tuple[int, ...] = ()) -> type[OrderError]:
    """The type of this error. `refusals`: codes the caller knows to be definite refusals for its request, beyond the
    table (an unknown or unmapped code in it counts as Refused)."""
    if isinstance(e, OrderError):
        return next((k for k in type(e).__mro__ if k in TYPES), Unexpected)
    c = code(e)
    if c in _CODES and (c not in refusals or not _CODES[c].unknown):
        return _CODES[c]
    if c in refusals:
        return Refused
    for cls, kind in _CLASSES:
        if isinstance(e, cls):
            return kind
    if c is None and "-1021" in str(e):   # stops.is_clock_drift's legacy match: a -1021 ccxt raised unparsed
        return ClockDrift
    if isinstance(e, ccxt.InvalidOrder) and c is None and "precision" in str(e):   # amount_to_precision, client side
        return Precision
    if isinstance(e, ccxt.ExchangeError) and type(e) is not ccxt.ExchangeError:
        return Refused
    return Unexpected


def wrap(e: BaseException) -> OrderError:
    """This error as its type (the gateway raises these), the original as `cause`."""
    return e if isinstance(e, OrderError) else classify(e)(e)


def transient(e: BaseException) -> bool:
    return classify(e).transient


def describe(e: BaseException) -> str:
    """'InsufficientMargin -2019: binanceusdm {...}': what a refusal message, a journal row or an alert shows."""
    k, c = classify(e), code(e)
    body = str(e) if isinstance(e, (ccxt.BaseError, OrderError)) else f"{type(e).__name__}: {e}"
    return f"{k.__name__}{f' {c}' if c is not None else ''}: {body}"


REPORT_EVERY_S = 3600.0
_reported: dict[tuple[str, str], float] = {}


async def report(alert, where: str, e: BaseException, symbol: str | None = None) -> None:
    """A site that carries on after an error it did not expect (not transient): the traceback is logged every time, and
    a human is told once per REPORT_EVERY_S per (site, error type) in this process. `alert`: Trader.alert's shape
    (kind, message, symbol, urgent), or None. Never raises: telling must not cost the caller its pass."""
    log.error("%s: %s", where, describe(e), exc_info=e)
    key, now = (where, type(e).__name__), time.monotonic()
    if alert is None or now - _reported.get(key, -math.inf) < REPORT_EVERY_S:
        return
    _reported[key] = now
    try:
        await alert("unexpected_error", f"{where}: {describe(e)}. Carried on (retried next pass); see the log for the "
                    "traceback", symbol, False)
    except Exception:  # noqa: BLE001 - an alert outage must not cost the caller its pass; logged above
        log.exception("unexpected_error alert for %s not sent", where)
