"""The fake's reduce-only LIMIT rules, which resting plan targets (#65) depend on (sources in tests/fake_reduce.py)."""

import ccxt
import pytest

from tests.fakes import SYM, FakeCcxt

RO = {"reduceOnly": True, "timeInForce": "GTC"}


async def short(fake, qty=100):
    return await fake.create_order(SYM, "market", "sell", qty)


async def target(fake, qty, price, cid=None):
    return await fake.create_order(SYM, "limit", "buy", qty, price, {**RO, **({"clientOrderId": cid} if cid else {})})


async def test_a_reduce_only_buy_limit_rests_below_and_fills_at_its_price_on_the_way_down():
    fake = FakeCcxt()
    await short(fake)
    o = await target(fake, 40, 0.9, "sb-t1-aa")
    assert o["status"] == "open" and o["filled"] == 0 and o["reduceOnly"]
    fake.price = 0.95
    assert (await fake.fetch_order(o["id"], SYM))["filled"] == 0
    fake.price = 0.85                                          # through it: filled at its own price
    got = await fake.fetch_order(o["id"], SYM)
    assert got["status"] == "closed" and got["filled"] == 40 and got["average"] == pytest.approx(0.9)
    assert fake.position == 60 and fake.fills[-1]["price"] == 0.9


async def test_a_marketable_buy_limit_takes_at_once_at_the_market():
    fake = FakeCcxt()
    await short(fake)
    fake.price = 0.88
    o = await target(fake, 40, 0.9)
    assert o["status"] == "closed" and o["average"] == pytest.approx(0.88) and fake.position == 60


async def test_reduce_only_competition_cancels_the_lower_priority_order():
    """FAQ "Summary of Failed Orders", mirrored for a short: 100 short, reduce-only BUY 50 @ 0.90 (A), then 60 @ 0.91
    (B, the higher price is the more aggressive): A goes."""
    fake = FakeCcxt()
    await short(fake)
    a = await target(fake, 50, 0.90)
    b = await target(fake, 60, 0.91)
    assert (await fake.fetch_order(a["id"], SYM))["status"] == "expired"
    assert [o["id"] for o in await fake.fetch_open_orders(SYM)] == [b["id"]]
    with pytest.raises(ccxt.InvalidOrder, match="-2022"):     # a lower-priority newcomer that doesn't fit
        await target(fake, 50, 0.89)


async def test_orders_that_fit_the_position_never_compete_and_a_partial_market_close_wins():
    fake = FakeCcxt()
    await short(fake)
    a, b = await target(fake, 50, 0.95), await target(fake, 50, 0.90)
    assert len(await fake.fetch_open_orders(SYM)) == 2         # 100 resting on 100: no competition
    await fake.create_order(SYM, "market", "buy", 30, None, {"reduceOnly": True})
    assert fake.position == 70
    assert (await fake.fetch_order(b["id"], SYM))["status"] == "expired"   # the farthest one gave way
    assert (await fake.fetch_order(a["id"], SYM))["status"] == "open"


async def test_reduce_only_limits_expire_when_the_position_closes():
    fake = FakeCcxt()
    await short(fake)
    a = await target(fake, 50, 0.9)
    await fake.create_order(SYM, "market", "buy", 100, None, {"reduceOnly": True})
    got = await fake.fetch_order(a["id"], SYM)
    assert fake.position == 0 and got["status"] == "expired" and got["filled"] == 0


async def test_left_resting_at_flat_when_told_and_never_fills_without_a_position():
    fake = FakeCcxt()
    fake.ro_expire_on_flat = False                             # the open question: the desk cancels either way
    await short(fake)
    a = await target(fake, 50, 0.9)
    fake.trigger_stop()                                        # the closePosition path: no order of ours
    assert fake.position == 0
    assert (await fake.fetch_order(a["id"], SYM))["status"] == "open"
    fake.price = 0.8
    got = await fake.fetch_order(a["id"], SYM)
    assert got["status"] == "expired" and got["filled"] == 0 and fake.position == 0


async def test_a_reduce_only_fill_is_capped_at_the_position():
    fake = FakeCcxt()
    await short(fake)
    a = await target(fake, 100, 0.9)
    await fake.create_order(SYM, "market", "buy", 40, None, {"reduceOnly": True})   # 100 + 40 > 100: a goes
    assert (await fake.fetch_order(a["id"], SYM))["status"] == "expired"
    b = await target(fake, 60, 0.9)
    fake.sym[SYM]["qty"] = 25                                  # shrunk by something that is not an order (ADL)
    fake.price = 0.85
    got = await fake.fetch_order(b["id"], SYM)
    assert got["filled"] == 25 and got["status"] == "expired" and fake.position == 0


async def test_amount_to_precision_raises_on_zero_like_ccxt():
    """ccxt 4.5.84 base/exchange.py amount_to_precision: a result of '0' raises InvalidOrder."""
    fake = FakeCcxt()
    assert fake.amount_to_precision(SYM, 0.15) == "0.1"
    with pytest.raises(ccxt.InvalidOrder, match="minimum amount precision"):
        fake.amount_to_precision(SYM, 0.05)
    with pytest.raises(ccxt.InvalidOrder):
        fake.amount_to_precision(SYM, 0.0)
