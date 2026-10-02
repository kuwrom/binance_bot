"""The fake against what Binance demo answered to `shortbot probe --all` on 2026-10-02 (#55, #75). Each test cites the
journal's `probe_result` decision row (data/shortbot.db, `decisions.id`) whose raw answers it mirrors."""

import re
import time
from pathlib import Path

import ccxt
import pytest

from tests.fakes import SYM, FakeCcxt

SRC = Path(__file__).resolve().parents[1] / "src" / "shortbot"
TRIG = {"trigger": True}
RO = {"reduceOnly": True, "timeInForce": "GTC"}


async def short(fake, qty=100):
    return await fake.create_order(SYM, "market", "sell", qty)


async def stop(fake, trigger, qty=None):
    params = {"triggerPrice": trigger, "workingType": "MARK_PRICE"}
    params.update({"reduceOnly": True} if qty else {"closePosition": True})
    return await fake.create_order(SYM, "STOP_MARKET", "buy", qty, None, params)


async def close(fake, qty=100):
    return await fake.create_order(SYM, "market", "buy", qty, None, {"reduceOnly": True})


def pin(fake, frac_ms: int) -> int:
    """The exchange clock at `frac_ms` past a whole second, moved only by `advance`. Returns that start."""
    base = (int(time.time()) - 5) * 1000 + frac_ms
    fake.now_ms = lambda: base + round(fake.clock_s * 1000)
    return base


# ------------------------------------------------------------ 388: countdownCancelAll
async def test_the_countdown_cancels_regular_orders_and_conditional_ones():
    """probe_result 388: the SELL limit AND the reduce-only stop were cancelled (algoStatus CANCELED). A closePosition
    stop can't rest flat (-4509), so the probe couldn't place one: the fake cancels it too (every open order of the
    symbol, as the endpoint's doc says)."""
    fake = FakeCcxt()
    await short(fake, 10)
    lim = await fake.create_order(SYM, "limit", "sell", 50, 1.2, {"timeInForce": "GTC"})
    ro, cps = await stop(fake, 1.3, qty=10), await stop(fake, 1.4)
    await fake.fapiPrivatePostCountdownCancelAll({"symbol": "PUMPUSDT", "countdownTime": 3000})
    fake.advance(4)
    assert fake.limits[lim["id"]]["status"] == "canceled"
    assert await fake.fetch_open_orders(SYM, params=TRIG) == []
    got = await fake.fetch_order(ro["id"], SYM, params=TRIG)
    assert got["status"] == "canceled" and got["info"]["algoStatus"] == "CANCELED"
    assert (await fake.fetch_order(cps["id"], SYM, params=TRIG))["info"]["algoStatus"] == "CANCELED"


def test_no_desk_code_arms_the_countdown():
    """It would strip the protective stops with the orders (388): only the demo probes may call it."""
    allowed = {"probes.py", "probe_cases.py", "probe_kit.py"}
    pat = re.compile(r"countdown_?cancel_?all", re.I)
    users = sorted(str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if pat.search(p.read_text()))
    assert users and set(users) <= allowed, users


# ------------------------------------------------------------ 389: a closePosition stop needs a position
async def test_a_close_position_stop_while_flat_is_refused_4509_and_a_second_one_4130():
    """probe_result 389: flat, a first and a second closePosition stop both answer -4509 (an ExchangeError ccxt doesn't
    map) and none is listed; with a position the second answers -4130."""
    fake = FakeCcxt()
    for _ in range(2):
        with pytest.raises(ccxt.ExchangeError, match="-4509") as e:
            await stop(fake, 1.1)
        assert type(e.value) is ccxt.ExchangeError
    assert await fake.fetch_open_orders(SYM, params=TRIG) == []
    await short(fake)
    await stop(fake, 1.1)
    with pytest.raises(ccxt.ExchangeError, match="-4130"):
        await stop(fake, 1.2)


# ------------------------------------------------------------ 390 / 391: stops at flat
async def test_a_close_position_stop_expires_with_its_position():
    """probe_result 390: gone from the open list within 0.5 s of flat, algoStatus EXPIRED."""
    fake = FakeCcxt()
    await short(fake)
    s = await stop(fake, 1.1)
    await close(fake)
    assert await fake.fetch_open_orders(SYM, params=TRIG) == []
    got = await fake.fetch_order(s["id"], SYM, params=TRIG)
    assert got["status"] == "expired" and got["info"]["algoStatus"] == "EXPIRED"


async def test_a_reduce_only_stop_keeps_resting_at_flat():
    """probe_result 391: the pre-arm shape (reduce-only, its own quantity) is still NEW 15.5 s after flat."""
    fake = FakeCcxt()
    await short(fake)
    cps, ro = await stop(fake, 1.1), await stop(fake, 1.09, qty=100)
    await close(fake)
    fake.advance(15.5)
    assert [o["id"] for o in await fake.fetch_open_orders(SYM, params=TRIG)] == [ro["id"]]
    assert (await fake.fetch_order(ro["id"], SYM, params=TRIG))["info"]["algoStatus"] == "NEW"
    assert (await fake.fetch_order(cps["id"], SYM, params=TRIG))["info"]["algoStatus"] == "EXPIRED"


# ------------------------------------------------------------ 392: cancelling a finished algo order
async def test_cancelling_a_canceled_or_expired_algo_order_answers_2011():
    """probe_result 392: CANCELED (a repeat cancel) and EXPIRED (a closePosition stop after flat) both -2011."""
    fake = FakeCcxt()
    await short(fake)
    cps, ro = await stop(fake, 1.1), await stop(fake, 1.09, qty=100)
    await fake.cancel_order(ro["id"], SYM, params=TRIG)
    with pytest.raises(ccxt.OrderNotFound, match="-2011"):
        await fake.cancel_order(ro["id"], SYM, params=TRIG)
    await close(fake)
    for _ in range(2):
        with pytest.raises(ccxt.OrderNotFound, match="-2011"):
            await fake.cancel_order(cps["id"], SYM, params=TRIG)


# ------------------------------------------------------------ 393: an oversize reduce-only order is capped
async def test_an_oversize_reduce_only_limit_rests_capped_at_the_position():
    """probe_result 393: a reduce-only LIMIT for 2x the position is accepted with origQty = the position, and rests."""
    fake = FakeCcxt()
    await short(fake)
    o = await fake.create_order(SYM, "limit", "buy", 200, 0.9, RO)
    assert o["status"] == "open" and o["amount"] == 100
    assert (await fake.fetch_order(o["id"], SYM))["amount"] == 100
    (raw,) = await fake.fapiPrivateGetOpenOrders({"symbol": "PUMPUSDT"})
    assert float(raw["origQty"]) == 100 and raw["reduceOnly"] is True


async def test_an_oversize_reduce_only_market_order_closes_just_the_position():
    """probe_result 393: a reduce-only MARKET for 2x fills the position's size (origQty 3.5 of 7) and leaves it flat."""
    fake = FakeCcxt()
    await short(fake)
    o = await close(fake, 200)
    assert o["status"] == "closed" and o["amount"] == 100 and o["filled"] == 100 and fake.position == 0


async def test_a_capped_newcomer_still_competes_as_the_faq_says():
    """The cap is per order; the FAQ's competition among several stays (tests/fake_reduce.py): a capped newcomer at the
    best price cancels what no longer fits, and a lower-priority one that doesn't fit is refused."""
    fake = FakeCcxt()
    await short(fake)
    a = await fake.create_order(SYM, "limit", "buy", 50, 0.90, RO)
    b = await fake.create_order(SYM, "limit", "buy", 150, 0.95, RO)
    assert b["amount"] == 100 and (await fake.fetch_order(a["id"], SYM))["status"] == "expired"
    with pytest.raises(ccxt.InvalidOrder, match="-2022"):
        await fake.create_order(SYM, "limit", "buy", 150, 0.89, RO)


async def test_a_reduce_only_order_while_flat_is_still_2022():
    fake = FakeCcxt()
    for kind, px in (("limit", 0.9), ("market", None)):
        with pytest.raises(ccxt.InvalidOrder, match="-2022"):
            await fake.create_order(SYM, kind, "buy", 10, px, RO)


# ------------------------------------------------------------ 395: income rows
async def test_income_rows_carry_the_trade_id_and_a_time_floored_to_the_second():
    """probe_result 395, first pair: trade 168325228 at 1790954095875, its COMMISSION at 1790954095000. A closing
    trade's REALIZED_PNL (listed first) and COMMISSION share one tranId; `info` repeats the tradeId."""
    fake = FakeCcxt()
    base = pin(fake, 875)
    await short(fake)
    fake.advance(2.5)                                            # the exit at .375 past its second
    fake.price = 0.9
    await close(fake)
    entry, exit_ = fake.fills
    assert (entry["timestamp"], exit_["timestamp"]) == (base, base + 2500)
    rows = await fake.fapiPrivateGetIncome({"symbol": "PUMPUSDT"})
    assert [(r["incomeType"], r["tradeId"], r["time"] - base) for r in rows] == [
        ("COMMISSION", entry["id"], -875), ("REALIZED_PNL", exit_["id"], 2125), ("COMMISSION", exit_["id"], 2125)]
    assert all(r["info"] == r["tradeId"] and isinstance(r["tranId"], int) for r in rows)
    assert rows[1]["tranId"] == rows[2]["tranId"] != rows[0]["tranId"]


async def test_income_reads_start_at_start_time_and_page_by_limit():
    """Get-Income-History doc: startTime/endTime inclusive, oldest first, `limit` 100 by default and 1000 at most. A
    startTime at the fill's own ms misses its row, stamped earlier in the same second (395)."""
    fake = FakeCcxt()
    base = pin(fake, 400)
    await short(fake)
    assert await fake.fapiPrivateGetIncome({"startTime": base}) == []
    assert len(await fake.fapiPrivateGetIncome({"startTime": base - 400})) == 1
    fake.income += [dict(fake.income[0], tranId=i) for i in range(150)]
    assert len(await fake.fapiPrivateGetIncome({})) == 100
    assert len(await fake.fapiPrivateGetIncome({"limit": 5000})) == 151
    assert await fake.fapiPrivateGetIncome({"endTime": base - 401}) == []
