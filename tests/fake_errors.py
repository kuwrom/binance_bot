"""The ccxt errors binanceusdm raises for Binance USDⓈ-M answers, built the way ccxt 4.5.84 builds them:
handle_errors() raises `'binanceusdm ' + body`, and picks the class from exceptions['linear']['exact'] first,
then the common 'exact' map (ccxt/async_support/binance.py ~2122 and ~2666).
Codes and messages: https://developers.binance.com/docs/derivatives/usds-margined-futures/error-code
"""

import ccxt

DEMO_FAPI = "https://demo-fapi.binance.com/fapi/v1"


def _body(code: int, msg: str) -> str:
    return f'binanceusdm {{"code":{code},"msg":"{msg}"}}'


def clock_drift():   # common map -1021 -> InvalidNonce (seen live on ONDO #5)
    return ccxt.InvalidNonce(_body(-1021, "Timestamp for this request is outside of the recvWindow."))


def unknown_order():   # -2011 CANCEL_REJECTED: no such OPEN order (filled, cancelled, expired, or wrong endpoint)
    return ccxt.OrderNotFound(_body(-2011, "Unknown order sent."))


def no_such_order():   # -2013 NO_SUCH_ORDER (common map)
    return ccxt.OrderNotFound(_body(-2013, "Order does not exist."))


def lookup_interval_too_big():
    # -1127 MORE_THAN_XX_HOURS (common map -> BadRequest). Inferred: Account-Trade-List says startTime..endTime "cannot
    # be longer than 7 days" but not which code it answers; -1127 is the error-code page's interval error
    return ccxt.BadRequest(_body(-1127, "More than 168 hours between startTime and endTime."))


def margin_insufficient():   # -2019 (linear map -> InsufficientFunds)
    return ccxt.InsufficientFunds(_body(-2019, "Margin is insufficient."))


def would_trigger():   # -2021 (linear map -> OrderImmediatelyFillable); New-Algo-Order doc
    return ccxt.OrderImmediatelyFillable(_body(-2021, "Order would immediately trigger."))


def reduce_only_rejected():   # -2022 (linear map -> InvalidOrder); dev.binance.vision/t/1198: no position to close
    return ccxt.InvalidOrder(_body(-2022, "ReduceOnly Order is rejected."))


def tick_size():   # -4014 PRICE_NOT_INCREASED_BY_TICK_SIZE (common map -> BadRequest)
    return ccxt.BadRequest(_body(-4014, "Price not increased by tick size."))


def position_mode_already_set():   # -4059 (common map -> OperationRejected)
    return ccxt.OperationRejected(_body(-4059, "No need to change position side."))


def open_orders_block_margin_mode():
    # -4067 (common map -> OperationRejected). Live demo 2026-10-01 (MOVR split entry): with an open order on the
    # symbol Binance answers -4067 before it would say "no need to change" (-4046)
    return ccxt.OperationRejected(_body(-4067, "Position side cannot be changed if there exists open orders."))


def margin_mode_already_set():
    # -4046: ccxt 4.5.84 handle_errors raises MarginModeAlreadySet on this message (binance.py ~12462) and
    # set_margin_mode re-raises it, as options.setMarginMode.throwMarginModeAlreadySet defaults to True (~1413)
    return ccxt.MarginModeAlreadySet(_body(-4046, "No need to change margin type."))


def margin_type_position_exists():   # -4048 (common map -> OperationRejected)
    return ccxt.OperationRejected(_body(-4048, "Margin type cannot be changed if there exists position."))


def isolated_under_multi_assets():   # -4168 (linear map -> BadRequest, ccxt binance.py ~2191)
    return ccxt.BadRequest(_body(-4168, "Unable to adjust to isolated-margin mode under the Multi-Assets mode."))


def max_stop_orders():   # -4045 MAX_STOP_ORDER_EXCEEDED (error-code page; ccxt common map -> OperationRejected)
    return ccxt.OperationRejected(_body(-4045, "Reach max stop order limit."))


def duplicate_client_id():
    # -4116 DUPLICATED_CLIENT_ORDER_ID (linear map -> InvalidOrder); New-Order doc: the id is unique among OPEN orders.
    # Inferred: the Algo endpoint's duplicate-clientAlgoId answer is undocumented, so the fake uses -4116 for both
    return ccxt.InvalidOrder(_body(-4116, "clientOrderId is duplicated."))


def switch_algo():   # -4120 STOP_ORDER_SWITCH_ALGO (not in ccxt's maps -> ExchangeError); change log 2025-11-06
    return ccxt.ExchangeError(_body(-4120, "Order type not supported for this endpoint. Please use the Algo Order "
                                           "API endpoints instead."))


def one_close_position_stop():
    # -4130 (not in ccxt's maps -> ExchangeError). No longer on the error-code page; SECONDARY source: testnet probes
    # 2026-09-24 (github.com/bcup02/trade-alerts PR #65) still see a second same-side closePosition stop refused
    return ccxt.ExchangeError(_body(-4130, "An open stop or take profit order with GTE and closePosition in the "
                                           "direction is existing."))


def min_notional(minimum: float):   # -4164 MIN_NOTIONAL (linear map -> InvalidOrder); reduce-only orders are exempt
    return ccxt.InvalidOrder(_body(-4164, f"Order's notional must be no smaller than {minimum} (unless you choose "
                                          "reduce only)."))


def gtd_too_soon():   # -5040 (linear map -> BadRequest); New-Order doc: goodTillDate > now + 600 s
    return ccxt.BadRequest(_body(-5040, "The goodTillDate timestamp must be greater than the current time plus 600 "
                                        "seconds and smaller than 253402300799000"))


def status_unknown():
    # -1007 (common map -> RequestTimeout): "Send status unknown; execution status unknown" - the order may have run
    return ccxt.RequestTimeout(_body(-1007, "Timeout waiting for response from backend server. Send status unknown; "
                                            "execution status unknown."))


def unavailable(path: str, executed: bool):
    """HTTP 503 (general-info doc). 'Unknown error...' = execution status UNKNOWN (it may have run); 'Service
    Unavailable.' = a failure. ccxt: a body without "code" falls through handle_errors to httpExceptions['503'] ->
    ExchangeNotAvailable, message `id method url status reason body`. Inferred: the real body's shape is
    undocumented; one that carries a code would map by that code instead."""
    msg = "Unknown error, please check your request or try again later." if executed else "Service Unavailable."
    return ccxt.ExchangeNotAvailable(f'binanceusdm POST {DEMO_FAPI}/{path} 503 Service Unavailable {{"msg":"{msg}"}}')


def no_api_key():   # ccxt check_required_credentials() on the keyless prod client
    return ccxt.AuthenticationError('binanceusdm requires "apiKey" credential')


def invalid_symbol():   # -1121 BAD_SYMBOL (common map -> BadSymbol, ccxt binance.py ~2702): not listed on the account
    return ccxt.BadSymbol(_body(-1121, "Invalid symbol."))
