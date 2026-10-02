"""Replay world (backtest/replay_world.py): read-only journal snapshot, writable world copy, risk limits in force at
t, scan-argument inference and the blocked network feeds (task #29)."""

import asyncio
import json
import sqlite3

import pytest

from shortbot.backtest import replay_world as W
from shortbot.backtest.replay_exchange import ReplayUnavailable
from shortbot.journal import Journal
from shortbot.market import social

T = 1_790_000_000_000


def _journal(tmp_path):
    j = Journal(tmp_path / "live.db")
    j.log_candidates("2026-09-21T13:33:20+00:00", T, [
        {"symbol": "AAA/USDT:USDT", "price": 1.0, "score": 30, "setups": ["pump_fade"], "vetoes": [], "signals": [],
         "features": {"change_24h": 12.5, "cg_trending_rank": 3, "binance_hype": 70, "hype_sentiment": "bull"}},
        {"symbol": "BBB/USDT:USDT", "price": 2.0, "score": 5, "setups": [], "vetoes": ["major coin: x"],
         "signals": [], "features": {"change_24h": -6.2}}], "neutral")
    j.log_decision("set_params", "test", snapshot={"experiment": "e1", "changes": {"max_leverage": 10},
                                                   "before": {"min_quote_volume_24h": 20e6, "max_leverage": 3}})
    return j


def test_snapshot_is_read_only_and_world_is_clean(tmp_path):
    j = _journal(tmp_path)
    j.set("market_map_last", "{}")
    snap = W.snapshot(tmp_path / "live.db", tmp_path / "s" / "snap.db")
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(str(snap)).execute("DELETE FROM candidates")
    w = W.world(snap, tmp_path / "s" / "world.db")
    db = sqlite3.connect(str(w))
    assert db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM kv WHERE k='market_map_last'").fetchone()[0] == 0
    scans = W.logged_scans(snap, T - 1, T + 1)
    assert len(scans) == 1 and scans[0]["regime"] == "neutral" and len(scans[0]["rows"]) == 2
    assert scans[0]["rows"][0]["setups"] == ["pump_fade"] and scans[0]["rows"][0]["features"]["change_24h"] == 12.5


def test_limits_timeline(tmp_path):
    db = sqlite3.connect(str(tmp_path / "live.db"))
    db.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, ts TEXT, action TEXT, reasoning TEXT, snapshot TEXT)")
    db.executemany("INSERT INTO decisions (ts, action, reasoning, snapshot) VALUES (?,?,?,?)", [
        ("2026-09-27T13:59:23+00:00", "set_params", "x", json.dumps({"changes": {"max_leverage": 10},
                                                                     "before": {"min_quote_volume_24h": 20e6,
                                                                                "max_leverage": 3}})),
        ("2026-09-28T00:00:00+00:00", "hold", "x", "{}"),
        ("2026-09-29T18:15:08+00:00", "set_params", "x", json.dumps({"changes": {"min_quote_volume_24h": 5e6}})),
        ("2026-09-30T00:00:00+00:00", "reset_params", "x", "{}")])
    db.commit()
    at = W.limits_timeline(tmp_path / "live.db")
    before = W._ms("2026-09-27T13:00:00+00:00")
    assert (at(before).max_leverage, at(before).min_quote_volume_24h) == (3, 20e6)
    mid = at(W._ms("2026-09-29T00:00:00+00:00"))
    assert (mid.max_leverage, mid.min_quote_volume_24h) == (10, 20e6)
    late = at(W._ms("2026-09-29T19:00:00+00:00"))
    assert (late.max_leverage, late.min_quote_volume_24h) == (10, 5e6)
    assert at(W._ms("2026-10-01T00:00:00+00:00")).min_quote_volume_24h == 20e6


def row(ch, scan=None):
    return {"features": {"change_24h": ch, **({"scan": scan} if scan else {})}}


def test_infer_args():
    assert W.infer_args([row(52.0), row(12.3)])["direction"] == "gainers"
    assert W.infer_args([row(52.0), row(12.3)])["min_change"] == 10
    assert W.infer_args([row(58.3), row(5.49)])["min_change"] == 5
    assert W.infer_args([row(-19.7), row(-5.03)])["direction"] == "losers"
    both = W.infer_args([row(c) for c in [30, 20, 10, 9, -5, -7]])
    assert both["direction"] == "both" and both["min_change"] == 5 and both["inferred"]
    assert W.infer_args([row(c) for c in [*range(1, 15), -3]])["direction"] == "now"
    logged = {"direction": "both", "min_change": 3, "depth": 25, "top_n": 10, "groups": None}
    assert W.infer_args([row(4, logged)]) == {**logged, "inferred": False}


def test_offline_feeds_block_the_network_and_restore():
    live = social.coingecko_trending
    with W.offline_feeds():
        with pytest.raises(ReplayUnavailable):
            asyncio.run(social.coingecko_trending())
        from shortbot import news
        with pytest.raises(ReplayUnavailable):
            asyncio.run(news.news_for("AAA"))
    assert social.coingecko_trending is live
    o = W.Oracle()
    o.load([{"symbol": "1000AAA/USDT:USDT", "features": {"cg_trending_rank": 3, "binance_hype": 70,
                                                          "hype_sentiment": "bull"}}])
    with W.offline_feeds(o):
        assert asyncio.run(social.coingecko_trending()) == {"AAA": 3}
        assert asyncio.run(social.binance_hype())["AAA"]["hype"] == 70
