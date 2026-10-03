"""Client order ids (clientid.py): Binance's format, and which send errors leave the outcome unknown."""

import asyncio

import ccxt
import pytest

from shortbot import clientid, errors


def test_ids_fit_binance_format_and_are_unique_per_intent():
    a, b = clientid.new("entry", "8166c604"), clientid.new("entry", "8166c604")
    assert a != b and clientid.ours(a) and all(clientid.VALID.match(x) and len(x) <= 36 for x in (a, b))
    assert len(clientid.new("prearm", 10 ** 12)) <= 36
    with pytest.raises(ValueError):
        clientid.new("entry", "x" * 30)                 # > 36 chars: Binance would answer -4015
    assert not clientid.ours("x-xcKtGhcu3kd9") and not clientid.ours("web_1") and not clientid.ours(None)


@pytest.mark.parametrize("err, unknown", [
    (ccxt.RequestTimeout("-1007 execution status unknown"), True), (ccxt.ExchangeNotAvailable("503"), True),
    (ccxt.OperationFailed("-1006 / -1000"), True), (ccxt.BadResponse("garbled"), True),
    (ccxt.NetworkError("reset"), True),
    (ccxt.InvalidNonce("-1021"), True), (ccxt.RateLimitExceeded("429"), True), (ccxt.ExchangeError("unmapped"), True),
    (asyncio.TimeoutError(), True), (ccxt.InvalidOrder("-4164 min notional"), False),
    (ccxt.InsufficientFunds("-2019 margin"), False), (ccxt.OrderImmediatelyFillable("-2021"), False),
    (ccxt.BadRequest("-1102"), False)])
def test_only_a_definite_refusal_counts_as_not_placed(err, unknown):
    assert errors.classify(err).unknown is unknown


async def test_lookup_returns_none_only_when_binance_does_not_know_the_id():
    class T:
        def __init__(self, err=None):
            self.err, self.seen = err, None

        async def fetch_order(self, i, s, params=None):
            self.seen = (i, s, params)
            if self.err:
                raise self.err
            return {"id": "9", "status": "open"}
    t = T()
    assert (await clientid.lookup(t, "X/USDT:USDT", "sb-e1-ab"))["id"] == "9"
    assert t.seen == (None, "X/USDT:USDT", {"clientOrderId": "sb-e1-ab"})
    assert await clientid.lookup(T(ccxt.OrderNotFound("-2013")), "X/USDT:USDT", "sb-e1-ab") is None
    with pytest.raises(ccxt.RequestTimeout):            # can't tell: the caller keeps it unknown
        await clientid.lookup(T(ccxt.RequestTimeout("-1007")), "X/USDT:USDT", "sb-e1-ab")
