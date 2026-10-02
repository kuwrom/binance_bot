"""Trade-client markets (#17): refreshed every TRADE_MARKETS_TTL and at once for a symbol the client doesn't know, so
a perp listed after the process started gets its stop; and one symbol whose stop read fails never fails the others."""

import asyncio
import time
from types import SimpleNamespace

import ccxt
import pytest

from shortbot import exchange
from shortbot.config import RiskLimits, Settings
from shortbot.exchange import Exchanges
from shortbot.guardian import Guardian
from shortbot.journal import Journal
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks

NEW = "NEW/USDT:USDT"


def real_ex(fake: FakeCcxt) -> Exchanges:
    ex = Exchanges(Settings("demo", "", "", ":memory:", "", "", RiskLimits()))
    ex.trade, ex.public = fake, fake.prod
    return ex


async def started() -> tuple[FakeCcxt, Exchanges]:
    """A long-running process that loaded its markets; then a perp is listed on the exchange."""
    fake = FakeCcxt()
    fake.markets = None                                          # a fresh ccxt client: markets is None until loaded
    ex = real_ex(fake)
    await ex.trade_markets()
    ex._trade_tried_at -= 600                                    # ... ten minutes ago
    fake.add_symbol(NEW, 2.0)
    return fake, ex


def guardian(ex, t: Trader | None = None) -> Guardian:
    t = t or Trader(ex, Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
    g = Guardian(SimpleNamespace(settings=ex.settings, journal=t.j, ex=ex, trader=t))
    g._last_resync = time.time()   # the fake has no time endpoint
    return g


async def test_the_ttl_refresh_sees_a_perp_listed_since():
    fake, ex = await started()
    await ex.trade_markets()
    assert NEW not in fake.markets and fake.market_loads == 1    # inside the TTL: the cached copy
    ex._trade_next_load = 0                                      # TTL passed
    await ex.trade_markets()
    assert NEW in fake.markets and fake.market_loads == 2


async def test_an_unknown_symbol_reloads_at_once_but_at_most_once_a_minute():
    fake, ex = await started()
    await ex.trade_markets(NEW)
    assert NEW in fake.markets and fake.market_loads == 2
    for s in ("TYPO", "TYPOUSDT", "TYPO/USDT:USDT"):
        await ex.trade_markets(s)                                # still unknown after a reload: no hammering
    assert fake.market_loads == 2
    ex._trade_tried_at -= exchange.FORCED_RELOAD_MIN_S + 1
    await ex.trade_markets("TYPO")
    assert fake.market_loads == 3
    await ex.trade_markets(SYM)                                  # known: nothing to do
    assert fake.market_loads == 3


async def test_concurrent_callers_share_one_reload():
    fake, ex = await started()
    real = fake.load_markets

    async def slow(reload=False, params=None):
        await asyncio.sleep(0.01)
        return await real(reload, params)
    fake.load_markets = slow
    await asyncio.gather(*(ex.trade_markets(NEW) for _ in range(25)))
    assert fake.market_loads == 2


async def test_a_failed_refresh_keeps_the_cache_and_backs_off():
    fake, ex = await started()
    calls = []

    async def down(reload=False, params=None):
        calls.append(reload)
        raise ccxt.RequestTimeout("exchangeInfo timed out")
    fake.load_markets = down
    ex._trade_next_load = 0
    await ex.trade_markets()                                     # no raise: the cached markets stay
    await ex.trade_markets()                                     # backing off: not tried on every call
    assert calls == [True] and SYM in fake.markets
    assert ex._trade_next_load >= time.time() + exchange.RELOAD_RETRY_S - 5


async def test_the_first_load_still_retries_then_raises(monkeypatch):
    fake = FakeCcxt()
    fake.markets = None                                          # nothing loaded yet (a fresh ccxt client)
    ex = real_ex(fake)

    async def down(reload=False, params=None):
        raise ccxt.RequestTimeout("exchangeInfo timed out")
    fake.load_markets = down

    async def instant(_s):
        return None
    monkeypatch.setattr(exchange, "_sleep", instant)
    with pytest.raises(ccxt.RequestTimeout):
        await ex.trade_markets()


async def test_a_perp_listed_after_start_gets_its_stop():
    fake, ex = await started()
    fake.sym[NEW].update(qty=50.0, entry=2.0)                    # shorted by a fresher process (a restarted MCP)
    t = Trader(ex, Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
    held = await t.positions()
    assert [p["symbol"] for p in held] == [NEW] and held[0]["UNPROTECTED"]
    r = await t.ensure_stop(NEW, 10)
    assert r["action"] == "stop_placed" and r["stop"] < fake.liq_price(NEW)
    assert [fake.algo[i]["symbol"] for i in fake.stops] == [NEW]


async def test_the_guardian_pass_protects_a_perp_listed_after_start():
    fake, ex = await started()
    await fake.create_order(SYM, "market", "sell", 100)          # an older position, already known
    fake.sym[NEW].update(qty=50.0, entry=2.0)
    g = guardian(ex)
    await g.check_positions()
    assert sorted(fake.algo[i]["symbol"] for i in fake.stops) == [NEW, SYM]
    assert "position_check_error" not in [a["kind"] for a in g.j.alerts(100)]


async def test_one_symbols_failed_stop_read_never_fails_the_others():
    fake = FakeCcxt()
    fake.add_symbol(NEW, 2.0)
    for s in (SYM, NEW):
        await fake.create_order(s, "market", "sell", 50)
    real = fake.fetch_open_orders

    async def broken(s, params=None):
        if s == NEW:
            raise ccxt.ExchangeNotAvailable("binanceusdm 503 Service Unavailable")
        return await real(s, params)
    fake.fetch_open_orders = broken
    ex = FakeEx(fake)
    t = Trader(ex, Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
    rows = {p["symbol"]: p for p in await t.positions()}
    assert rows[SYM]["UNPROTECTED"] is True and "error" not in rows[SYM]
    assert rows[NEW]["UNPROTECTED"] == "unknown" and "503" in rows[NEW]["error"]   # never "protected"
    g = guardian(ex, t)
    await g.check_positions()
    assert [fake.algo[i]["symbol"] for i in fake.stops] == [SYM]          # the other symbol still got its stop
    assert ("position_check_error", NEW) in [(a["kind"], a["symbol"]) for a in g.j.alerts(100)]


async def test_a_fresh_process_asking_for_a_symbol_loads_markets_first():
    """A one-shot CLI (move-stop, close) asks for a symbol before anything loaded: ccxt's `markets` is None then.
    Seen live 2026-10-02 (demo drill #65): `in None` raised TypeError before any order was sent."""
    fake = FakeCcxt()
    fake.markets = None
    ex = real_ex(fake)
    await ex.trade_markets(SYM)
    assert fake.markets and SYM in fake.markets
