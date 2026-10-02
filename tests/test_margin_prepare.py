"""#64: an entry never opens on a symbol it can't confirm isolated (Trader._prepare), and a short Binance closed itself
(ADL, liquidation) is journalled as such (reconcile, GET /fapi/v1/forceOrders)."""

import time

import ccxt
import pytest

from shortbot import reconcile
from shortbot.trading import ISOLATED_OK_KEY, ISOLATED_TRUST_S
from tests.fakes import SYM, FakeCcxt
from tests.test_entries import desk, prop
from tests.test_fake_parity import gtd
from tests.test_reconcile import setup, short


def symbol_config_down(fake):
    async def down(req=None):
        raise ccxt.BadRequest('binanceusdm {"code":-1000,"msg":"An unknown error occured while processing."}')
    fake.fapiPrivateGetSymbolConfig = down


async def entry(t):
    return await t.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100)


# ------------------------------------------------------------ _prepare
async def test_a_confirmed_isolated_symbol_is_remembered():
    _, t, fake = desk()
    fake.sym[SYM]["margin"] = "CROSSED"
    await t._prepare(SYM, 3)                    # switched: success
    assert fake.sym[SYM]["margin"] == "ISOLATED" and float(t.j.get(ISOLATED_OK_KEY + SYM)) > time.time() - 5
    t.j.set(ISOLATED_OK_KEY + SYM, "0")
    await t._prepare(SYM, 3)                    # -4046 MarginModeAlreadySet: isolated already
    assert float(t.j.get(ISOLATED_OK_KEY + SYM)) > time.time() - 5


async def test_a_split_entrys_second_leg_proceeds_on_the_first_legs_confirmation_when_symbol_config_is_down():
    d, t, fake = desk()
    symbol_config_down(fake)
    fake.price = 1.035
    r = await d.propose_group([prop(1.04, tranches=[{"limit_price": 1.04, "share": 0.5},
                                                    {"limit_price": 1.07, "share": 0.5}])])
    assert r["ok"] and len(r["placed"]) == 2, r   # leg 2's _prepare met -4067 (leg 1 rests)


async def test_a_crossed_symbol_pinned_by_an_open_order_is_refused_before_any_order():
    _, t, fake = desk()
    fake.sym[SYM]["margin"] = "CROSSED"
    await fake.create_order(SYM, "limit", "sell", 10, 1.2, gtd())   # someone else's order, resting in cross
    before = len(fake.orders)
    with pytest.raises(RuntimeError, match="CROSSED"):
        await t._prepare(SYM, 2)
    with pytest.raises(RuntimeError, match="CROSSED"):   # open_short: refused before its journal row and order
        await entry(t)
    assert fake.position == 0 and len(fake.orders) == before
    assert not t.j.open_trades(include_opening=True)


@pytest.mark.parametrize("seen_s_ago", [None, ISOLATED_TRUST_S + 60])
async def test_unconfirmed_isolation_is_refused_when_symbol_config_is_down(seen_s_ago):
    _, t, fake = desk()
    symbol_config_down(fake)
    if seen_s_ago:
        t.j.set(ISOLATED_OK_KEY + SYM, str(time.time() - seen_s_ago))
    await fake.create_order(SYM, "limit", "sell", 10, 1.2, gtd())
    with pytest.raises(RuntimeError, match="can't be confirmed isolated"):
        await t._prepare(SYM, 2)


async def test_a_readable_isolated_config_lets_the_entry_through():
    _, t, fake = desk()
    await fake.create_order(SYM, "limit", "sell", 10, 1.2, gtd())
    await t._prepare(SYM, 2)                    # -4067, symbolConfig says ISOLATED
    assert float(t.j.get(ISOLATED_OK_KEY + SYM)) > time.time() - 5


async def test_multi_assets_mode_refuses_an_entry_loudly():
    _, t, fake = desk()
    fake.multi_assets, fake.sym[SYM]["margin"] = True, "CROSSED"
    with pytest.raises(ccxt.BadRequest, match="-4168"):
        await t._prepare(SYM, 2)


# ------------------------------------------------------------ exits Binance made itself
@pytest.mark.parametrize("kind, reason", [("ADL", "adl"), ("LIQUIDATION", "liquidation")])
async def test_a_forced_close_is_journalled_as_adl_or_liquidation(monkeypatch, kind, reason):
    t, fake = setup(monkeypatch)
    tid = await short(t)
    fake.advance(60)
    fake.force_close(SYM, kind)
    fake.advance(60)
    (res,) = await reconcile.reconcile(t)
    got = t.j.get_trade(tid)["exit_reason"]
    assert got.startswith(reason) and res["reason"] == got and got.lower().count("liquidat") <= 1


async def test_a_stop_out_keeps_the_generic_reason_and_an_unreadable_force_list_closes_anyway(monkeypatch):
    t, fake = setup(monkeypatch)
    tid = await short(t)
    fake.advance(60)
    fake.trigger_stop()
    fake.advance(60)
    await reconcile.reconcile(t)
    assert t.j.get_trade(tid)["exit_reason"] == reconcile.CLOSED_ON_EXCHANGE
    tid2 = await short(t)
    fake.advance(60)
    fake.force_close(SYM, "ADL")
    fake.advance(60)

    async def down(req=None):
        raise ccxt.NetworkError("forceOrders timed out")
    fake.fapiPrivateGetForceOrders = down
    await reconcile.reconcile(t)
    assert t.j.get_trade(tid2)["status"] == "closed"
    assert t.j.get_trade(tid2)["exit_reason"] == reconcile.CLOSED_ON_EXCHANGE


def test_force_orders_are_matched_by_the_trades_own_exit_orders():
    rows = [{"orderId": 7, "symbol": "PUMPUSDT"}, {"orderId": 9, "symbol": "PUMPUSDT"}]
    assert reconcile.forced_by(rows, {"9"}) and not reconcile.forced_by(rows, {"8"})
    assert not reconcile.forced_by({"code": -1}, {"9"})   # an odd answer is no match
    assert isinstance(FakeCcxt().force_orders, list)
