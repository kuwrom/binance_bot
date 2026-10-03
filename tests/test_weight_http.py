"""The weight guard on real ccxt clients (#70): Exchanges builds guarded binanceusdm clients whose responses feed the
per-host budget and whose requests wait their turn by class. HTTP is faked one layer below ccxt (tests/fake_http.py),
so ccxt's own fetch, retry loop and 418/429 -> DDoSProtection mapping run for real."""

import time
from types import SimpleNamespace

import ccxt
import pytest

from shortbot import weight
from shortbot.config import RiskLimits, Settings
from shortbot.exchange import Exchanges
from shortbot.guardian_loop import Job
from shortbot.weight import DESK, PROTECTIVE, RESEARCH, BudgetWait
from tests.fake_http import FakeHost, FakeSession, fake_clock, wire

KL = {"symbol": "BTCUSDT", "interval": "1h", "limit": 2}


@pytest.fixture
def clock(monkeypatch):
    weight.reset()
    yield fake_clock(monkeypatch, weight)
    weight.reset()


@pytest.fixture
async def demo():
    """Demo mode with keys: `public` reads mainnet fapi, `trade` the demo host (separate budgets today)."""
    main, dm = FakeHost("fapi.binance.com", 2400), FakeHost("demo-fapi.binance.com", 6000)
    ex = Exchanges(Settings("demo", "k", "s", ":memory:", "", "", RiskLimits()))
    session = FakeSession(main, dm)
    for c in (ex.public, ex.trade):
        wire(c, session)
    yield SimpleNamespace(ex=ex, main=main, demo=dm)
    await ex.close()


async def research_klines(client, wait_s=30.0):
    with weight.tagged(RESEARCH, wait_s=wait_s):
        return await client.fapiPublicGetKlines(KL)


async def test_research_blocks_above_1200_while_protective_calls_pass(clock, demo):
    demo.main.step = 250
    for _ in range(5):                                   # 250 ... 1,250: admitted while the reading was <= 1,200
        await research_klines(demo.ex.public)
    with pytest.raises(BudgetWait, match="1250/2400"):
        await research_klines(demo.ex.public)
    for _ in range(4):                                   # 1,500 ... 2,250: protective calls never wait
        with weight.tagged(PROTECTIVE):
            await demo.ex.public.fapiPublicGetTime()
    assert len(demo.main.paths("/klines")) == 5 and len(demo.main.paths("/time")) == 4
    with pytest.raises(BudgetWait):
        with weight.tagged(DESK, wait_s=10):            # 2,250 > 80%: the desk waits too
            await demo.ex.public.fapiPublicGetTime()
    clock.t += weight.WINDOW_S                           # the reading ages out: research resumes
    await research_klines(demo.ex.public)
    assert len(demo.main.paths("/klines")) == 6


async def test_a_429_on_one_host_does_not_block_the_other(clock, demo):
    demo.main.answer_next(429, retry_after=10)
    with pytest.raises(ccxt.DDoSProtection), weight.tagged(PROTECTIVE):
        await demo.ex.public.fapiPublicGetTime()
    with pytest.raises(BudgetWait, match="429"):
        await research_klines(demo.ex.public)            # 2 min of yielding on that host
    await research_klines(demo.ex.trade)                 # the demo host is untouched
    with weight.tagged(PROTECTIVE):
        await demo.ex.public.fapiPublicGetTime()         # and protective calls still go out on the limited one
    assert demo.main.paths("/klines") == [] and len(demo.demo.paths("/klines")) == 1


async def test_demo_split_hosts_are_tracked_separately(clock, demo):
    await demo.ex.trade.fapiPublicGetExchangeInfo()      # the demo host's own limit: 6,000/min (measured 2026-10-02)
    demo.main.used, demo.demo.used = 1499, 2499
    await research_klines(demo.ex.public)
    await research_klines(demo.ex.trade)
    assert weight.budget("fapi.binance.com").used == 1500 and weight.budget("demo-fapi.binance.com").used == 2500
    with pytest.raises(BudgetWait):
        await research_klines(demo.ex.public)            # 1,500 of 2,400
    await research_klines(demo.ex.trade)                 # 2,501 of 6,000: under half


async def test_live_shares_one_budget_between_the_trade_and_public_clients(clock):
    ex = Exchanges(Settings("live", "k", "s", ":memory:", "", "", RiskLimits()))
    host = FakeHost("fapi.binance.com", 2400)
    session = FakeSession(host)
    for c in (ex.public, ex.trade):
        wire(c, session)
    try:
        host.used = 1999
        with weight.tagged(PROTECTIVE):
            await ex.trade.fapiPublicGetTime()           # the trade client's response...
        with pytest.raises(BudgetWait):
            await research_klines(ex.public)             # ...holds back research on the public client
    finally:
        await ex.close()


async def test_ccxt_never_resends_a_429_and_raises_it_at_once(clock, demo):
    assert demo.ex.public.options["maxRetriesOnFailure"] == 3      # market data retries transient failures...
    demo.main.answer_next(429, times=4)
    t0 = time.monotonic()
    with pytest.raises(ccxt.DDoSProtection), weight.tagged(PROTECTIVE):
        await demo.ex.public.fapiPublicGetTime()
    assert len(demo.main.sent) == 1                      # ...but never a 429: resending it is what earns a 418 ban
    assert time.monotonic() - t0 < 0.5                   # and without ccxt's 1 s pauses before giving up


async def test_an_order_write_upgrades_the_rest_of_its_sequence(clock, demo):
    demo.demo.used = 5000
    with weight.tagged(DESK, wait_s=10):
        await demo.ex.trade.fapiPrivatePostOrder({"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET",
                                                  "quantity": "0.001"})
        await demo.ex.trade.fapiPublicGetTime()          # 5,001 of 6,000 > 80%, but the sequence has started
    assert [m for m, _ in demo.demo.sent] == ["POST", "GET"]


async def test_market_reloads_never_wait_while_holding_the_shared_lock(clock, demo, monkeypatch):
    classes = []

    async def load(reload=False, params=None):
        classes.append(weight.current())
        demo.ex.public.markets = demo.ex.trade.markets = {"BTC/USDT:USDT": {"active": True}}
    monkeypatch.setattr(demo.ex.public, "load_markets", load)
    monkeypatch.setattr(demo.ex.trade, "load_markets", load)
    with weight.tagged(RESEARCH):
        await demo.ex.markets()
        await demo.ex.trade_markets()
        assert weight.current() == RESEARCH
    assert classes == [PROTECTIVE, PROTECTIVE]


async def test_guardian_lanes_tag_their_jobs_and_record_a_budget_wait(clock, demo):
    from tests.test_guardian_loop import lane_kinds, lane_with
    seen = {}

    async def metrics():
        seen["metrics"] = weight.current()
        await demo.ex.public.fapiPublicGetKlines(KL)

    async def positions():
        seen["positions"] = weight.current()
        await demo.ex.public.fapiPublicGetTime()
    ln = lane_with(Job("metrics", metrics, 60, 30))
    ln.name = "research"
    weight.record("https://fapi.binance.com/fapi/v1/time", 200, {"x-mbx-used-weight-1m": "2000"}, "")
    t0 = clock.t
    await ln._run(ln.jobs[0])
    assert seen["metrics"] == RESEARCH and clock.t - t0 <= 30 - weight.JOB_MARGIN_S
    assert "guardian_error:metrics" in lane_kinds(ln) and "BudgetWait" in ln.j.alerts(5)[0]["message"]
    pl = lane_with(Job("positions", positions, 60, 240))
    pl.name = "protective"
    await pl._run(pl.jobs[0])
    assert seen["positions"] == PROTECTIVE and demo.main.paths("/time") == ["/fapi/v1/time"]
    assert weight.current() == DESK


async def test_mcp_tools_run_under_their_class(monkeypatch):
    from shortbot import mcp_server, mcp_trade
    TOOLS = mcp_server.TOOLS
    seen = []

    async def record(*a, **k):
        seen.append(weight.current())
        return {"verdict_for_shorts": "favorable"}
    app = SimpleNamespace(ex=None, journal=None, trader=SimpleNamespace(move_stop=record))
    monkeypatch.setattr(mcp_trade, "get_app", lambda: app)
    monkeypatch.setattr(mcp_trade, "regime", SimpleNamespace(market_regime=record))
    monkeypatch.setattr(mcp_trade, "scanner", SimpleNamespace(scan_shorts=record, coin_snapshot=record))
    await TOOLS["scan_shorts"]()
    await TOOLS["coin_snapshot"]("BTC")
    await TOOLS["move_stop"]("BTC", 1.0, "test")
    assert seen == [RESEARCH, RESEARCH, DESK, PROTECTIVE]


async def test_cli_commands_run_under_their_class(monkeypatch):
    from shortbot import cli
    seen = {}

    async def run(args):
        seen[args.cmd] = weight.current()
    monkeypatch.setattr(cli, "_run", run)
    monkeypatch.setattr(cli, "get_app", lambda: SimpleNamespace(journal=None))
    for cmd in ("scan", "positions", "close", "update-plan", "set-params"):
        await cli._audited(SimpleNamespace(cmd=cmd))
    assert seen == {"scan": RESEARCH, "positions": DESK, "close": PROTECTIVE, "update-plan": PROTECTIVE,
                    "set-params": PROTECTIVE}
    from tests.test_cli_fallback import _subparsers
    assert cli.PROTECTIVE_CMDS | cli.RESEARCH_CMDS <= set(_subparsers())   # no stale names
