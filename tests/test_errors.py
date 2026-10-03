"""The order path's error vocabulary (errors.py, #37): every Binance answer it must tell apart maps to one type, checked
against ccxt 4.5.84's OWN handle_errors (so neither the fake nor the table can drift from what ccxt raises)."""

import asyncio
import inspect
import json

import ccxt
import pytest

from shortbot import clientid, errors
from shortbot.weight import BudgetWait
from tests import fake_errors as err
from tests.fakes import SYM, FakeCcxt

URL = f"{err.DEMO_FAPI}/order"
E = errors

# (code, Binance's message, type): messages from the USDⓈ-M error-code page (-4130: dev.binance.vision/t/16847)
CODES = [
    (-2011, "Unknown order sent.", E.Gone), (-2013, "Order does not exist.", E.Gone),
    (-2021, "Order would immediately trigger.", E.WouldTrigger),
    (-4142, "REJECT: take profit or stop order will be triggered immediately.", E.WouldTrigger),
    (-4130, "An open stop or take profit order with GTE and closePosition in the direction is existing.",
     E.StopExists),
    (-2022, "ReduceOnly Order is rejected.", E.ReduceOnlyRejected),
    (-4118, "ReduceOnly Order Failed. Please check your existing position and open orders", E.ReduceOnlyRejected),
    (-2024, "Position is not sufficient.", E.ReduceOnlyRejected),
    (-4509, "Time in Force (TIF) GTE can only be used with open positions. Please ensure that positions are "
     "available.", E.ReduceOnlyRejected),   # demo probe #55, 2026-10-02
    (-1021, "Timestamp for this request is outside of the recvWindow.", E.ClockDrift),
    (-1000, "An unknown error occured while processing the request.", E.OutcomeUnknown),
    (-1001, "Internal error; unable to process your request. Please try again.", E.OutcomeUnknown),
    (-1006, "An unexpected response was received from the message bus. Execution status unknown.", E.OutcomeUnknown),
    (-1007, "Timeout waiting for response from backend server. Send status unknown; execution status unknown.",
     E.OutcomeUnknown),
    (-1008, "Request throttled by system-level protection. Reduce-only/close-position orders are exempt.",
     E.OutcomeUnknown),
    (-1003, "Too many requests; current limit is 2400 requests per minute.", E.RateLimited),
    (-1015, "Too many new orders.", E.RateLimited),
    (-2018, "Balance is insufficient.", E.InsufficientMargin), (-2019, "Margin is insufficient.", E.InsufficientMargin),
    (-1111, "Precision is over the maximum defined for this asset.", E.Precision),
    (-1013, "Filter failure: LOT_SIZE", E.Precision), (-4003, "Quantity less than zero.", E.Precision),
    (-4005, "Quantity greater than max quantity.", E.Precision),
    (-4014, "Price not increased by tick size.", E.Precision), (-4023, "Qty not increased by step size.", E.Precision),
    (-4164, "Order's notional must be no smaller than 5.0 (unless you choose reduce only).", E.Precision),
    (-4141, "Symbol is closed.", E.MarketClosed), (-1122, "Invalid symbol status.", E.MarketClosed),
    (-4140, "Invalid symbol status for opening position.", E.MarketClosed), (-1121, "Invalid symbol.", E.MarketClosed),
    (-4087, "User can only place reduce only order.", E.MarketClosed),
    (-4088, "User can not place order currently.", E.MarketClosed),
    (-4400, "Futures Trading Quantitative Rules violated, only reduceOnly order is allowed.", E.MarketClosed),
    (-4401, "Futures Trading Risk Control Rules violated, only reduceOnly order allowed.", E.MarketClosed),
    (-4120, "Order type not supported for this endpoint. Please use the Algo Order API endpoints instead.", E.Refused),
    (-4045, "Reach max stop order limit.", E.Refused), (-2010, "NEW_ORDER_REJECTED", E.Refused),
    (-4116, "ClientOrderId is duplicated.", E.Refused), (-1102, "A mandatory parameter was not sent.", E.Refused),
    (-4131, "The counterparty's best price does not meet the PERCENT_PRICE filter limit.", E.Refused),
    (-5022, "Due to the order could not be executed as maker, the Post Only order will be rejected.", E.Refused),
    (-4999, "A code ccxt has never seen.", E.Unexpected),
]


def ccxt_raises(status: int, body: str, url: str = URL) -> Exception:
    """What ccxt 4.5.84's binanceusdm raises for this HTTP answer (handle_errors, then the HTTP-status fallback)."""
    x = ccxt.binanceusdm()
    x.options["hasAlreadyAuthenticatedSuccessfully"] = True
    try:
        parsed = json.loads(body) if body.startswith("{") else None
        x.handle_errors(status, "Bad Request", url, "POST", {}, body, parsed, None, None)
        x.handle_http_status_code(status, "Bad Request", url, "POST", body)
    except Exception as e:  # noqa: BLE001 - the answer under test
        return e
    raise AssertionError(f"ccxt raised nothing for {status} {body}")


@pytest.mark.parametrize("code, msg, kind", CODES, ids=[str(c[0]) for c in CODES])
def test_each_binance_code_maps_to_one_type_through_ccxts_own_handler(code, msg, kind):
    e = ccxt_raises(400, json.dumps({"code": code, "msg": msg}))
    assert E.classify(e) is kind, f"{type(e).__name__}: {e}"
    assert E.code(e) == code


@pytest.mark.parametrize("status, kind", [(429, E.RateLimited), (418, E.RateLimited), (503, E.OutcomeUnknown),
                                          (502, E.OutcomeUnknown), (504, E.OutcomeUnknown), (408, E.OutcomeUnknown)])
def test_http_answers_without_a_code(status, kind):
    assert E.classify(ccxt_raises(status, "<html>busy</html>")) is kind


def _built():
    """Every fake_errors builder that answers with a Binance code, with the arguments it needs."""
    args = {"min_notional": (5.0,), "unavailable": ("order", True), "unmapped": ()}
    for name, f in inspect.getmembers(err, inspect.isfunction):
        if f.__module__ == err.__name__ and not name.startswith("_"):
            yield name, f(*args.get(name, ()))


@pytest.mark.parametrize("name, e", list(_built()), ids=[n for n, _ in _built()])
def test_the_fakes_errors_are_what_ccxt_raises(name, e):
    """fake_errors builds each class by hand: ccxt's own handler, fed the same body, must raise the same class."""
    if E.code(e) is None:
        pytest.skip("no Binance code: a transport or client-side error")
    status = 429 if " 429 " in str(e) else 400
    body = str(e)[str(e).index("{"):]
    assert type(ccxt_raises(status, body)) is type(e)


@pytest.mark.parametrize("e, kind", [
    (ccxt.RequestTimeout("timed out"), E.OutcomeUnknown), (ccxt.ExchangeNotAvailable("503"), E.OutcomeUnknown),
    (ccxt.NetworkError("reset"), E.OutcomeUnknown), (ccxt.BadResponse("garbled"), E.OutcomeUnknown),
    (ccxt.OperationFailed("x"), E.OutcomeUnknown), (asyncio.TimeoutError(), E.OutcomeUnknown),
    (TimeoutError(), E.OutcomeUnknown), (ConnectionResetError(), E.OutcomeUnknown),
    (clientid.Unknown("sb-o1-ab", err.status_unknown()), E.OutcomeUnknown),
    (ccxt.InvalidNonce("-1021"), E.ClockDrift), (ccxt.ExchangeError("answered -1021 late"), E.ClockDrift),
    (ccxt.DDoSProtection("418"), E.RateLimited), (BudgetWait("budget_wait: research"), E.RateLimited),
    (ccxt.BadSymbol("binanceusdm does not have market symbol X/USDT:USDT"), E.MarketClosed),
    (ccxt.InsufficientFunds("no code"), E.InsufficientMargin), (ccxt.OrderNotFound("no code"), E.Gone),
    (ccxt.BadRequest("no code"), E.Refused), (ccxt.InvalidOrder("createOrder() requires a price"), E.Refused),
    (ccxt.ExchangeError("binanceusdm {}"), E.Unexpected), (ValueError("a bug"), E.Unexpected),
    (KeyError("filled"), E.Unexpected)])
def test_without_a_code_the_class_decides(e, kind):
    assert E.classify(e) is kind


def test_the_code_wins_over_the_class():
    """ccxt matches messages before codes and folds several codes into one class: the code decides."""
    assert E.classify(ccxt.InvalidOrder('binanceusdm {"code":-2022,"msg":"x"}')) is E.ReduceOnlyRejected
    assert E.classify(ccxt.InsufficientFunds('binanceusdm {"code":-2024,"msg":"x"}')) is E.ReduceOnlyRejected
    assert E.classify(ccxt.InvalidOrder('binanceusdm {"code":-1013,"msg":"Filter failure: LOT_SIZE"}')) is E.Precision


def test_amount_to_precision_below_one_step_is_precision():
    fake = FakeCcxt()
    with pytest.raises(ccxt.InvalidOrder) as got:
        fake.amount_to_precision(SYM, 0.01)
    assert E.classify(got.value) is E.Precision


def test_flags_unknown_means_never_resend_and_transient_means_retry_unchanged():
    unknown = {k for k in E.TYPES if k.unknown}
    transient = {k for k in E.TYPES if k.transient}
    assert unknown == {E.ClockDrift, E.OutcomeUnknown, E.RateLimited, E.Unexpected}
    assert transient == {E.ClockDrift, E.OutcomeUnknown, E.RateLimited}
    assert all(issubclass(k, E.OrderError) for k in E.TYPES) and len(E.TYPES) == 12


def test_definite_refusals_are_not_unknown_and_an_unmapped_code_is():
    for e in (err.one_close_position_stop(), err.switch_algo(), err.would_trigger(), err.margin_insufficient()):
        assert not E.classify(e).unknown   # definite refusals in every context: nothing was placed
    assert E.classify(err.unmapped(-4999)).unknown and E.classify(err.status_unknown()).unknown


def test_describe_names_the_type_and_code():
    assert E.describe(err.margin_insufficient()).startswith("InsufficientMargin -2019: binanceusdm")
    assert E.describe(ValueError("x")) == "Unexpected: ValueError: x"


async def test_report_logs_every_time_and_alerts_once_per_site_and_type(monkeypatch):
    sent = []

    async def alert(kind, msg, symbol, urgent):
        sent.append((kind, msg, symbol, urgent))
    monkeypatch.setattr(E, "_reported", {})
    for _ in range(3):
        await E.report(alert, "entry_intents.reconcile", ValueError("a bug"), SYM)
    await E.report(alert, "entry_intents.reconcile", KeyError("x"), SYM)
    await E.report(alert, "orphans.sweep", ValueError("a bug"))
    assert [(k, s) for k, _, s, _ in sent] == [("unexpected_error", SYM)] * 2 + [("unexpected_error", None)]
    assert "Unexpected: ValueError: a bug" in sent[0][1] and not any(u for *_, u in sent)

    async def broken(*a):
        raise RuntimeError("telegram down")
    await E.report(broken, "elsewhere", ValueError("y"))   # never raises
