"""Stage-2 cache filling (backtest/replay_window.py): monthly vs daily archive files, keys, hist.db metrics, movers."""

import sqlite3

from shortbot.backtest import replay_window as RW
from shortbot.backtest.replay_world import _ms
from tests import replay_fakes as F


def test_periods_use_published_months_and_days_otherwise():
    today = _ms("2026-10-02T10:00:00+00:00")
    p = RW.periods(_ms("2026-07-26T00:00:00+00:00"), _ms("2026-10-05T00:00:00+00:00"), today)
    assert p[:2] == ["2026-07", "2026-08"]                     # July from the 26th: still one monthly file
    assert p[2] == "2026-09-01" and p[-1] == "2026-10-01" and len(p) == 2 + 30 + 1   # not today (unpublished)
    assert RW.periods(_ms("2026-09-29T00:00:00+00:00"), _ms("2026-09-30T00:00:00+00:00"), today) == ["2026-09-29"]


def test_keys():
    assert RW.key("perp", "AAAUSDT", "2026-08") == "data/futures/um/monthly/klines/AAAUSDT/1m/AAAUSDT-1m-2026-08.zip"
    assert RW.key("mark", "AAAUSDT", "2026-09-03") == (
        "data/futures/um/daily/markPriceKlines/AAAUSDT/5m/AAAUSDT-5m-2026-09-03.zip")
    assert RW.key("spot", "AAAUSDT", "2026-08") == "data/spot/monthly/klines/AAAUSDT/5m/AAAUSDT-5m-2026-08.zip"
    assert RW.key("prem", "牛来USDT", "2026-09-03").startswith("data/futures/um/daily/premiumIndexKlines/牛来USDT/5m/")


def test_metrics_from_hist_and_movers(tmp_path):
    h = tmp_path / "hist.db"
    db = sqlite3.connect(str(h))
    db.execute("CREATE TABLE metrics_h (symbol TEXT, ts INTEGER, oi REAL, oi_val REAL, ls_top REAL, ls_top_acct REAL,"
               " ls_global REAL, taker REAL)")
    db.executemany("INSERT INTO metrics_h VALUES (?,?,?,?,?,?,?,?)",
                   [("DEMUSDT", F.T0 + k * F.HOUR, 1e6, 3e7, 1.2, 1.1, 1.5, 0.9) for k in range(30)])
    db.commit()
    s = F.build(tmp_path / "c.db")
    assert RW.metrics_from_hist(s, h, ["DEMUSDT"], F.T0, F.T0 + 10 * F.HOUR) == 10
    assert len(s.metrics("DEMUSDT", F.T0, F.T0 + 10 * F.HOUR)) == 10
    mv = RW.movers(s, ["AAAUSDT", "BBBUSDT", "BTCUSDT", "DEMUSDT"], [F.T], depth=1, margin=0)
    assert len(mv) == 3 and {"BBBUSDT", "BTCUSDT"} < set(mv)    # top loser, majors, one of the tied top gainers


def test_gaps_in_hist_db_are_filled_from_the_archive_and_rest(tmp_path):
    """hist.db lacked the non-ASCII perps: their metrics come from the daily archive, funding from REST pages."""
    import asyncio

    s = F.build(tmp_path / "c.db")
    MET = ("create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,"
           "sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
           "2026-09-24 00:00:00,DEMUSDT,10,20,1.1,1.2,1.3,0.9\n2026-09-24 01:00:00,DEMUSDT,11,22,1.1,1.2,1.3,0.9\n")

    class Fetch:
        keys: list = []

        async def csv(self, key):
            self.keys.append(key)
            return MET if key.endswith("2026-09-24.zip") else None

    class Client:
        calls: list = []

        async def get(self, url, params=None, timeout=None):
            self.calls.append(params["startTime"])
            first = params["startTime"]
            rows = [{"fundingTime": first + k * 3_600_000, "fundingRate": "0.0001"} for k in range(1000)
                    if first + k * 3_600_000 < params["endTime"]]

            class R:
                status_code = 200

                def json(self):
                    return rows
            return R()
    gaps = asyncio.run(RW.fill_gaps(s, Fetch(), Client(), ["DEMUSDT", "AAAUSDT"], F.T0, F.T0 + 2 * F.DAY,
                                    pace_s=0))
    assert gaps == {"metrics": ["DEMUSDT"], "funding": ["DEMUSDT"]}         # AAA already had both
    assert [m["oi_val"] for m in s.metrics("DEMUSDT", 0, 2 ** 62)] == [20.0, 22.0]
    assert len(s.funding("DEMUSDT", 0, 2 ** 62)) == 48 and len(Client.calls) == 1
