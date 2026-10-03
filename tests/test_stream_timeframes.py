"""Every timeframe the code reads comes from the stream store (2026-10-03): taker-buy volume from the stream and from
raw-kline backfills, the flow reader, 30m built from 5m, an old store migrated, and the hub serving flow candles."""

import asyncio
import sqlite3

from shortbot.market import stream_read as R
from shortbot.market import streams as S

M5, M30 = S.TF_MS["5m"], S.TF_MS["30m"]
NOW = 1_790_000_000_000 // M30 * M30 + 7 * M5 + 1000          # 35 minutes into a 30m bucket


def kmsg(sym, tf, ts, px, closed, v=10.0, V=4.0):
    return {"stream": f"{sym.lower()}@kline_{tf}", "data": {"e": "kline", "s": sym, "k": {
        "t": ts, "T": ts + S.TF_MS[tf] - 1, "s": sym, "i": tf, "o": str(px), "c": str(px + 1), "h": str(px + 2),
        "l": str(px - 1), "v": str(v), "q": str(v * px), "V": str(V), "Q": str(V * px), "x": closed}}}


def raw_kline(ts, px, v=10.0, V=4.0):
    """A /fapi/v1/klines row: 12 fields, numbers as strings."""
    return [ts, str(px), str(px + 2), str(px - 1), str(px + 1), str(v), ts + M5 - 1, str(v * px), 7, str(V),
            str(V * px), "0"]


def five_minute_store(tmp_path, n=40):
    st = S.StreamStore(tmp_path / "s.db")
    start = NOW // M5 * M5 - (n - 1) * M5
    for i in range(n - 1):                                      # closed bars, then the forming one
        S.handle(st, kmsg("ETHUSDT", "5m", start + i * M5, 100.0 + i, True, v=10 + i, V=4 + i), NOW)
    S.handle(st, kmsg("ETHUSDT", "5m", start + (n - 1) * M5, 200.0, False), NOW)
    st.flush()
    return st, start


def test_stream_bars_carry_taker_base_volume_and_flow_reader(tmp_path):
    st, start = five_minute_store(tmp_path)
    flow = R.flow_candles("ETH/USDT:USDT", "5m", 5, tmp_path / "s.db", now_ms=NOW)
    assert [r[0] for r in flow] == [NOW // M5 * M5 - k * M5 for k in (4, 3, 2, 1, 0)]
    assert flow[-2][5:] == [10 + 38, 4 + 38]                          # v and taker-buy base of the last closed bar (i = 38)
    assert S.candles("ETHUSDT", "5m", 5, tmp_path / "s.db", now_ms=NOW) == [r[:6] for r in flow]


def test_raw_backfill_carries_taker_volume_but_ccxt_rows_do_not(tmp_path):
    st = S.StreamStore(tmp_path / "s.db")
    t0 = NOW // M5 * M5 - 10 * M5
    st.bars("SOLUSDT", "5m", [raw_kline(t0 + i * M5, 50.0 + i) for i in range(11)], NOW)
    flow = R.flow_candles("SOLUSDT", "5m", 4, tmp_path / "s.db", now_ms=NOW)
    assert flow is not None and flow[-1][6] == 4.0 and flow[-1][5] == 10.0
    st.bars("ADAUSDT", "5m", [[t0 + i * M5, 1, 2, 0.5, 1.5, 9] for i in range(11)], NOW)
    assert R.flow_candles("ADAUSDT", "5m", 4, tmp_path / "s.db", now_ms=NOW) is None      # no taker volume: REST
    assert S.candles("ADAUSDT", "5m", 4, tmp_path / "s.db", now_ms=NOW) is not None


def test_thirty_minutes_are_built_from_five(tmp_path):
    st, start = five_minute_store(tmp_path)
    bars5 = S.candles("ETHUSDT", "5m", 39, tmp_path / "s.db", now_ms=NOW)
    got = S.candles("ETHUSDT", "30m", 3, tmp_path / "s.db", now_ms=NOW)
    b0 = NOW // M30 * M30 - M30                                      # the last complete 30m bucket
    six = [b for b in bars5 if b0 <= b[0] < b0 + M30]
    assert got[-2] == [b0, six[0][1], max(b[2] for b in six), min(b[3] for b in six), six[-1][4],
                       sum(b[5] for b in six)]
    assert got[-1][0] == b0 + M30                                     # the forming 30m bar is last, like REST
    flow5 = R.flow_candles("ETHUSDT", "5m", 39, tmp_path / "s.db", now_ms=NOW)
    flow30 = R.flow_candles("ETHUSDT", "30m", 2, tmp_path / "s.db", now_ms=NOW)
    assert flow30[-2][6] == sum(b[6] for b in flow5 if b0 <= b[0] < b0 + M30)                 # taker volume sums
    assert S.candles("ETHUSDT", "30m", 50, tmp_path / "s.db", now_ms=NOW) is None         # more than the store has


def test_an_old_store_gains_the_taker_column(tmp_path):
    db = sqlite3.connect(tmp_path / "s.db")
    db.execute("CREATE TABLE candles (symbol TEXT, tf TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, "
               "q REAL, tbq REAL, closed INTEGER, PRIMARY KEY (symbol, tf, ts)) WITHOUT ROWID")
    db.commit()
    db.close()
    S.StreamStore(tmp_path / "s.db")
    cols = [r[1] for r in sqlite3.connect(tmp_path / "s.db").execute("PRAGMA table_info(candles)")]
    assert cols[-1] == "tbv"


def test_every_readable_timeframe_and_its_backfill_depth():
    assert set(S.READABLE) == {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}
    assert S.BACKFILL["5m"] >= 288 and S.BACKFILL["15m"] >= 120 and S.BACKFILL["1h"] >= 170 and S.BACKFILL["1d"] >= 400
    assert all(S.KEEP[tf] >= S.BACKFILL[tf] for tf in S.TFS)


def test_the_hub_serves_flow_candles_from_the_store(tmp_path):
    from shortbot import clock
    from shortbot.market.hub import DataHub
    st, start = five_minute_store(tmp_path)
    st.db.execute("UPDATE fresh SET ts=? WHERE stream='kline_5m'", (NOW - 1000,))
    st.db.commit()

    class Ex:
        public = None

        async def flow_klines(self, symbol, tf, limit):
            raise AssertionError("REST must not be called while the store is fresh")
    hub = DataHub(Ex())
    hub.streams_db = tmp_path / "s.db"
    ctx = hub.context("ETH/USDT:USDT")
    with clock.at(NOW):
        rows = asyncio.run(ctx.flow_candles("5m", 5))
    assert len(rows) == 5 and len(rows[0]) == 7 and ctx.sources["flow_5m"] == "stream"
