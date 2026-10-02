"""What the running desk does with a probe (#55): the guardian treats a LIVE probe's position as explained (no sticky
reconciliation trip) but still enforces its stop; an expired probe row counts as absent, so a crashed probe's leftover
trips as any untracked position does. The open-orders sweep leaves probe orders alone. Plus the fake endpoints the
probes measure."""

import time

import ccxt
import pytest

from shortbot import entry_intents, probes
from shortbot import killswitch as ks
from shortbot.guardian_positions import UNTRACKED_GRACE_S
from shortbot.journal import Journal
from shortbot.killswitch import ACTIVE, REDUCING
from tests.fakes import SYM, FakeCcxt
from tests.test_entry_intents import desk
from tests.test_killswitch_guardian import guardian


def untracked(fake, stop=True):
    fake.position, fake.entry = 50, 1.0
    if stop:
        fake.stops["s1"] = 1.1
    fake.sym[SYM]["lev"] = 5   # liquidation ~1.19: the 1.1 stop stays inside it (#64)


async def test_a_live_probe_row_explains_the_position_an_expired_one_does_not():
    g, fake = guardian()
    untracked(fake)
    g.j.set(probes.ACTIVE_KEY + SYM, str(time.time() + 60))
    for _ in range(3):
        await g.check_positions()
        assert SYM not in g.untracked_since
    assert ks.read(g.j)["state"] == ACTIVE
    g.j.set(probes.ACTIVE_KEY + SYM, str(time.time() - 1))   # the probe died: its row ran out
    await g.check_positions()
    assert SYM in g.untracked_since
    g.untracked_since[SYM] -= UNTRACKED_GRACE_S
    await g.check_positions()
    st = ks.read(g.j)
    assert st["state"] == REDUCING and st["cause"] == "reconciliation"


async def test_a_live_probe_position_still_gets_a_stop():
    g, fake = guardian()
    untracked(fake, stop=False)
    g.j.set(probes.ACTIVE_KEY + SYM, str(time.time() + 60))
    await g.check_positions()
    assert len(fake.stops) == 1 and ks.read(g.j)["state"] == ACTIVE


def test_active_symbols_ignore_garbage_and_other_modes():
    j = Journal(":memory:")
    j.set(probes.ACTIVE_KEY + "A/USDT:USDT", str(time.time() + 60))
    j.set(probes.ACTIVE_KEY + "B/USDT:USDT", "not a time")
    j.set(probes.ACTIVE_KEY + "C/USDT:USDT", str(time.time() - 5))
    assert probes.active_symbols(j) == {"A/USDT:USDT"}
    live = Journal(":memory:", "live")
    live.set(probes.ACTIVE_KEY + "A/USDT:USDT", str(time.time() + 60))
    assert probes.active_symbols(live) == set()   # probes are demo only: a live guardian never honours a row


async def test_the_open_orders_sweep_leaves_probe_orders_alone():
    d, t, fake = desk()
    await fake.create_order(SYM, "limit", "sell", 50, 1.2, {"timeInForce": "GTC", "clientOrderId": "sb-x9PUMP-1a2b3c4d"})
    await entry_intents.reconcile(d)
    assert d._rows("status='external'") == []


# ------------------------------------------------------------ the fake endpoints the probes measure
async def test_fake_countdown_cancels_regular_orders_once_and_zero_stops_it():
    fake = FakeCcxt()
    lim = await fake.create_order(SYM, "limit", "sell", 50, 1.2, {"timeInForce": "GTC"})
    st = await fake.create_order(SYM, "STOP_MARKET", "buy", 10, None, {"triggerPrice": 1.3, "reduceOnly": True})
    r = await fake.fapiPrivatePostCountdownCancelAll({"symbol": "PUMPUSDT", "countdownTime": 3000})
    assert r == {"symbol": "PUMPUSDT", "countdownTime": "3000"}
    fake.advance(1)
    assert fake.limits[lim["id"]]["status"] == "open"
    fake.advance(3)
    assert fake.limits[lim["id"]]["status"] == "canceled" and st["id"] in fake.prearms   # inferred: algo stays
    lim2 = await fake.create_order(SYM, "limit", "sell", 50, 1.2, {"timeInForce": "GTC"})
    await fake.fapiPrivatePostCountdownCancelAll({"symbol": "PUMPUSDT", "countdownTime": 3000})
    await fake.fapiPrivatePostCountdownCancelAll({"symbol": "PUMPUSDT", "countdownTime": 0})
    fake.advance(10)
    assert fake.limits[lim2["id"]]["status"] == "open"


async def test_fake_raw_algo_query_and_income_trade_ids():
    fake = FakeCcxt()
    o = await fake.create_order(SYM, "STOP_MARKET", "buy", 10, None,
                                {"triggerPrice": 1.3, "reduceOnly": True, "clientOrderId": "sb-x8PUMP-00"})
    q = await fake.fapiPrivateGetAlgoOrder({"clientAlgoId": "sb-x8PUMP-00"})
    assert q["algoId"] == int(o["id"]) and q["algoStatus"] == "NEW" and q["symbol"] == "PUMPUSDT"
    with pytest.raises(ccxt.OrderNotFound, match="-2013"):
        await fake.fapiPrivateGetAlgoOrder({"clientAlgoId": "never-sent"})
    await fake.create_order(SYM, "market", "sell", 100)
    (inc,) = await fake.fapiPrivateGetIncome({"symbol": "PUMPUSDT"})
    assert inc["tradeId"] == fake.fills[-1]["id"] and inc["time"] == fake.fills[-1]["timestamp"]
    mark = await fake.fapiPublicGetPremiumIndex({"symbol": "PUMPUSDT"})
    assert float(mark["markPrice"]) == fake.price
