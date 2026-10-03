"""market/streams.py against a fake combined-stream socket shaped like Binance's (measured 2026-10-02)."""

import asyncio
import json

import pytest

from shortbot.market import stream_read as R
from shortbot.market import streams as S

M15 = S.TF_MS["15m"]
T0 = 1_759_400_000_000 // M15 * M15


def kmsg(sym, ts, c, closed, tf="15m"):
    return {"stream": f"{sym.lower()}@kline_{tf}", "data": {"e": "kline", "E": ts, "s": sym, "k": {
        "t": ts, "T": ts + S.TF_MS[tf] - 1, "s": sym, "i": tf, "o": str(c), "c": str(c), "h": str(c + 1),
        "l": str(c - 1), "v": "10", "n": 5, "x": closed, "q": "100", "V": "4", "Q": "40", "B": "0"}}}


class FakeWS:
    def __init__(self, msgs, clock, drop_after=None):
        self.msgs, self.clock, self.drop_after, self.n = list(msgs), clock, drop_after, 0

    async def __aenter__(self):   # a real connect: the clock is at the first message
        if self.msgs:
            self.clock.t = self.msgs[0][0] / 1000
        return self

    async def __aexit__(self, *a):
        return False

    async def recv(self):
        await asyncio.sleep(0)   # a real socket awaits the network
        if self.drop_after is not None and self.n >= self.drop_after or not self.msgs:
            raise ConnectionError("dropped")
        self.n += 1
        ts, msg = self.msgs.pop(0)
        self.clock.t = ts / 1000
        return json.dumps(msg)


class Clock:
    t = T0 / 1000

    def __call__(self):
        return self.t


def store(tmp_path):
    return S.StreamStore(tmp_path / "streams.db"), tmp_path / "streams.db"


def test_parses_all_market_and_kline_messages(tmp_path):
    st, p = store(tmp_path)
    S.handle(st, {"stream": "!markPrice@arr@1s", "data": [{"e": "markPriceUpdate", "s": "BTCUSDT", "p": "101",
                                                           "i": "100", "P": "101", "r": "0.0001", "T": 1}]}, T0)
    S.handle(st, {"stream": "!ticker@arr", "data": [{"s": "BTCUSDT", "P": "5.5", "c": "101", "q": "1e9",
                                                     "h": "105", "l": "95"}]}, T0)
    assert S.handle(st, {"stream": "x", "data": {}}, T0) is None
    st.flush()
    m = R.latest("mark", p, now_ms=T0)["BTCUSDT"]
    assert m["premium"] == pytest.approx(0.01) and m["funding"] == 0.0001
    assert R.latest("ticker", p, now_ms=T0)["BTCUSDT"]["pct"] == 5.5
    assert R.latest("ticker", p, now_ms=T0 + 200_000) == {}            # older than max_age_s


def test_candles_fresh_gapped_and_stale(tmp_path):
    st, p = store(tmp_path)
    for i in range(5):
        S.handle(st, kmsg("ETHUSDT", T0 + i * M15, 10 + i, True), T0 + (i + 1) * M15)
    S.handle(st, kmsg("ETHUSDT", T0 + 5 * M15, 15, False), T0 + 5 * M15 + 1000)
    st.flush()
    now = T0 + 5 * M15 + 2000
    got = R.candles("ETH/USDT:USDT", "15m", 4, p, now_ms=now)
    assert [r[0] for r in got] == [T0 + i * M15 for i in (2, 3, 4, 5)] and got[-1][4] == 15
    assert R.candles("ETHUSDT", "15m", 7, p, now_ms=now) is None        # window longer than the data
    assert R.candles("ETHUSDT", "15m", 4, p, now_ms=now + 3 * M15) is None   # stale: last close 3 bars old
    st.db.execute("DELETE FROM candles WHERE ts=?", (T0 + 3 * M15,))
    st.db.commit()
    assert R.candles("ETHUSDT", "15m", 4, p, now_ms=now) is None        # a gap inside the window
    assert R.candles("ETHUSDT", "15m", 2, p, now_ms=now) is not None    # the gap is outside a short window
    assert R.candles("ETHUSDT", "15m", 4, tmp_path / "missing.db") is None


def test_disconnect_then_gap_backfill_once(tmp_path):
    st, p = store(tmp_path)
    clock = Clock()
    # two closed bars are stored; connection 1 sees the forming bar 2 and drops; connection 2 opens 3 bars later
    for i in range(2):
        S.handle(st, kmsg("SOLUSDT", T0 + i * M15, 10 + i, True), T0 + (i + 1) * M15)
    st.flush()
    first = [(T0 + 2 * M15 + 1000, kmsg("SOLUSDT", T0 + 2 * M15, 12, False))]
    later = T0 + 5 * M15 + 1000
    second = [(later, kmsg("SOLUSDT", T0 + 5 * M15, 15, False))]
    socks = iter([FakeWS(first, clock), FakeWS(second, clock)])
    calls = []

    async def backfill(sym, tf, since, limit):
        calls.append((sym, tf, since, limit))
        return [[since + i * M15, 12 + i, 13 + i, 11 + i, 12 + i, 1] for i in range(3)]   # bars 2, 3, 4

    col = S.StreamCollector(st, ["SOL/USDT:USDT"], tfs=("15m",), connect=lambda url: next(socks),
                            backfill=backfill, clock=clock)
    names = S.groups(col.symbols, col.tfs)[0]
    with pytest.raises(ConnectionError):
        asyncio.run(col.run_one(names))
    with pytest.raises(ConnectionError):
        asyncio.run(col.run_one(names))
    assert calls == [("SOL/USDT:USDT", "15m", T0 + 2 * M15, S.BACKFILL["15m"])]   # only the missing bars
    got = R.candles("SOLUSDT", "15m", 6, p, now_ms=later)
    assert [r[0] for r in got] == [T0 + i * M15 for i in range(6)] and got[-1][4] == 15
    asyncio.run(col.fill_gaps(col.symbols))
    assert len(calls) == 1                                               # no gap left, nothing re-fetched


def test_failed_backfill_is_not_retried_and_leaves_none(tmp_path):
    st, p = store(tmp_path)
    clock = Clock()
    clock.t = (T0 + 10 * M15) / 1000

    calls = []

    async def boom(*a):
        calls.append(a)
        raise TimeoutError("rest down")

    col = S.StreamCollector(st, ["XRPUSDT"], tfs=("15m",), backfill=boom, clock=clock)
    asyncio.run(col.fill_gaps(col.symbols))
    asyncio.run(col.fill_gaps(col.symbols))
    assert len(calls) == 1
    assert R.candles("XRPUSDT", "15m", 3, p, now_ms=T0 + 10 * M15) is None


def test_groups_respect_the_stream_cap():
    syms = [f"C{i}USDT" for i in range(183)]
    gs = S.groups(syms)
    assert sum(map(len, gs)) == 2 + 183 * len(S.TFS) and all(len(g) <= S.MAX_STREAMS_PER_CONN for g in gs)
    assert gs[0][:2] == list(S.ALL_MARKET) and "c0usdt@kline_15m" in gs[0]
