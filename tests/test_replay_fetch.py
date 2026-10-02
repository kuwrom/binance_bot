"""Replay cache filling (backtest/replay_fetch.py): metadata from exchangeInfo, file keys, movers (task #29)."""

import asyncio

from shortbot import clock
from shortbot.backtest import replay_fetch as RF
from shortbot.backtest.replay_store import Store, parse_klines
from tests import replay_fakes as F

KL = ("open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,"
      "ignore\n1790208000000,1.0,1.1,0.9,1.05,100,1790208059999,104,7,60,62.4,0\n"
      "1790208060000000,1.05,1.2,1.0,1.1,50,1790208119999999,55,3,20,22,0\n")


def test_parse_klines_keeps_base_volumes_and_handles_microseconds():
    b = parse_klines(KL)
    assert b[0] == (1790208000000, 1.0, 1.1, 0.9, 1.05, 100.0, 104.0, 7, 60.0, 62.4)
    assert b[1][0] == 1790208060000 and b[1][5] == 50.0 and b[1][8] == 20.0


def sym(raw, **k):
    return {"symbol": raw, "contractType": "PERPETUAL", "underlyingType": "COIN", "quoteAsset": "USDT",
            "status": "TRADING", "onboardDate": k.get("on", 1), "deliveryDate": k.get("dd", 4133404800000),
            "underlyingSubType": k.get("tags", []), "filters": [{"filterType": "MIN_NOTIONAL", "notional": "5"}]}


def test_build_meta_point_in_time_fields():
    prod = [sym("AAAUSDT", tags=["AI"]), sym("OLDUSDT", dd=F.T0 - 30 * F.DAY), sym("NEWUSDT", on=F.T0 + 99 * F.DAY),
            sym("DELUSDT", dd=F.T0 + F.DAY), {**sym("XAUUSDT"), "underlyingType": "COMMODITY"},
            {**sym("BTCUSDT_261225"), "contractType": "CURRENT_QUARTER"}]
    demo = [{"symbol": "AAAUSDT", "status": "TRADING"}, {"symbol": "DELUSDT", "status": "PENDING_TRADING"}]
    meta = RF.build_meta(prod, demo, {"AAAUSDT": {"spot_raw": "AAAUSDT", "spot_mult": 1.0}},
                         F.T0, F.T0 + 2 * F.DAY)
    assert sorted(meta) == ["AAAUSDT", "DELUSDT"]
    assert meta["AAAUSDT"]["demo"] and meta["AAAUSDT"]["spot"] == ["AAAUSDT", 1.0] and meta["AAAUSDT"]["tags"] == ["AI"]
    assert meta["DELUSDT"]["delivery_ms"] == F.T0 + F.DAY and not meta["DELUSDT"]["demo"]
    assert meta["AAAUSDT"]["delivery_ms"] is None and meta["AAAUSDT"]["min_notional"] == 5.0


class FakeFetcher:
    def __init__(self):
        self.keys = []

    async def csv(self, key):
        self.keys.append(key)
        return KL if "AAAUSDT" in key else None


def test_fetch_kind_caches_files(tmp_path):
    s, f = Store(tmp_path / "c.db"), FakeFetcher()
    n = asyncio.run(RF.fetch_kind(f, s, "perp", ["AAAUSDT", "ZZZUSDT"], ["2026-09-24"]))
    assert n == 2 and f.keys == ["data/futures/um/daily/klines/AAAUSDT/1m/AAAUSDT-1m-2026-09-24.zip",
                                 "data/futures/um/daily/klines/ZZZUSDT/1m/ZZZUSDT-1m-2026-09-24.zip"]
    asyncio.run(RF.fetch_kind(f, s, "perp", ["AAAUSDT", "ZZZUSDT"], ["2026-09-24"]))
    assert len(f.keys) == 2                                  # cached, a missing file included
    assert len(s.fine("perp", "AAAUSDT", 0, 2 ** 62)) == 2
    assert RF.KEYS["metrics"].format(um="U", spot="S", raw="X", day="D") == "U/daily/metrics/X/X-metrics-D.zip"


def test_days_skip_unpublished():
    assert RF.days(F.T0, F.T0 + 2 * F.DAY) == ["2026-09-24", "2026-09-25"]


def test_movers_are_the_extremes_at_each_moment(tmp_path):
    s = F.build(tmp_path / "c.db")
    with clock.at(F.T):
        mv = RF.movers(s, [F.T], depth=1, margin=0, universe="demo")
    assert mv == ["AAAUSDT", "BBBUSDT", "BTCUSDT", "ETHUSDT"]


def test_days_stop_before_today():
    import time
    now = int(time.time() * 1000)
    d = RF.days(now - 3 * F.DAY, now + 5 * F.DAY)
    assert len(d) == 3 and all(isinstance(x, str) for x in d)
