"""Market-news labels (#92, market_news_label.py): BTC, ETH and alt-breadth returns filled once 24h are due, never
before; an event without reference prices is no_data; the report clusters by day."""

import json
import time

from shortbot.journal import Journal
from shortbot.market_news_label import BAR_MS, LABEL_AFTER_MS, MAX_TRIES, basket, label, report, returns, window_start
from tests.test_market_news import Ex

H = 3_600_000


def add_event(j, ts_ms, classes="holder_transfer", ref=True):
    r = {"btc": 80_000.0, "eth": 4_000.0, "alts": {"A0/USDT:USDT": 1.0, "A1/USDT:USDT": 2.0}} if ref else None
    j.db.execute("INSERT INTO market_news_events (ts, ts_ms, coin, classes, title, key, ref) VALUES (?,?,?,?,?,?,?)",
                 (time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ts_ms / 1000)), ts_ms, "BTC", classes, "t",
                  f"k{ts_ms}{classes}", json.dumps(r) if r else None))
    j.db.commit()


def test_returns_and_basket():
    bars = [[0, 100, 100, 100 - k - 1, 100 - k - 1, 1] for k in range(96)]
    assert returns(100, bars) == {"r1h": -4.0, "r4h": -16.0, "r24h": -96.0}
    assert returns(100, bars[:10])["r4h"] is None
    b = basket({"A": {"r1h": -1, "r4h": 1, "r24h": None}, "B": {"r1h": -3, "r4h": -1, "r24h": None}})
    assert b["r1h_median"] == -2 and b["r1h_down_pct"] == 100 and b["r4h_down_pct"] == 50 and b["r24h_median"] is None


async def test_due_events_are_labelled_and_the_rest_wait():
    j = Journal(":memory:")
    now = int(time.time() * 1000)
    add_event(j, now - LABEL_AFTER_MS - H)               # due
    add_event(j, now - LABEL_AFTER_MS - H, "etf_flow")   # due, same 15m bar: its klines come from the cache
    add_event(j, now - 2 * H)                            # not yet
    add_event(j, now - LABEL_AFTER_MS - 2 * H, ref=False)
    ex = Ex()
    assert await label(ex, j, now) == 3
    klines = [c for c in ex.public.calls if c[0] == "klines"]
    assert len(klines) == 4 and all(c[2] == "15m" and c[4] < 100 for c in klines)   # one fetch per symbol
    t0 = now - LABEL_AFTER_MS - H
    assert all(c[3] == window_start(t0) and t0 < c[3] <= t0 + BAR_MS for c in klines)   # after detection only
    rows = j.db.execute("SELECT outcome FROM market_news_events ORDER BY id").fetchall()
    o = json.loads(rows[0][0])
    assert 0 < o["start_lag_min"] <= 15
    assert o["btc"]["r1h"] < 0 and o["btc"]["low24h"] < o["btc"]["low4h"] < 0 and o["eth"]["r24h"] < 0
    assert o["alts"]["n"] == 2 and o["alts"]["r4h_down_pct"] == 100
    assert rows[2][0] is None and json.loads(rows[3][0]) == {"no_data": True}
    rep = report(j)
    assert rep["labelled"] == 2 and rep["no_data"] == 1 and rep["failed"] == 0 and rep["pending"] == 1
    assert rep["classes"]["all"]["events"] == 2 and rep["classes"]["all"]["days"] == 1
    assert rep["classes"]["etf_flow"]["4h"]["btc"]["mean_pct"] < 0
    assert "title" not in json.dumps(rep)


async def test_a_failed_btc_fetch_is_retried_then_marked_failed_and_a_failed_alt_drops_out():
    j = Journal(":memory:")
    now = int(time.time() * 1000)
    add_event(j, now - LABEL_AFTER_MS - H)
    ex = Ex()
    real = ex.public.fetch_ohlcv

    async def btc_down(sym, *a, **k):
        if sym.startswith("BTC"):
            raise TimeoutError("weight guard: budget wait")
        return await real(sym, *a, **k)
    ex.public.fetch_ohlcv = btc_down
    for i in range(MAX_TRIES - 1):
        assert await label(ex, j, now) == 0                       # left for the next run
        assert tuple(j.db.execute("SELECT label_tries, outcome FROM market_news_events").fetchone()) == (i + 1, None)
    assert await label(ex, j, now) == 1
    assert "failed" in json.loads(j.db.execute("SELECT outcome FROM market_news_events").fetchone()[0])
    assert report(j)["failed"] == 1 and report(j)["labelled"] == 0

    add_event(j, now - LABEL_AFTER_MS - 2 * H)

    async def alt_down(sym, *a, **k):
        if sym.startswith("A1"):
            raise TimeoutError("418")
        return await real(sym, *a, **k)
    ex.public.fetch_ohlcv = alt_down
    assert await label(ex, j, now) == 1
    o = json.loads(j.db.execute("SELECT outcome FROM market_news_events ORDER BY id DESC").fetchone()[0])
    assert o["alts"]["n"] == 1 and o["btc"]["r1h"] is not None
