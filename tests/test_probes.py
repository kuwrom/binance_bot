"""Demo exchange-behaviour probes (#55): `shortbot probe NAME [--symbol SYM] [--all]`. Against the fake every probe runs
end to end, leaves the symbol clean, journals one probe_result row and finds what the fake assumes (the fake IS the
assumption each probe checks on demo); it refuses in live mode, on a symbol the desk is using, or off ACTIVE."""

import json
from types import SimpleNamespace

import ccxt
import pytest

from shortbot import cli, probe_kit, probe_stream, probes
from shortbot import killswitch as ks
from shortbot.cli_args import build_parser
from shortbot.config import RiskLimits, Settings
from shortbot.entries import EntryDesk
from shortbot.journal import Journal, now_iso
from shortbot.trading import Trader
from tests.fakes import FakeCcxt, FakeEx, neutral_budget, ok_prechecks

XRP = "XRP/USDT:USDT"
TICK = 0.0001


def desk(mode="demo"):
    fake = FakeCcxt()
    fake.add_symbol(XRP, 2.5, tick=TICK, step=0.1, min_notional=5.0)
    ex = FakeEx(fake)
    if mode == "live":
        ex.settings = Settings("live", "k", "s", ":memory:", "", "", RiskLimits())
    return Trader(ex, Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget), fake


@pytest.fixture
def clock(monkeypatch):
    """probe_kit._sleep moves the fake's exchange clock (countdowns, GTD) and runs the test's hooks, never waits."""
    state = {"fake": None, "hooks": []}

    async def sleep(s):
        if state["fake"] is not None:
            state["fake"].advance(s)
        for h in state["hooks"]:
            h(s)
    monkeypatch.setattr(probe_kit, "_sleep", sleep)
    return state


@pytest.fixture(autouse=True)
def quiet_stream(monkeypatch):
    """No sockets in tests: each URL 'connects' and hears nothing."""
    async def listen(urls, done, settled=None, timeout_s=10.0):
        (settled if settled is not None else set()).update(urls)
        await done.wait()
        return {k: {"connected": True, "error": None, "events": []} for k in urls}
    monkeypatch.setattr(probe_stream, "listen", listen)


def clean(fake, s=XRP) -> bool:
    return (fake.sym[s]["qty"] == 0
            and not [o for o in fake.limits.values() if o["symbol"] == s and o["status"] == "open"]
            and not [i for i in [*fake.stops, *fake.prearms] if fake._algo_sym(i) == s])


def rows(tr):
    return [r for r in tr.j.decisions(limit=200) if r["action"] == "probe_result"]


def bump_when_near_stop_rests(fake):
    def hook(_s):
        if any(fake._algo_sym(i) == XRP for i in fake.prearms):
            fake.set_price(XRP, round(fake.sym[XRP]["price"] + 5 * TICK, 4))
    return hook


# ------------------------------------------------------------ every probe, end to end
@pytest.mark.parametrize("name", [n for n in probes.ORDER if n != "income-timestamps"])
async def test_each_probe_runs_cleans_up_journals_and_matches_the_fake(name, clock):
    tr, fake = desk()
    clock["fake"] = fake
    r = await probes.run(tr, name, "XRPUSDT")
    assert r["ok"] and r["probe"] == name and r["clean"], r
    assert r["matches_fake"] is (None if name == "user-stream" else True), r["verdict"]
    assert clean(fake)
    (row,) = rows(tr)
    snap = json.loads(row["snapshot"])
    assert row["symbol"] == XRP and snap["probe"] == name and snap["calls"] and snap["fake_assumption"]
    assert snap["number"] == probes.CASES[name].number and name in row["reasoning"]
    assert probes.active_symbols(tr.j) == set()


async def test_income_timestamps_reads_the_probe_trades_and_finds_the_trade_time(clock):
    tr, fake = desk()
    clock["fake"] = fake
    assert (await probes.run(tr, "close-stop-at-flat", "XRPUSDT"))["ok"]
    n = len(fake.orders)
    r = await probes.run(tr, "income-timestamps", "XRPUSDT")
    assert r["ok"] and r["matches_fake"] is True and len(fake.orders) == n   # read only
    pairs = r["observed"]["pairs"]
    assert {p["type"] for p in pairs} == {"COMMISSION", "REALIZED_PNL"}
    assert all(-1000 < p["delta_ms"] <= 0 and p["income_time"] % 1000 == 0 for p in pairs)   # probe_result 395


async def test_income_timestamps_without_trades_says_so(clock):
    tr, fake = desk()
    clock["fake"] = fake
    r = await probes.run(tr, "income-timestamps", "XRPUSDT")
    assert r["ok"] and r["matches_fake"] is None and "no trades" in r["verdict"]


async def test_cancel_finished_when_the_near_stop_triggers(clock):
    tr, fake = desk()
    clock["fake"] = fake
    clock["hooks"].append(bump_when_near_stop_rests(fake))
    r = await probes.run(tr, "cancel-finished", "XRPUSDT")
    obs = r["observed"]
    assert r["ok"] and r["clean"] and obs["triggered"] and r["matches_fake"] is True
    assert obs["cancels"]["finished"]["code"] == -2011 and obs["cancels"]["after_flat"]["code"] == -2011
    assert clean(fake) and fake.fills[-1]["side"] == "buy"   # the trigger bought the short back


async def test_cancel_finished_without_a_trigger_still_measures_canceled_and_expired(clock):
    tr, fake = desk()
    clock["fake"] = fake
    r = await probes.run(tr, "cancel-finished", "XRPUSDT")
    obs = r["observed"]
    assert not obs["triggered"] and obs["cancels"]["canceled_first"]["ok"]
    assert obs["cancels"]["canceled_repeat"]["code"] == -2011 and obs["cancels"]["after_flat"]["code"] == -2011
    assert r["matches_fake"] is True and clean(fake)


async def test_user_stream_never_journals_the_listen_key(clock, monkeypatch):
    tr, fake = desk()
    clock["fake"] = fake
    seen = {}

    async def listen(urls, done, settled=None, timeout_s=10.0):
        seen.update(urls)
        settled.update(urls)
        await done.wait()
        return {k: {"connected": k == "doc_demo_private", "error": None if k == "doc_demo_private" else "x",
                    "events": [{"e": "ORDER_TRADE_UPDATE"}, {"e": "ALGO_UPDATE"}] if k == "doc_demo_private" else []}
                for k in urls}
    monkeypatch.setattr(probe_stream, "listen", listen)
    r = await probes.run(tr, "user-stream", "XRPUSDT")
    key = (await fake.fapiPrivatePostListenKey())["listenKey"]
    assert set(seen) == {"ccxt_pro_demo", "doc_demo_private", "doc_demo_legacy"}
    assert all(key in u for u in seen.values()) and seen["ccxt_pro_demo"].startswith("wss://fstream.binancefuture")
    assert key not in json.dumps(r) and key not in json.dumps([dict(x) for x in tr.j.decisions(limit=50)])
    assert r["observed"]["doc_demo_private"]["event_types"] == ["ALGO_UPDATE", "ORDER_TRADE_UPDATE"]
    assert clean(fake)


async def test_run_all_runs_every_probe_in_order_and_leaves_the_symbol_clean(clock):
    tr, fake = desk()
    clock["fake"] = fake
    out = await probes.run_all(tr, "XRPUSDT")
    assert [r["probe"] for r in out["results"]] == probes.ORDER and out["ok"]
    assert all(r["clean"] for r in out["results"]) and clean(fake) and len(rows(tr)) == len(probes.ORDER)


# ------------------------------------------------------------ cleanup
async def test_a_fault_mid_probe_still_flattens_and_cancels(clock, monkeypatch):
    tr, fake = desk()
    clock["fake"] = fake

    async def down(req=None):
        raise ccxt.NetworkError("mark price unreadable")
    monkeypatch.setattr(fake, "fapiPublicGetPremiumIndex", down)
    r = await probes.run(tr, "cancel-finished", "XRPUSDT")
    assert r["ok"] is False and "NetworkError" in r["error"] and r["clean"] and clean(fake)
    assert any(c["call"] == "cleanup: close position" for c in r["calls"])
    assert len(rows(tr)) == 1 and probes.active_symbols(tr.j) == set()


async def test_a_trip_mid_probe_stops_it_before_its_next_opening_order(clock, monkeypatch):
    tr, fake = desk()
    clock["fake"] = fake
    send = fake.create_order

    async def create_order(s, type_, side, amount, price=None, params=None):
        try:
            return await send(s, type_, side, amount, price, params)
        finally:
            if type_ == "STOP_MARKET":   # another process trips the kill switch after the first flat stop (-4509)
                tr.j.set(ks.KV_KEY, json.dumps({"state": ks.REDUCING, "by": "test", "reason": "mid-probe", "at": None}))
    monkeypatch.setattr(fake, "create_order", create_order)
    r = await probes.run(tr, "second-close-stop", "XRPUSDT")
    assert r["ok"] is False and "REDUCING" in r["error"] and r["clean"] and clean(fake)
    assert not [o for o in fake.orders if o[1] == "sell"]   # the short was never opened


async def test_the_probe_row_is_live_only_while_a_probe_position_exists(clock):
    tr, fake = desk()
    clock["fake"] = fake
    seen = []
    clock["hooks"].append(lambda s: seen.append((fake.sym[XRP]["qty"] > 0, probes.active_symbols(tr.j))))
    await probes.run(tr, "close-stop-at-flat", "XRPUSDT")
    assert seen and all(syms == {XRP} for held, syms in seen)
    assert tr.j.get(probes.ACTIVE_KEY + XRP) is None
    seen.clear()
    await probes.run(tr, "fetch-by-cid", "XRPUSDT")   # a flat probe holds nothing
    assert all(syms == set() for _, syms in seen)


# ------------------------------------------------------------ refusals: nothing sent, nothing journalled
async def _refused(tr, fake, name="fetch-by-cid", why=""):
    r = await probes.run(tr, name, "XRPUSDT")
    assert r["ok"] is False and r["refused"] and why in r["error"], r
    assert rows(tr) == []
    return r


async def test_refuses_in_live_mode(clock):
    tr, fake = desk("live")
    clock["fake"] = fake
    await _refused(tr, fake, why="demo")
    assert fake.orders == [] and (await probes.run_all(tr, "XRPUSDT"))["ok"] is False


async def test_refuses_off_the_demo_host(clock):
    tr, fake = desk()
    fake.urls = {"api": {"fapiPrivate": "https://fapi.binance.com/fapi/v1"}}
    await _refused(tr, fake, why="demo")
    assert fake.orders == []


async def test_refuses_on_a_symbol_with_an_open_journal_trade(clock):
    tr, fake = desk()
    tr.j.create_trade(XRP, "pump_fade", "t", 2.8, 2)
    await _refused(tr, fake, name="close-stop-at-flat", why="trade")
    assert fake.orders == []


async def test_refuses_on_a_symbol_with_resting_entries(clock):
    tr, fake = desk()
    EntryDesk(tr)
    tr.j._insert("INSERT INTO entries (group_id, symbol, status, created_at, expires_ms) VALUES ('g', ?, 'pending', ?, 0)",
                 (XRP, now_iso()))
    await _refused(tr, fake, why="entries")
    assert fake.orders == []


async def test_refuses_on_a_symbol_with_a_position_or_orders_on_the_exchange(clock):
    tr, fake = desk()
    await fake.create_order(XRP, "limit", "sell", 5, 2.7, {"timeInForce": "GTC"})   # the owner's, say
    await _refused(tr, fake, why="open order")
    tr2, fake2 = desk()
    await fake2.create_order(XRP, "market", "sell", 5)
    await _refused(tr2, fake2, why="position")
    assert len(fake2.orders) == 1


async def test_refuses_unless_the_kill_switch_is_active(clock):
    tr, fake = desk()
    await ks.trip(tr.j, tr.ex.settings, ks.REDUCING, "test", "probe refusal")
    await _refused(tr, fake, why="kill switch")
    assert fake.orders == []


# ------------------------------------------------------------ CLI
def test_cli_probe_command_is_protective_and_takes_a_name_or_all():
    p = build_parser()
    a = p.parse_args(["probe", "fetch-by-cid", "--symbol", "ETHUSDT"])
    assert (a.cmd, a.name, a.symbol, a.all) == ("probe", "fetch-by-cid", "ETHUSDT", False)
    a = p.parse_args(["probe", "--all"])
    assert a.all and a.name is None and a.symbol == probes.DEFAULT_SYMBOL
    assert "probe" in cli.PROTECTIVE_CMDS and "probe" not in cli.READS
    with pytest.raises(SystemExit):
        p.parse_args(["probe", "no-such-probe"])


async def test_cli_runs_the_probe_through_the_desk(clock, monkeypatch):
    tr, fake = desk()
    clock["fake"] = fake
    app = SimpleNamespace(trader=tr, journal=tr.j, ex=tr.ex, settings=tr.ex.settings)
    monkeypatch.setattr(cli, "get_app", lambda *a, **k: app)

    async def no_close():
        return None
    monkeypatch.setattr(cli, "close_app", no_close)
    r = await cli._audited(build_parser().parse_args(["probe", "algo-unknown-cid", "--symbol", "XRPUSDT"]))
    assert r["ok"] and r["probe"] == "algo-unknown-cid" and fake.orders == []
    assert (await cli._audited(build_parser().parse_args(["probe"])))["ok"] is False   # neither a name nor --all
