"""The fake itself: each Binance behaviour the desk's failure paths depend on (sources in fake_errors.py / fakes.py)."""

import time

import ccxt
import pytest

from tests.fakes import SYM, FakeCcxt, FakeEx

SYM2 = "MOVR/USDT:USDT"
GTD = {"timeInForce": "GTD"}
TRIG = {"trigger": True}


def gtd(hours=6):
    return {**GTD, "goodTillDate": int((time.time() + hours * 3600) * 1000)}


async def short(fake, qty=100, s=SYM):
    return await fake.create_order(s, "market", "sell", qty)


async def stop(fake, trigger, s=SYM, qty=None):
    params = {"triggerPrice": trigger, "workingType": "MARK_PRICE"}
    params.update({"reduceOnly": True} if qty else {"closePosition": True})
    return await fake.create_order(s, "STOP_MARKET", "buy", qty, None, params)


async def test_reduce_only_close_needs_a_position_and_never_exceeds_it():
    fake = FakeCcxt()
    with pytest.raises(ccxt.InvalidOrder, match="-2022"):
        await fake.create_order(SYM, "market", "buy", 10, None, {"reduceOnly": True})
    await short(fake)
    o = await fake.create_order(SYM, "market", "buy", 150, None, {"reduceOnly": True})
    assert o["filled"] == 100 and fake.position == 0
    with pytest.raises(AssertionError, match="long"):                      # one-way mode would flip it long
        await fake.create_order(SYM, "market", "buy", 10)


async def test_conditional_orders_live_on_the_algo_service_only():
    fake = FakeCcxt()
    await short(fake)
    sid = (await stop(fake, 1.05))["id"]
    pid = (await stop(fake, 1.10, qty=50))["id"]
    assert await fake.fetch_open_orders(SYM) == []                         # regular open orders: none
    listed = await fake.fetch_open_orders(SYM, params=TRIG)
    assert [o["info"]["closePosition"] for o in listed] == [True, False]
    with pytest.raises(ccxt.OrderNotFound, match="-2011"):                 # regular endpoint, algo id
        await fake.cancel_order(sid, SYM)
    with pytest.raises(ccxt.OrderNotFound, match="-2013"):
        await fake.fetch_order(sid, SYM)
    with pytest.raises(ccxt.OrderNotFound, match="-2013"):                 # untriggered algo orders can't be modified
        await fake.edit_order(pid, SYM, "limit", "buy", 50, 1.2)
    await fake.cancel_order(pid, SYM, params=TRIG)
    with pytest.raises(ccxt.OrderNotFound, match="-2011"):                 # a repeat cancel
        await fake.cancel_order(pid, SYM, params=TRIG)
    assert (await fake.fetch_order(pid, SYM, params=TRIG))["status"] == "canceled"
    assert list(fake.stops) == [sid] and fake.prearms == {}


async def test_one_close_position_stop_per_symbol_and_none_that_would_trigger_at_once():
    fake = FakeCcxt()
    fake.add_symbol(SYM2, 2.0, tick=0.001)
    await short(fake)
    await short(fake, 50, SYM2)
    await stop(fake, 1.05)
    await stop(fake, 2.1, SYM2)                                             # the other symbol has its own
    with pytest.raises(ccxt.ExchangeError, match="-4130"):
        await stop(fake, 1.06)
    with pytest.raises(ccxt.OrderImmediatelyFillable, match="-2021"):
        await stop(fake, 1.0, qty=10)                                        # pre-armed stops too


async def test_a_price_move_fills_the_limit_then_triggers_its_prearmed_stop():
    fake = FakeCcxt()
    lim = await fake.create_order(SYM, "limit", "sell", 100, 1.04, gtd())
    pre = await stop(fake, 1.10, qty=100)
    fake.price = 1.12
    assert [(f["side"], f["price"]) for f in fake.fills] == [("sell", 1.04), ("buy", 1.10)]
    assert fake.position == 0 and fake.limits[lim["id"]]["status"] == "closed"
    assert (await fake.fetch_order(pre["id"], SYM, params=TRIG))["info"]["algoStatus"] == "FINISHED"
    pnl = [float(i["income"]) for i in fake.income if i["incomeType"] == "REALIZED_PNL"]
    assert pnl == [pytest.approx(-6.0)]


async def test_a_triggered_stop_fills_under_its_own_order_id_and_since_is_a_hard_bound():
    fake = FakeCcxt()
    lim = await fake.create_order(SYM, "limit", "sell", 100, 1.04, gtd())
    pre = await stop(fake, 1.10, qty=100)
    fake.price = 1.12
    actual = (await fake.fetch_order(pre["id"], SYM, params=TRIG))["info"]["actualOrderId"]
    assert actual and actual != pre["id"]                                  # userTrades never carry the algo id
    trades = await fake.fetch_my_trades(SYM)
    assert [(x["order"], x["side"], x["price"]) for x in trades] == [(lim["id"], "sell", 1.04), (actual, "buy", 1.10)]
    assert float(trades[1]["info"]["realizedPnl"]) == pytest.approx(-6.0)
    o = await fake.fetch_order(actual, SYM)
    assert o["status"] == "closed" and o["reduceOnly"] and o["lastTradeTimestamp"] == trades[1]["timestamp"]
    assert (await fake.fetch_order(lim["id"], SYM))["lastTradeTimestamp"] == trades[0]["timestamp"]
    assert await fake.fetch_my_trades(SYM, since=trades[1]["timestamp"] + 1) == []
    assert await fake.fapiPrivateGetIncome({"startTime": trades[1]["timestamp"] + 1}) == []


async def test_user_trades_answer_7_day_windows_as_ccxt_asks_for_them():
    fake = FakeCcxt()
    fake.clock_s = -9 * 86400                                              # a short opened 9 days ago
    await short(fake)
    fake.advance(9 * 86400)
    await fake.create_order(SYM, "market", "buy", 100, None, {"reduceOnly": True})
    sell, buy = fake.fills
    assert await fake.fetch_my_trades(SYM, since=sell["timestamp"]) == [sell]   # ccxt caps endTime at since + 7 d
    with pytest.raises(ccxt.BadRequest, match="-1127"):                    # more than 7 days apart
        await fake.fetch_my_trades(SYM, since=sell["timestamp"], params={"until": buy["timestamp"]})
    assert await fake.fetch_my_trades(SYM) == [buy]                        # neither bound: the last 7 days
    assert await fake.fetch_my_trades(SYM, since=buy["timestamp"] - 1, params={"until": buy["timestamp"] - 1}) == []
    fake.fills += [{**buy, "id": str(i)} for i in range(3, 1203)]
    assert len(await fake.fetch_my_trades(SYM, since=buy["timestamp"])) == 500        # default limit
    assert len(await fake.fetch_my_trades(SYM, since=buy["timestamp"], limit=5000)) == 1000


async def test_a_prearmed_stop_without_a_position_is_rejected_when_it_triggers():
    fake = FakeCcxt()
    pre = await stop(fake, 1.10, qty=100)                                    # accepted: no check before trigger
    fake.price = 1.2
    assert fake.fills == [] and fake.prearms == {}
    assert (await fake.fetch_order(pre["id"], SYM, params=TRIG))["status"] == "rejected"


async def test_close_position_stop_closes_the_whole_position_and_goes_with_it():
    fake = FakeCcxt()
    await short(fake, 60)
    await short(fake, 40)
    await stop(fake, 1.05)
    fake.price = 1.08
    assert fake.position == 0 and fake.fills[-1]["amount"] == 100 and fake.fills[-1]["price"] == 1.05
    await short(fake)
    sid = (await stop(fake, 1.2))["id"]
    await fake.create_order(SYM, "market", "buy", 100, None, {"reduceOnly": True})
    assert fake.stops == {}                                                  # flat: the closePosition stop is gone
    assert (await fake.fetch_order(sid, SYM, params=TRIG))["status"] == "expired"


async def test_a_marketable_gtd_limit_takes_at_once():
    fake = FakeCcxt()
    o = await fake.create_order(SYM, "limit", "sell", 100, 0.99, gtd())
    assert o["status"] == "closed" and fake.position == 100 and fake.fills[0]["price"] == 1.0


async def test_gtd_needs_ten_minutes_and_expires_on_the_exchange():
    fake = FakeCcxt()
    with pytest.raises(ccxt.BadRequest, match="-5040"):
        await fake.create_order(SYM, "limit", "sell", 100, 1.04, gtd(hours=0.1))
    o = await fake.create_order(SYM, "limit", "sell", 100, 1.04, gtd(hours=1))
    fake.fill_limit(o["id"], qty=30)
    fake.advance(3500)
    assert (await fake.fetch_order(o["id"], SYM))["status"] == "open"
    fake.advance(200)
    got = await fake.fetch_order(o["id"], SYM)
    assert got["status"] == "expired" and got["filled"] == 30
    with pytest.raises(ccxt.OrderNotFound, match="-2011"):
        await fake.cancel_order(o["id"], SYM)
    fake.price = 1.1                                                         # an expired order never fills
    assert fake.position == 30


async def test_binance_forgets_dead_unfilled_orders_three_days_after_creation():
    fake = FakeCcxt()
    dead = await fake.create_order(SYM, "limit", "sell", 100, 1.04, gtd())
    await fake.cancel_order(dead["id"], SYM)
    part = await fake.create_order(SYM, "limit", "sell", 100, 1.05, gtd(hours=1))
    fake.fill_limit(part["id"], qty=30)
    pre = await stop(fake, 1.2, qty=100)
    await fake.cancel_order(pre["id"], SYM, params=TRIG)
    fake.advance(3 * 86400 - 60)
    assert (await fake.fetch_order(dead["id"], SYM))["status"] == "canceled"
    fake.advance(120)                                                        # created + 3 days < now
    with pytest.raises(ccxt.OrderNotFound, match="-2013"):
        await fake.fetch_order(dead["id"], SYM)
    with pytest.raises(ccxt.OrderNotFound, match="-2013"):
        await fake.fetch_order(pre["id"], SYM, params=TRIG)
    got = await fake.fetch_order(part["id"], SYM)                            # a fill keeps it (90 days)
    assert got["status"] == "expired" and got["filled"] == 30


async def test_last_trade_timestamp_only_for_closed_or_partly_filled_open_orders():
    fake = FakeCcxt()
    o = await fake.create_order(SYM, "limit", "sell", 100, 1.04, gtd(hours=1))
    assert (await fake.fetch_order(o["id"], SYM))["lastTradeTimestamp"] is None
    fake.fill_limit(o["id"], qty=30)
    assert (await fake.fetch_order(o["id"], SYM))["lastTradeTimestamp"] == fake.fills[-1]["timestamp"]
    fake.advance(3700)
    got = await fake.fetch_order(o["id"], SYM)
    assert got["status"] == "expired" and got["filled"] == 30 and got["lastTradeTimestamp"] is None


async def test_placement_rejections_tick_min_notional_and_margin():
    fake = FakeCcxt(equity=100)
    with pytest.raises(ccxt.BadRequest, match="-4014"):
        await fake.create_order(SYM, "limit", "sell", 100, 1.04005, gtd())
    with pytest.raises(ccxt.InvalidOrder, match="-4164"):
        await fake.create_order(SYM, "limit", "sell", 4, 1.04, gtd())
    await fake.set_leverage(3, SYM)
    await fake.create_order(SYM, "limit", "sell", 200, 1.04, gtd())         # 208 / 3 = 69 of 100 margin
    with pytest.raises(ccxt.InsufficientFunds, match="-2019"):
        await short(fake, 100)                                               # 100 / 3 = 33 more: too much
    await stop(fake, 1.2, qty=500)                                           # conditional orders hold no margin
    await fake.create_order(SYM, "market", "sell", 50)


async def test_client_ids_are_unique_among_open_orders_only_so_a_resend_runs_twice():
    fake = FakeCcxt()
    await fake.create_order(SYM, "limit", "sell", 100, 1.04, {**gtd(), "newClientOrderId": "a"})
    with pytest.raises(ccxt.InvalidOrder, match="-4116"):
        await fake.create_order(SYM, "limit", "sell", 100, 1.05, {**gtd(), "newClientOrderId": "a"})
    for _ in range(2):                                                       # the first already filled: not open
        await fake.create_order(SYM, "market", "sell", 50, None, {"newClientOrderId": "m"})
    assert fake.position == 100
    got = await fake.fetch_order(None, SYM, params={"clientOrderId": "m"})
    assert got["status"] == "closed" and got["filled"] == 50


async def test_answers_lost_after_the_order_ran():
    fake = FakeCcxt()
    fake.ack_lost = 1
    with pytest.raises(ccxt.RequestTimeout, match="-1007"):
        await short(fake)
    assert fake.position == 100                                             # it ran anyway
    fake.ack_lost_503 = 1
    with pytest.raises(ccxt.ExchangeNotAvailable, match="Unknown error"):
        await stop(fake, 1.05)
    assert list(fake.stops.values()) == [1.05]
    fake.unavailable_503 = 1
    with pytest.raises(ccxt.ExchangeNotAvailable, match="Service Unavailable."):
        await short(fake)
    assert fake.position == 100                                             # this one did not run
    fake.ack_lost_unsent = 1
    with pytest.raises(ccxt.RequestTimeout, match="-1007"):
        await short(fake)
    assert fake.position == 100                                             # "unknown" can also mean: not run
    assert issubclass(ccxt.RequestTimeout, ccxt.NetworkError) and issubclass(ccxt.ExchangeNotAvailable,
                                                                             ccxt.NetworkError)


async def test_prod_and_demo_prices_diverge_and_prod_has_no_keys():
    fake = FakeCcxt()
    ex = FakeEx(fake)
    fake.prod.price = 1.2
    assert (await ex.public.fetch_ticker(SYM))["last"] == 1.2 and (await ex.trade.fetch_ticker(SYM))["last"] == 1.0
    assert (await ex.public.fetch_ohlcv(SYM))[-1][4] == 1.2
    with pytest.raises(ccxt.AuthenticationError):
        await ex.public.create_order(SYM, "market", "sell", 10)
    assert fake.orders == []

    async def candles(s, tf="1h", since=None, limit=30):
        return [[0, 9, 9, 9, 9, 1]]
    fake.fetch_ohlcv = candles
    fake.prod.prices.clear()
    assert await ex.public.fetch_ohlcv(SYM) == [[0, 9, 9, 9, 9, 1]]       # a test's stub on the fake still applies


async def test_symbols_keep_separate_books():
    fake = FakeCcxt()
    fake.add_symbol(SYM2, 2.0, tick=0.001)
    await short(fake)
    await short(fake, 50, SYM2)
    await stop(fake, 1.05)
    await stop(fake, 2.1, SYM2)
    assert {p["symbol"]: p["contracts"] for p in await fake.fetch_positions()} == {SYM: 100, SYM2: 50}
    assert [f["amount"] for f in await fake.fetch_my_trades(SYM2)] == [50]
    assert {i["symbol"] for i in await fake.fapiPrivateGetIncome({"symbol": "MOVRUSDT"})} == {"MOVRUSDT"}
    assert await fake.fapiPrivateGetIncome({"symbol": "MOVRUSDT", "incomeType": "FUNDING_FEE"}) == []
    assert {i["incomeType"] for i in await fake.fapiPrivateGetIncome({"incomeType": "COMMISSION"})} == {"COMMISSION"}
    await fake.cancel_all_orders(SYM, params=TRIG)
    assert list(fake.stops.values()) == [2.1]
    fake.set_price(SYM2, 2.2)
    assert fake.sym[SYM2]["qty"] == 0 and fake.position == 100
    assert fake.price_to_precision(SYM2, 2.0806) == "2.081" and fake.amount_to_precision(SYM2, 1.99) == "1.9"


async def test_margin_mode_answers_like_ccxt():
    fake = FakeCcxt()
    fake.add_symbol(SYM2, 2.0)
    with pytest.raises(ccxt.MarginModeAlreadySet, match="-4046"):            # ccxt 4.5.84 re-raises "no need"
        await fake.set_margin_mode("isolated", SYM)
    await fake.create_order(SYM2, "limit", "sell", 10, 2.1, gtd())
    assert (await fake.set_margin_mode("cross", SYM))["code"] == 200        # another symbol's order: no effect
    with pytest.raises(ccxt.OperationRejected, match="-4067"):
        await fake.set_margin_mode("isolated", SYM2)
    with pytest.raises(ccxt.BadSymbol):
        fake.market("NOPE/USDT:USDT")
    with pytest.raises(ccxt.ExchangeError, match="-4120"):                  # a stop without a trigger price
        await fake.create_order(SYM, "STOP_MARKET", "buy", None, None, {"closePosition": True})


async def test_markets_load_once_and_a_perp_listed_since_needs_a_reload():
    fake, new = FakeCcxt(), "NEW/USDT:USDT"
    await fake.load_markets()
    fake.add_symbol(new, 2.0)
    fake.sym[new].update(qty=10.0, entry=2.0)    # a position opened on it (by another, fresher client)
    await fake.load_markets()                    # no reload: ccxt returns the snapshot
    with pytest.raises(ccxt.BadSymbol):
        fake.price_to_precision(new, 2.2)
    with pytest.raises(ccxt.BadSymbol):
        await fake.fetch_open_orders(new, params=TRIG)
    assert [p["symbol"] for p in await fake.fetch_positions()] == ["NEWUSDT"]   # safe_market: the raw id
    await fake.load_markets(reload=True)
    assert [p["symbol"] for p in await fake.fetch_positions()] == [new] and fake.market_loads == 2
    assert await fake.fetch_open_orders(new, params=TRIG) == []


async def test_positions_carry_no_leverage_and_binance_isolated_liquidation_price():
    fake = FakeCcxt(price=1.0)
    await fake.set_leverage(10, SYM)
    await short(fake)
    p = (await fake.fetch_positions())[0]
    assert p["leverage"] is None                                         # v3 positionRisk
    assert p["liquidationPrice"] == pytest.approx(1.1 / 1.01)           # (N/10 + 0 + N) / (Q * 1.01)


async def test_leverage_brackets_answer_like_the_doc():
    fake = FakeCcxt()
    rows = await fake.fapiPrivateGetLeverageBracket({"symbol": "PUMPUSDT"})
    assert rows[0]["symbol"] == "PUMPUSDT" and rows[0]["brackets"][0]["maintMarginRatio"] == 0.01
    with pytest.raises(ccxt.BadSymbol, match="-1121"):
        await fake.fapiPrivateGetLeverageBracket({"symbol": "NOPEUSDT"})
    with pytest.raises(ccxt.AuthenticationError):                      # signed: never on the keyless prod client
        await fake.prod.fapiPrivateGetLeverageBracket({"symbol": "PUMPUSDT"})


async def test_account_side_answers_like_binance_and_ccxt():
    fake = FakeCcxt()
    fake.add_symbol(SYM2, 2.0)
    assert await fake.fapiPrivateGetAdlQuantile({}) == []                   # flat: demo answers [] (2026-10-02)
    fake.position, fake.entry, fake.sym[SYM]["adl"] = 100, 1.0, 3
    (p,) = await fake.fetch_positions()
    info = p["info"]
    assert p["marginMode"] == "isolated" and p["notional"] == 100 and p["leverage"] is None
    assert float(info["isolatedMargin"]) == float(info["isolatedWallet"]) + float(info["unRealizedProfit"])
    assert float(info["maintMargin"]) == 100 * 0.01 and info["marginAsset"] == "USDT" and info["notional"] == "-100.0"
    assert await fake.fapiPrivateGetAdlQuantile({}) == [{"symbol": "PUMPUSDT",
                                                          "adlQuantile": {"LONG": 0, "SHORT": 0, "BOTH": 3}}]
    assert (await fake.fapiPrivateGetAdlQuantile({"symbol": "PUMPUSDT"}))["adlQuantile"]["BOTH"] == 3   # one object
    with pytest.raises(ccxt.OperationRejected, match="-4048"):               # a position exists
        await fake.set_margin_mode("cross", SYM)
    fake.sym[SYM]["margin"] = "CROSSED"
    (p,) = await fake.fetch_positions()
    assert p["marginMode"] == "cross" and p["info"]["isolatedMargin"] == "0"
    assert [r["marginType"] for r in await fake.fapiPrivateGetSymbolConfig({"symbol": "PUMPUSDT"})] == ["CROSSED"]
    fake.multi_assets = True
    assert await fake.fapiPrivateGetMultiAssetsMargin({}) == {"multiAssetsMargin": True}
    with pytest.raises(ccxt.BadRequest, match="-4168"):
        await fake.set_margin_mode("isolated", SYM2)
    fake.sym[SYM]["margin"] = "ISOLATED"
    i = fake.force_close(SYM, "ADL")
    (o,) = await fake.fapiPrivateGetForceOrders({"symbol": "PUMPUSDT", "autoCloseType": "ADL"})
    assert fake.position == 0 and o["orderId"] == int(i) and o["clientOrderId"] == "adl_autoclose"
    assert await fake.fapiPrivateGetForceOrders({"autoCloseType": "LIQUIDATION"}) == []
