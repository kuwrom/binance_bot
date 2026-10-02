"""ReplayExchange (backtest/replay_exchange.py): the exchange surface the live scanner uses, served from the replay
cache as of the market clock. The real scan_shorts runs on it, and never sees data after the clock (task #29)."""

import asyncio
import json
import shutil

import pytest

from shortbot import clock
from shortbot.backtest import replay_bars as RB
from shortbot.backtest.replay_exchange import ReplayExchange, ReplayUnavailable
from shortbot.backtest.replay_world import offline_feeds
from shortbot.config import RiskLimits
from shortbot.journal import Journal
from shortbot.scanner import scan_shorts
from tests import replay_fakes as F

HOUR, MIN, DAY = F.HOUR, F.MIN, F.DAY


@pytest.fixture
def store(tmp_path):
    return F.build(tmp_path / "cache.db")


def run(coro, t=F.T):
    async def go():
        with clock.at(t):
            return await coro
    return asyncio.run(go())


def test_universe_is_point_in_time(store):
    rex = ReplayExchange(store, universe="demo")
    perps = run(rex.coin_perps())
    assert sorted(perps) == ["AAA/USDT:USDT", "BBB/USDT:USDT", "BTC/USDT:USDT"]   # not NEW (later), OLD, DEM
    assert "DEM/USDT:USDT" in run(ReplayExchange(store, universe="prod").coin_perps())
    m = perps["AAA/USDT:USDT"]
    assert m["info"]["contractType"] == "PERPETUAL" and m["info"]["status"] == "TRADING" and m["linear"]
    assert "NEW/USDT:USDT" in run(rex.coin_perps(), t=F.T + 2 * HOUR)


def test_tickers_are_the_rolling_24h_window(store):
    rex = ReplayExchange(store)
    tk = run(rex.public.fetch_tickers())
    full = store.fine("perp", "AAAUSDT", 0, 2 ** 62)
    want = RB.ticker(full, F.T)
    got = tk["AAA/USDT:USDT"]
    assert got["symbol"] == "AAA/USDT:USDT"
    for k in ("open", "last", "high", "low", "quoteVolume", "baseVolume"):
        assert got[k] == pytest.approx(want[k]), k
    assert got["percentage"] == pytest.approx(want["percentage"]) and got["percentage"] > 10


def test_klines_have_the_forming_bar_last_and_live_shapes(store):
    rex = ReplayExchange(store)
    c1h = run(rex.public.fetch_ohlcv("AAA/USDT:USDT", "1h", limit=60))
    assert len(c1h) == 60 and len(c1h[0]) == 6 and c1h[-1][0] == F.T // HOUR * HOUR
    mark = run(rex.public.fetch_ohlcv("AAA/USDT:USDT", "5m", limit=12, params={"price": "mark"}))
    assert mark[-1][5] == 0.0 and mark[-1][4] != c1h[-1][4]
    d1 = run(rex.public.fetch_ohlcv("AAA/USDT:USDT", "1d", limit=200))
    assert [b[0] for b in d1] == [F.T0, F.T0 + DAY, F.T0 + 2 * DAY]
    flow = run(rex.flow_klines("AAA/USDT:USDT", "5m", 288))
    assert len(flow) == 288 and len(flow[0]) == 7 and flow[-2][6] == pytest.approx(0.55 * flow[-2][5])
    prem = run(rex.premium_klines("AAA/USDT:USDT", "1h", 7))
    assert len(prem) == 7 and len(prem[0]) == 5 and prem[-1][4] == pytest.approx(0.0002)
    spot = run(rex.spot_flow_klines("AAAUSDT", "1h", 7))
    assert len(spot) == 7 and len(spot[0]) == 7


def test_market_info_and_delisting_lead(store):
    rex = ReplayExchange(store, delist_lead_days=7)
    a = run(rex.market_info("AAA/USDT:USDT"))
    assert a["listing_age_days"] == pytest.approx(round((F.T - F.META["AAAUSDT"]["onboard_ms"]) / 864e5, 1))
    assert a["delisting_scheduled"] is False and a["tags"] == ["AI"]
    b = run(rex.market_info("BBB/USDT:USDT"))
    assert b["delisting_scheduled"] is True and b["delivery_date_ms"] == F.T + 2 * DAY
    assert run(ReplayExchange(store, delist_lead_days=1).market_info("BBB/USDT:USDT"))["delisting_scheduled"] is False
    with pytest.raises(ValueError):
        run(rex.market_info("NEW/USDT:USDT"))
    assert run(ReplayExchange(store, universe="demo").account_tradable("AAA/USDT:USDT")) is True


def test_funding_positioning_and_unavailable_feeds(store):
    rex = ReplayExchange(store)
    rates = run(rex.public.fetch_funding_rates())
    assert rates["AAA/USDT:USDT"]["fundingRate"] == pytest.approx(0.0001)   # P + clamp(I - P), 8 h
    assert run(rex.funding_intervals())["AAAUSDT"]["interval_h"] == 8
    hist = run(rex.public.fetch_funding_rate_history("AAA/USDT:USDT", limit=21))
    assert len(hist) == 21 and hist[-1]["timestamp"] <= F.T
    oi = run(rex.oi_history("AAA/USDT:USDT", "1h", 25))
    assert len(oi) == 25 and oi[-1]["ts"] == F.T // HOUR * HOUR and oi[-1]["oi_value"] > oi[0]["oi_value"]
    assert run(rex.taker_ratio("AAA/USDT:USDT", "1h", 6)) == [pytest.approx(0.55 / 0.45)] * 6
    assert run(rex.long_short_ratios("AAA/USDT:USDT")) == {"top_trader_position_ls": 1.2, "global_account_ls": 1.5}
    with pytest.raises(ReplayUnavailable):
        run(rex.public.fetch_order_book("AAA/USDT:USDT"))
    assert run(rex.spot_symbol("AAA/USDT:USDT")) == ("AAAUSDT", 1.0)
    assert run(rex.spot_symbol("BBB/USDT:USDT"))[0] is None


def test_limits_follow_the_timeline(store):
    lim = {F.T - HOUR: RiskLimits(min_quote_volume_24h=5e6)}
    rex = ReplayExchange(store, limits_at=lambda t: lim.get(F.T - HOUR) if t >= F.T - HOUR else RiskLimits())
    with clock.at(F.T):
        assert rex.limits.min_quote_volume_24h == 5e6
    with clock.at(F.T - 2 * HOUR):
        assert rex.limits.min_quote_volume_24h == 20e6


def _scan(store, world):
    j = Journal(world)
    rex = ReplayExchange(store, universe="demo", db_path=world)
    with offline_feeds():
        res = run(scan_shorts(rex, min_change=5, top_n=10, depth=25, journal=j, regime="neutral",
                              direction="both"))
    rows = [dict(r) for r in j.db.execute("SELECT * FROM candidates ORDER BY symbol")]
    return res, rows


def test_the_real_scanner_runs_on_history_and_logs_its_arguments(store, tmp_path):
    res, rows = _scan(store, tmp_path / "world.db")
    assert [r["symbol"] for r in rows] == ["AAA/USDT:USDT", "BBB/USDT:USDT"]
    assert all(r["ts_ms"] == F.T for r in rows)
    f = json.loads(rows[0]["features"])
    assert f["scan"] == {"direction": "both", "min_change": 5, "depth": 25, "top_n": 10, "groups": None}
    assert f["change_24h"] > 10 and f["quote_volume"] > 0 and f["rsi_1h"] is not None and f["tape_state"]
    assert "order_book" in f["data_missing"] and "funding" not in f["data_missing"]
    assert not any("data unavailable" in v for v in json.loads(rows[0]["vetoes"]))
    assert res["universe"] == 3


def test_no_look_ahead_end_to_end(store, tmp_path):
    clean = _scan(store, tmp_path / "a.db")[1]
    shutil.copy(store.path, tmp_path / "poisoned.db")
    from shortbot.backtest.replay_store import Store
    bad = Store(tmp_path / "poisoned.db")
    F.poison_after(bad, F.T)
    dirty = _scan(bad, tmp_path / "b.db")[1]
    strip = lambda rows: [{k: r[k] for k in ("symbol", "price", "score", "tradable", "setups", "vetoes", "signals",  # noqa: E731
                                              "features")} for r in rows]
    assert strip(dirty) == strip(clean)


def test_the_look_ahead_probe_is_sensitive(store, tmp_path):
    """Poisoning data the scan legitimately reads (two hours before T) must change it, or the test above is vacuous."""
    clean = _scan(store, tmp_path / "a.db")[1]
    F.poison_after(store, F.T - 2 * HOUR)
    early = _scan(store, tmp_path / "b.db")[1]
    assert [r["features"] for r in early] != [r["features"] for r in clean]


def _to_five(store, kind):
    for (sym,) in store.db.execute("SELECT DISTINCT symbol FROM bars WHERE kind=?", (kind,)).fetchall():
        five = RB.group(store.fine(kind, sym, 0, 2 ** 62), 5 * MIN)
        store.db.execute("DELETE FROM bars WHERE kind=? AND symbol=?", (kind, sym))
        store.put_bars(kind, sym, five)
    steps = {**(store.get("steps") or {}), kind: 5 * MIN}
    store.set("steps", steps)


def test_five_minute_auxiliary_series(store):
    full = ReplayExchange(store)
    want_prem = run(full.premium_klines("AAA/USDT:USDT", "1h", 7))
    want_mark5 = run(full.public.fetch_ohlcv("AAA/USDT:USDT", "5m", limit=12, params={"price": "mark"}))
    for kind in ("mark", "prem", "spot"):
        _to_five(store, kind)
    rex = ReplayExchange(store)
    t5 = F.T // (5 * MIN) * (5 * MIN)                     # stage 2 scans on the 5-minute grid
    assert run(rex.premium_klines("AAA/USDT:USDT", "1h", 7), t5)[:-1] == run(full.premium_klines(
        "AAA/USDT:USDT", "1h", 7), t5)[:-1]
    assert len(run(rex.public.fetch_ohlcv("AAA/USDT:USDT", "5m", limit=12, params={"price": "mark"}), t5)) == 12
    with pytest.raises(ReplayUnavailable):
        run(rex.public.fetch_ohlcv("AAA/USDT:USDT", "1m", limit=60, params={"price": "mark"}))
    assert run(rex.spot_flow_klines("AAAUSDT", "1h", 7), t5)[-2] == run(full.spot_flow_klines("AAAUSDT", "1h", 7),
                                                                       t5)[-2]
    assert want_prem and want_mark5


def test_klines_since_like_rest(store):
    rex = ReplayExchange(store)
    since = F.T0 + DAY + 7 * MIN
    start = -(-since // (5 * MIN)) * (5 * MIN)
    got = run(rex.public.fetch_ohlcv("AAA/USDT:USDT", "5m", since=start, limit=36), F.T0 + 3 * DAY - 1)
    full = RB.group(store.fine("perp", "AAAUSDT", start, start + 36 * 5 * MIN), 5 * MIN)
    assert [g[:6] for g in got] == [[b[0], b[1], b[2], b[3], b[4], b[5]] for b in full]
    near_now = run(rex.public.fetch_ohlcv("AAA/USDT:USDT", "5m", since=F.T - 20 * MIN, limit=36))
    assert all(b[0] + 5 * MIN <= F.T for b in near_now[:-1]) and near_now[-1][0] <= F.T   # never past the clock
    store.derive_five("perp")
    assert run(rex.public.fetch_ohlcv("AAA/USDT:USDT", "5m", since=start, limit=36), F.T0 + 3 * DAY - 1) == got
