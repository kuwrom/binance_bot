"""histdata: parsers on samples copied from real data.binance.vision files, hourly metrics aggregation, file planning,
S3 listing pagination, the 1000X spot multiplier and the load60 shape."""

import asyncio
import json

import httpx

from shortbot import histdata as HD
from shortbot import histdata_vision as V

H = 3_600_000
D = 86_400_000
T0 = 1789862400000          # 2026-09-20 00:00 UTC

FUT_KLINES = """open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore
1789866000000,0.342500,0.343000,0.336900,0.339100,3196140.1,1789869599999,1083893.8669000,11632,1435001.3,486780.2574900,0
1789862400000,0.344600,0.345000,0.341400,0.342600,2824593.8,1789865999999,969067.8344800,11192,1184082.4,406330.9997300,0
"""
SPOT_KLINES = """1789862400000000,0.34510000,0.34540000,0.34190000,0.34290000,246538.50000000,1789865999999999,84678.55966000,1750,113219.60000000,38886.13145000,0
1789866000000000,0.34280000,0.34310000,0.33760000,0.33950000,386734.50000000,1789869599999999,131268.64768000,1765,122729.90000000,41624.80393000,0
"""
PREMIUM = """open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore
1789862400000,-0.00007960,0.00000000,-0.00166806,-0.00000055,0,1789865999999,0,720,0,0,0
"""
METRICS = """create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio
2026-09-20 00:05:00,CRVUSDT,70154210.8000000000000000,24062894.3044000000000000,1.08276077,1.91135300,0.77167582,0.34911300
2026-09-20 00:00:00,CRVUSDT,70073673.6000000000000000,24085467.3208833600000000,1.08408878,1.90930400,0.77294686,0.94591900
2026-09-20 00:55:00,CRVUSDT,70120857.8000000000000000,23997451.3479739100000000,1.09447583,1.90819800,0.77805611,0.25722100
2026-09-20 01:00:00,CRVUSDT,70200000.0000000000000000,24100000.0000000000000000,1.10000000,1.92000000,0.78000000,
"""
FUNDING = """calc_time,funding_interval_hours,last_funding_rate
1785571200000,8,0.00007479
1785542400001,8,0.00010000
"""


def test_futures_klines_with_header_sorted_ms():
    b = V.parse_klines(FUT_KLINES)
    assert [x["ts"] for x in b] == [T0, T0 + H]
    assert b[0] == {"ts": T0, "o": 0.3446, "h": 0.345, "l": 0.3414, "c": 0.3426, "qv": 969067.83448, "n": 11192,
                    "tbq": 406330.99973}


def test_spot_klines_headerless_microseconds_become_ms():
    b = V.parse_klines(SPOT_KLINES)
    assert [x["ts"] for x in b] == [T0, T0 + H] and b[1]["c"] == 0.3395 and b[1]["n"] == 1765


def test_premium_close_is_the_premium_fraction():
    assert V.parse_klines(PREMIUM)[0]["c"] == -0.00000055


def test_metrics_parse_sorts_and_maps_columns():
    rows = V.parse_metrics(METRICS)
    assert [r["ts"] for r in rows] == [T0, T0 + 300_000, T0 + 3_300_000, T0 + H]
    r = rows[0]
    assert r["oi_val"] == 24085467.32088336 and r["oi"] == 70073673.6
    assert (r["ls_top"], r["ls_top_acct"], r["ls_global"], r["taker"]) == (1.909304, 1.08408878, 0.77294686, 0.945919)
    assert rows[-1]["taker"] is None           # empty cell -> None, not a crash


def test_metrics_hourly_last_snapshot_at_or_before_hour_end():
    hr = V.metrics_hourly(V.parse_metrics(METRICS))
    # rows stamped 00:00..00:55 are observed at 00:05..01:00 -> hour ending 01:00; the 00:55 row is the last
    assert hr[T0 + H]["oi_val"] == 23997451.34797391 and hr[T0 + H]["ls_top"] == 1.908198
    # the 01:00 row is observed at 01:05 -> belongs to the hour ending 02:00
    assert hr[T0 + 2 * H]["oi_val"] == 24100000.0
    # taker: mean of the 5-minute ratios of [00:00, 01:00); the empty 01:00 cell adds nothing
    assert abs(hr[T0 + H]["taker"] - (0.945919 + 0.349113 + 0.257221) / 3) < 1e-12
    assert hr[T0 + 2 * H]["taker"] is None


def test_funding_sorted_and_floored_to_the_second():
    assert V.parse_funding(FUNDING) == [[1785542400000, 0.0001], [1785571200000, 0.00007479]]


def test_spot_candidates_prefer_exact_pair_then_stripped_with_multiplier():
    assert V.spot_candidates("CRVUSDT") == [("CRVUSDT", 1.0)]
    assert V.spot_candidates("1000PEPEUSDT") == [("1000PEPEUSDT", 1.0), ("PEPEUSDT", 1000.0)]
    assert V.spot_candidates("1000000BOBUSDT")[1] == ("BOBUSDT", 1e6)
    assert V.spot_candidates("1MBABYDOGEUSDT")[1] == ("BABYDOGEUSDT", 1e6)


def test_plan_keys_uses_monthly_for_complete_months_and_daily_otherwise():
    p = "data/futures/um/monthly/klines/X/1h/X-1h-"
    d = "data/futures/um/daily/klines/X/1h/X-1h-"
    monthly = [f"{p}2026-07.zip", f"{p}2026-07.zip.CHECKSUM", f"{p}2026-08.zip"]
    daily = [f"{d}2026-07-30.zip", f"{d}2026-09-01.zip", f"{d}2026-09-28.zip", f"{d}2026-09-29.zip"]
    start, today = 1785110400000, 1790640000000          # 2026-07-27, 2026-09-29
    assert V.plan_keys(monthly, daily, start, today) == sorted(
        [f"{p}2026-07.zip", f"{p}2026-08.zip", f"{d}2026-09-01.zip", f"{d}2026-09-28.zip"])
    # the monthly file is not out yet -> fall back to the month's daily files
    assert V.plan_keys([], daily, start, today)[0] == f"{d}2026-07-30.zip"


async def test_s3_listing_follows_next_marker():
    pages = {
        "": "<ListBucketResult><Prefix>a/</Prefix><IsTruncated>true</IsTruncated><NextMarker>a/B/</NextMarker>"
            "<CommonPrefixes><Prefix>a/A/</Prefix></CommonPrefixes>"
            "<CommonPrefixes><Prefix>a/B/</Prefix></CommonPrefixes></ListBucketResult>",
        "a/B/": "<ListBucketResult><Prefix>a/</Prefix><IsTruncated>false</IsTruncated>"
                "<CommonPrefixes><Prefix>a/C/</Prefix></CommonPrefixes></ListBucketResult>",
    }
    f = V.Fetcher(2)
    await f.client.aclose()
    f.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text=pages[req.url.params["marker"]])))
    keys, pre = await f.list("a/", delimiter="/")
    await f.close()
    assert keys == [] and pre == ["a/A/", "a/B/", "a/C/"]


def test_load60_shape_and_spot_multiplier(tmp_path):
    db_path = tmp_path / "hist.db"
    db = HD.connect(db_path)
    start = T0 - D
    raw = "1000PEPEUSDT"
    HD.put_bars(db, raw, "perp", V.parse_klines(FUT_KLINES), start)
    HD.put_bars(db, raw, "prem", V.parse_klines(PREMIUM), start)
    HD.put_bars(db, raw, "spot", V.parse_klines(SPOT_KLINES), start)
    HD.put_metrics(db, raw, V.metrics_hourly(V.parse_metrics(METRICS)), start)
    HD.put_metrics(db, raw, {T0 + 3 * H: {"oi": 1.0, "oi_val": 2.0}}, start, merge=True)   # REST tail: OI only
    HD.put_funding(db, raw, V.parse_funding(FUNDING) + [[T0, 0.0002]], start)             # old rows dropped
    meta = {"symbol": "1000PEPE/USDT:USDT", "raw": raw, "status": "SETTLING", "onboard_ms": 1683244800000,
            "delist_ms": T0 + D, "tags": ["Meme"], "spot_raw": "PEPEUSDT", "spot_mult": 1000.0}
    db.execute("INSERT INTO coins VALUES (?,?,?)", (raw, json.dumps(meta), T0))
    db.commit()
    [c] = HD.load60(db_path)
    assert set(c) == {"symbol", "onboard_ms", "delist_ms", "tags", "perp", "oi", "premium", "funding", "spot",
                      "spot_mult", "ls_top", "ls_top_acct", "ls_global", "taker_ratio"}
    assert c["symbol"] == "1000PEPE/USDT:USDT" and c["delist_ms"] == T0 + D and c["tags"] == ["Meme"]
    assert set(c["perp"][0]) == {"ts", "o", "h", "l", "c", "qv", "n", "tbq"} and c["perp"][0]["ts"] == T0
    assert c["spot"][0]["c"] == 0.3429 * 1000 and c["spot"][0]["h"] == 0.3454 * 1000 and c["spot_mult"] == 1000.0
    assert c["spot"][0]["qv"] == 84678.55966                      # volume is not rescaled
    assert c["premium"] == {T0: -0.00000055}
    assert c["oi"][T0 + H] == 23997451.34797391 and c["oi"][T0 + 3 * H] == 2.0
    assert T0 + 3 * H not in c["ls_top"] and c["ls_top"][T0 + H] == 1.908198
    assert c["funding"] == [[T0, 0.0002]]
    assert HD.load60(db_path, symbols=["CRV"]) == []
    s = HD.summary(db, T0, T0 + 3 * H)
    assert s["coins"] == 1 and s["coverage_pct"]["spot"] == 100.0


def test_trim_dead_cuts_flat_bars_after_delisting_and_purges_long_dead_coins(tmp_path):
    db = HD.connect(tmp_path / "hist.db")
    start = T0 - D

    def bar(ts, n):
        return {"ts": ts, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "qv": 10.0 * n, "n": n, "tbq": 0.0}
    live = [bar(T0 + i * H, 5 if i < 3 else 0) for i in range(6)]    # trades stop after hour 2, flat bars follow
    for raw in ("ACXUSDT", "AGIXUSDT", "GONEUSDT"):
        HD.put_bars(db, raw, "perp", live if raw != "AGIXUSDT" else [bar(T0, 0)], start)
        HD.put_metrics(db, raw, {T0 + 5 * H: {"oi_val": 1.0}}, start)
    acx = {"status": "SETTLING", "deliveryDate": str(T0 + 3 * H)}
    assert HD.trim_dead(db, "ACXUSDT", acx, start) == T0 + 3 * H
    assert [r[0] for r in db.execute("SELECT ts FROM bars WHERE symbol='ACXUSDT'")] == [T0, T0 + H, T0 + 2 * H]
    assert db.execute("SELECT COUNT(*) FROM metrics_h WHERE symbol='ACXUSDT'").fetchone()[0] == 0
    assert HD.trim_dead(db, "AGIXUSDT", {"status": "SETTLING", "deliveryDate": "1719306000000"}, start) == 0
    assert db.execute("SELECT COUNT(*) FROM bars WHERE symbol='AGIXUSDT'").fetchone()[0] == 0
    assert HD.trim_dead(db, "GONEUSDT", None, start) == T0 + 3 * H              # not in exchangeInfo: last trade + 1h
    assert HD.trim_dead(db, "GONEUSDT", {"status": "TRADING"}, start) is None


def test_coin_perps_with_non_ascii_names_are_in_the_universe():
    """Binance lists coin perps named in Chinese (牛来USDT, 我踏马来了USDT...): the live scanner ranks them, so the
    history must hold them too (task #73; the ASCII-only pattern dropped them from hist.db and daily.db)."""
    coin = {"contractType": "PERPETUAL", "underlyingType": "COIN", "quoteAsset": "USDT"}
    assert HD._is_coin_perp("牛来USDT", None) and HD._is_coin_perp("牛来USDT", coin)
    assert HD._is_coin_perp("1000PEPEUSDT", coin) and HD._is_coin_perp("ETHUSDT", None)
    assert not HD._is_coin_perp("BTCUSDT_261225", None)                 # quarterly
    assert not HD._is_coin_perp("BTCDOMUSDT", coin)                     # composite index
    assert not HD._is_coin_perp("XAUUSDT", {**coin, "underlyingType": "COMMODITY"})
    assert not HD._is_coin_perp("ETHBTC", None) and not HD._is_coin_perp("USDT", None)

    class F:
        async def list(self, prefix, marker="", delimiter=None, max_keys=None):
            return [], [f"{prefix}{r}/" for r in ("牛来USDT", "AAAUSDT", "BTCUSDT_261225")]
    assert asyncio.run(HD.universe(F(), {"BBBUSDT": {**coin, "status": "TRADING"}})) == [
        "AAAUSDT", "BBBUSDT", "牛来USDT"]
