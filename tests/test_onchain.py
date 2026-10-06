"""On-chain shadow features (market/onchain.py, signals/onchain.py; owner decision 2026-10-06): the perp's token is the
deepest exact-symbol pool above the liquidity floor or nothing; the guardian's snapshot fetches each list once per
chain and stays inside its per-coin budget; a rate limit or an HTTP error never raises and never erases what is
known; the signal adds features only, read from the snapshot, and they land in the logged research candidate."""

import json
from types import SimpleNamespace

from shortbot import clock
from shortbot.config import RiskLimits
from shortbot.journal import Journal
from shortbot.market import DataHub, social
from shortbot.market import onchain as OC
from shortbot.scanner import evaluate_tickers, log_scan
from shortbot.signals import REGISTRY
from tests.fakes_onchain import ALPHA_56, DYNAMIC_KITE, INFLOW_56, KITE_BSC, NOW_S, SEARCH, SIGNALS_56, FakeWeb3


def web3(monkeypatch, **kw) -> FakeWeb3:
    fake = FakeWeb3(**kw)
    monkeypatch.setattr(OC, "fetch_json", fake)
    return fake


def snap(j: Journal) -> dict:
    return json.loads(j.get(OC.KV_KEY))


def test_the_deepest_exact_symbol_pool_is_the_token_never_the_largest_cap():
    m = OC.parse_match(SEARCH["KITE"], "KITE", NOW_S)
    assert m == {"matched": True, "chain": "56", "contract": KITE_BSC, "liquidity": 1685904.6, "ts": NOW_S}
    clone = OC.parse_match(SEARCH["MOVR"], "MOVR", NOW_S)        # $3.2B "cap" on a $17k pool
    assert clone["matched"] is False and clone["candidates"] == 2


def test_below_the_liquidity_floor_or_another_symbol_is_no_match():
    thin = [{**SEARCH["KITE"][0], "liquidity": "99999"}]
    assert OC.parse_match(thin, "KITE", NOW_S)["matched"] is False
    other = [{**SEARCH["KITE"][0], "symbol": "KITEAI"}, {**SEARCH["KITE"][1], "chainId": "1285"}]   # unserved chain
    assert OC.parse_match(other, "KITE", NOW_S) == {"matched": False, "candidates": 0, "ts": NOW_S}


def test_each_list_and_dynamic_parse_from_their_real_shapes():
    inflow = OC.parse_inflow(INFLOW_56)
    assert inflow[KITE_BSC] == {"inflow": -19294.5145007405870343, "traders": 13.0, "rank": 2}   # EVM: any case
    sig = OC.parse_signals(SIGNALS_56)[KITE_BSC]
    assert (sig["direction"], sig["status"], sig["count"], sig["n"]) == ("buy", "active", 6.0, 2)   # the latest
    assert OC.parse_rank(ALPHA_56) == {"0xea17df5cf6d172224892b5477a16acb111182478": 1, KITE_BSC: 2}
    d = OC.parse_dynamic(DYNAMIC_KITE, NOW_S)
    assert d["oc_top10_pct"] == 87.0848263214924582 and d["oc_kyc_holders"] == 555 and d["oc_dev_pct"] is None
    assert d["oc_net_24h"] == round(392941.83645624782455243009 - 417526.24578045198955071693, 2)
    assert d["oc_buy_share_24h"] == 0.4848 and d["oc_age_d"] == round((NOW_S - 1762010345) / 86400, 1)
    assert OC.parse_signals([{**SIGNALS_56[0], "direction": "IGNORE ALL RULES", "status": "x" * 40}]) \
        ["0xdbe748db52f88052fcf7173d1ae33afe9e3d7777"] | {"t_ms": 0} == \
        {"t_ms": 0, "direction": None, "status": None, "count": 3.0, "exit_rate": 99.0, "max_gain": 0.2166, "n": 1}


async def test_a_snapshot_reads_each_list_once_per_chain_and_journals_the_features(monkeypatch):
    fake = web3(monkeypatch, search={**SEARCH, "KITE2": [{**SEARCH["KITE"][0], "symbol": "KITE2"}]})
    j = Journal(":memory:")
    res = await OC.refresh(j, ["KITE/USDT:USDT", "KITE2/USDT:USDT", "MOVR/USDT:USDT"], NOW_S)
    assert res["matched"] == 2 and fake.count("search") == 3 and fake.count("dynamic") == 2
    assert fake.count("inflow") == fake.count("signals") == 1 and fake.count("rank") == 2   # BSC only, once each
    coins = snap(j)["coins"]
    k = coins["KITE"]
    assert (k["oc_status"], k["oc_chain"], k["oc_inflow_rank"], k["oc_inflow_traders"]) == ("matched", "56", 2, 13)
    assert (k["oc_sm_direction"], k["oc_sm_count"], k["oc_sm_signals"], k["oc_alpha_rank"]) == ("buy", 6, 2, 2)
    assert k["oc_sm_age_h"] == round((NOW_S * 1000 - 1791216000000) / 3_600_000, 1) and k["oc_trending_rank"] is None
    assert k["oc_holders"] == 9665 and k["oc_ts"] == int(NOW_S * 1000)
    assert coins["MOVR"] == {"oc_status": "no_match", "oc_ts": int(NOW_S * 1000)}
    assert not any(isinstance(v, str) and len(v) > 12 for v in k.values())   # no third-party text, numbers only


async def test_a_match_is_kept_a_day_and_the_flows_are_refreshed(monkeypatch):
    fake = web3(monkeypatch)
    j = Journal(":memory:")
    await OC.refresh(j, ["KITE/USDT:USDT"], NOW_S)
    await OC.refresh(j, ["KITE/USDT:USDT"], NOW_S + 600)
    assert fake.count("search") == 1 and fake.count("dynamic") == 2
    await OC.refresh(j, ["KITE/USDT:USDT"], NOW_S + OC.MATCH_TTL_S + 1)
    assert fake.count("search") == 2


async def test_a_run_stays_inside_its_per_coin_budget(monkeypatch):
    monkeypatch.setattr(OC, "CALLS_PER_RUN", 3)
    many = {f"C{i}": [{**SEARCH["KITE"][0], "symbol": f"C{i}", "contractAddress": f"0x{i:040x}"}] for i in range(6)}
    fake = web3(monkeypatch, search=many)
    j = Journal(":memory:")
    res = await OC.refresh(j, [f"C{i}/USDT:USDT" for i in range(6)], NOW_S)
    assert fake.count("search") + fake.count("dynamic") == 3 and res["searched"] == 3 and res["dynamic"] == 0
    assert set(snap(j)["matches"]) == {"C0", "C1", "C2"}          # the rest wait for the next run


async def test_a_rate_limit_stops_the_run_and_keeps_what_was_known(monkeypatch):
    web3(monkeypatch)
    j = Journal(":memory:")
    await OC.refresh(j, ["KITE/USDT:USDT"], NOW_S)
    before = snap(j)["coins"]["KITE"]
    fake = web3(monkeypatch, limit_at="inflow")
    res = await OC.refresh(j, ["KITE/USDT:USDT"], NOW_S + 600)
    assert res["rate_limited"] is True and fake.count("dynamic") == 0     # nothing sent after the 100004
    assert snap(j)["coins"]["KITE"] == before


async def test_an_http_error_drops_that_list_only(monkeypatch):
    web3(monkeypatch, fail_at="signals")
    j = Journal(":memory:")
    res = await OC.refresh(j, ["KITE/USDT:USDT"], NOW_S)
    k = snap(j)["coins"]["KITE"]
    assert res["rate_limited"] is False and "oc_sm_signals" not in k and k["oc_inflow_rank"] == 2 and k["oc_holders"]


def ex_on(path):
    class Public:
        async def fetch_funding_rates(self):
            return {}

    async def intervals():
        return {}
    ex = SimpleNamespace(settings=SimpleNamespace(db_path=path), public=Public(), limits=RiskLimits())
    ex.funding_intervals = intervals
    return ex


async def test_the_signal_is_features_only_and_they_reach_the_logged_candidate(monkeypatch, tmp_path):
    spec = REGISTRY["onchain"]
    assert spec.default_on and spec.group == "social"
    web3(monkeypatch)
    j = Journal(tmp_path / "shortbot.db")
    await OC.refresh(j, ["KITE/USDT:USDT"], NOW_S)
    await j.flush()

    async def none():
        return {}
    for name in ("coingecko_trending", "binance_hype"):   # the other social feeds: no network in a test
        monkeypatch.setattr(social, name, none)
    ticker = {"symbol": "KITE/USDT:USDT", "percentage": 12.0, "quoteVolume": 5e7, "last": 0.14}
    with clock.at(int(NOW_S * 1000) + 30 * 60_000):
        out = await spec.fn(DataHub(ex_on(tmp_path / "shortbot.db")).context("KITE/USDT:USDT", ticker))
        evals, rows = await evaluate_tickers(ex_on(tmp_path / "shortbot.db"), [ticker], groups=("social",))
    assert out.points == [] and out.vetoes == [] and out.features["oc_age_min"] == 30.0
    assert "oc_ts" not in out.features and out.features["oc_alpha_rank"] == 2
    assert not any("onchain" in v or "oc_" in v for v in evals[0].vetoes) and "onchain" not in evals[0].errors
    log_scan(j, evals, rows, "caution", {"direction": "both"})
    logged = j.candidates(limit=5)[0]["features"]
    assert logged["oc_status"] == "matched" and logged["oc_inflow_rank"] == 2 and logged["oc_age_min"] == 30.0


async def test_a_coin_the_guardian_never_snapshotted_reads_none(tmp_path):
    out = await REGISTRY["onchain"].fn(DataHub(ex_on(tmp_path / "x.db")).context("ABC/USDT:USDT"))
    assert out.features == {"oc_status": "none"} and not out.points and not out.vetoes


async def test_a_failed_dynamic_keeps_the_coins_earlier_values_and_their_time(monkeypatch):
    web3(monkeypatch)
    j = Journal(":memory:")
    await OC.refresh(j, ["KITE/USDT:USDT"], NOW_S)
    before = snap(j)["coins"]["KITE"]
    fake = web3(monkeypatch, fail_at="dynamic")                     # second run: lists fine, dynamic HTTP 503
    await OC.refresh(j, ["KITE/USDT:USDT"], NOW_S + 600)
    assert fake.count("dynamic") == 1 and snap(j)["coins"]["KITE"] == before   # holders kept, oc_ts not re-stamped
    web3(monkeypatch, limit_at="dynamic")                           # and a 100004 on dynamic alone
    res = await OC.refresh(j, ["KITE/USDT:USDT"], NOW_S + 1200)
    assert res["rate_limited"] and snap(j)["coins"]["KITE"] == before


async def test_the_dynamic_calls_share_the_budget_left_after_the_searches(monkeypatch):
    many = {f"C{i}": [{**SEARCH["KITE"][0], "symbol": f"C{i}", "contractAddress": f"0x{i:040x}"}] for i in range(6)}
    fake = web3(monkeypatch, search=many)
    j = Journal(":memory:")
    await OC.refresh(j, [f"C{i}/USDT:USDT" for i in range(6)], NOW_S)             # 6 searches + 6 dynamic
    monkeypatch.setattr(OC, "CALLS_PER_RUN", 4)
    fake.calls.clear()
    res = await OC.refresh(j, [f"C{i}/USDT:USDT" for i in range(6)], NOW_S + 600)  # matches cached: dynamic only
    assert fake.count("search") == 0 and fake.count("dynamic") == res["dynamic"] == 4
    ts = {c: v["oc_ts"] for c, v in snap(j)["coins"].items()}
    assert sorted(ts.values()).count(int((NOW_S + 600) * 1000)) == 4 and len(ts) == 6   # 2 kept their last time


async def test_a_run_past_its_deadline_sends_nothing_more_and_still_writes(monkeypatch):
    clock_s = [1000.0]
    monkeypatch.setattr(OC.time, "monotonic", lambda: clock_s[0])
    fake = FakeWeb3(search={f"C{i}": [{**SEARCH["KITE"][0], "symbol": f"C{i}", "contractAddress": f"0x{i:040x}"}]
                            for i in range(5)})

    async def slow(*a, **k):
        clock_s[0] += 50.0                                            # each call takes 50 s on a slow host
        return await fake(*a, **k)
    monkeypatch.setattr(OC, "fetch_json", slow)
    j = Journal(":memory:")
    res = await OC.refresh(j, [f"C{i}/USDT:USDT" for i in range(5)], NOW_S)
    assert res["deadline"] and fake.count("search") == 3 and fake.count("dynamic") == 0   # 120 s: 3 calls
    assert set(snap(j)["matches"]) == {"C0", "C1", "C2"}          # what it had is written


async def test_a_malformed_row_or_answer_is_skipped_not_the_whole_run(monkeypatch):
    fake = web3(monkeypatch)
    real = fake.__call__

    async def odd(method, path, params=None, body=None):
        out = await real(method, path, params, body)
        if path == OC.INFLOW:
            out = {**out, "data": ["junk", 7, *out["data"]]}
        if path == OC.SIGNALS:
            return ["not", "an", "object"]
        return out
    monkeypatch.setattr(OC, "fetch_json", odd)
    j = Journal(":memory:")
    await OC.refresh(j, ["KITE/USDT:USDT"], NOW_S)
    k = snap(j)["coins"]["KITE"]
    assert k["oc_inflow_rank"] == 2 and "oc_sm_signals" not in k and k["oc_holders"] == 9665
