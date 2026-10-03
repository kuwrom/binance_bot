"""The stream collector as a guardian task (#72): its own thread and event loop, so a crash, a dead socket or a
blocking store never slows the protective lane; the liquid universe and the backfill go through the guardian's
exchange client tagged research; housekeeping prunes the store."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from shortbot import guardian as gmod
from shortbot import stream_task as T
from shortbot import weight
from shortbot.market import streams as S
from tests.fakes import FakeCcxt
from tests.test_guardian import guardian_with, kinds
from tests.test_guardian_loop import fast_guardian

A, B, C = "AAA/USDT:USDT", "BBB/USDT:USDT", "CCC/USDT:USDT"


class Ex:
    def __init__(self, vols):
        self.vols, self.seen = vols, []
        self.public = SimpleNamespace(fetch_tickers=self.fetch_tickers, fapiPublicGetKlines=self.klines)

    async def coin_perps(self):
        return {s: {} for s in (A, B, C)}

    async def fetch_tickers(self):
        self.seen.append(("tickers", weight.current()))
        return {s: {"symbol": s, "quoteVolume": v} for s, v in self.vols.items()}

    async def klines(self, params):
        """Raw /fapi/v1/klines rows (12 fields, numbers as strings), as Binance answers."""
        self.seen.append(("klines", weight.current(), asyncio.get_running_loop(), threading.get_ident(), params))
        t = params["startTime"]
        return [[t, "1", "1", "1", "1", "1", t + 899_999, "1", 3, "0.5", "0.5", "0"]]


async def test_liquid_universe_is_the_5m_volume_floor_tagged_research():
    ex = Ex({A: 6e6, B: 4.9e6, C: 50e6, "DDD/USDT:USDT": 99e6})    # DDD isn't a coin perp on this account
    assert await T.liquid_universe(ex) == [C, A]                     # by volume, floor $5M
    assert ex.seen == [("tickers", weight.RESEARCH)]


async def until(cond, timeout=2.0):
    t0 = time.monotonic()
    while not cond():
        assert time.monotonic() - t0 < timeout, "timed out"
        await asyncio.sleep(0.01)


async def test_collector_runs_in_its_own_thread_and_backfills_on_the_guardian_loop(tmp_path):
    ex, got = Ex({A: 6e6}), {}

    class Collector:
        def __init__(self, store, symbols, backfill=None):
            self.backfill = backfill
            got.update(symbols=symbols, store=store)

        async def run(self):
            got["thread"] = threading.get_ident()
            got["rows"] = await self.backfill(A, "15m", 123, 99)
            await asyncio.Event().wait()

    task = asyncio.create_task(T.StreamTask(ex, tmp_path / "streams.db", collector=Collector).run())
    await until(lambda: "rows" in got)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _, cls, loop, tid, params = next(s for s in ex.seen if s[0] == "klines")
    assert got["symbols"] == [A] and got["rows"][0][:6] == [123, "1", "1", "1", "1", "1"]
    assert params == {"symbol": "AAAUSDT", "interval": "15m", "startTime": 123, "limit": 99}
    assert cls == weight.RESEARCH and loop is asyncio.get_running_loop() and tid == threading.get_ident()
    assert got["thread"] != threading.get_ident() and isinstance(got["store"], S.StreamStore)


async def test_the_real_collector_fills_the_store_from_its_thread(tmp_path):
    step = S.TF_MS["15m"]
    cur = int(time.time() * 1000) // step * step
    k = {"t": cur, "T": cur + step - 1, "s": "AAAUSDT", "i": "15m", "o": "1", "c": "1.1", "h": "1.2", "l": "0.9",
         "v": "10", "q": "11", "Q": "5", "x": False}
    msgs = [{"stream": "aaausdt@kline_15m", "data": {"e": "kline", "s": "AAAUSDT", "k": k}},
            {"stream": "!markPrice@arr@1s", "data": [{"s": "AAAUSDT", "p": "1.1", "i": "1.0", "r": "0.0001", "T": 0}]}]

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def recv(self):
            await asyncio.sleep(0 if msgs else 0.05)
            return json.dumps(msgs.pop(0) if msgs else {"stream": "!markPrice@arr@1s", "data": []})

    def collector(store, symbols, backfill=None):
        return S.StreamCollector(store, symbols, connect=lambda url: Socket(), backfill=backfill)
    ex, db = Ex({A: 6e6}), tmp_path / "streams.db"
    task = asyncio.create_task(T.StreamTask(ex, db, collector=collector).run())
    await until(lambda: S.fresh_symbols("kline_15m", 90, db) == ["AAAUSDT"] and S.latest("mark", db), timeout=5)
    await until(lambda: any(s[0] == "klines" for s in ex.seen))     # the gap backfill, via the guardian's loop
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(s[1] == weight.RESEARCH and s[3] == threading.get_ident() for s in ex.seen if s[0] == "klines")


async def test_a_universe_change_restarts_the_collector(tmp_path):
    ex, made, stopped = Ex({A: 6e6}), [], []

    class Collector:
        def __init__(self, store, symbols, backfill=None):
            self.symbols = symbols
            made.append(symbols)

        async def run(self):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(self.symbols)
    task = asyncio.create_task(T.StreamTask(ex, tmp_path / "streams.db", collector=Collector, refresh_s=0.05).run())
    await until(lambda: made == [[A]])
    await asyncio.sleep(0.12)                     # refreshes with the same universe: no reconnect
    assert made == [[A]]
    ex.vols[B] = 7e6
    await until(lambda: made == [[A], [B, A]] and stopped == [[A]])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_failed_universe_refresh_keeps_streaming(tmp_path):
    ex, made = Ex({A: 6e6}), []

    class Collector:
        def __init__(self, store, symbols, backfill=None):
            made.append(symbols)

        async def run(self):
            await asyncio.Event().wait()
    task = asyncio.create_task(T.StreamTask(ex, tmp_path / "streams.db", collector=Collector, refresh_s=0.03).run())
    await until(lambda: made == [[A]])

    async def down():
        raise RuntimeError("418")
    ex.public.fetch_tickers = down
    await asyncio.sleep(0.15)
    assert made == [[A]] and not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def lane_gaps(passes):
    return max(b - a for a, b in zip(passes, passes[1:]))


async def run_guardian_with(monkeypatch, tmp_path, collector):
    g = fast_guardian(monkeypatch)
    g.streams_db = tmp_path / "streams.db"
    passes = []

    async def entries():
        passes.append(time.monotonic())

    async def universe(ex):
        return [A]
    g.check_entries = entries
    monkeypatch.setattr(T, "liquid_universe", universe)
    monkeypatch.setattr(T, "StreamCollector", collector)
    task = asyncio.create_task(g.run())
    await asyncio.sleep(0.4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "guardian:streams" in {t.get_name() for t in g._tasks}
    return g, passes


async def test_a_crashing_collector_never_slows_the_protective_lane(monkeypatch, tmp_path):
    class Crash:
        def __init__(self, store, symbols, backfill=None):
            pass

        async def run(self):
            raise RuntimeError("collector bug")
    g, passes = await run_guardian_with(monkeypatch, tmp_path, Crash)
    assert len(passes) >= 15 and lane_gaps(passes) < 0.1
    assert "guardian_task_failed:streams" in kinds(g)                 # supervised: alerted and restarted


async def test_a_blocking_collector_never_slows_the_protective_lane(monkeypatch, tmp_path):
    """A SQLite busy-wait or a JSON burst is synchronous: on the guardian's loop it would stall every lane."""
    class Blocking:
        def __init__(self, store, symbols, backfill=None):
            pass

        async def run(self):
            while True:
                time.sleep(0.15)
                await asyncio.sleep(0)
    g, passes = await run_guardian_with(monkeypatch, tmp_path, Blocking)
    assert len(passes) >= 15 and lane_gaps(passes) < 0.1


async def test_a_dead_socket_never_slows_the_protective_lane(monkeypatch, tmp_path):
    class Refused:
        async def __aenter__(self):
            raise ConnectionError("refused")

        async def __aexit__(self, *a):
            return False

    def collector(store, symbols, backfill=None):
        return S.StreamCollector(store, symbols, connect=lambda url: Refused(), backfill=backfill)
    g, passes = await run_guardian_with(monkeypatch, tmp_path, collector)
    assert len(passes) >= 15 and lane_gaps(passes) < 0.1
    assert "guardian_task_failed:streams" not in kinds(g)            # a drop is the collector's own backoff


async def test_no_stream_task_or_scan_for_an_in_memory_journal(monkeypatch):
    g = fast_guardian(monkeypatch)
    assert g.streams_db is None and S.path_for(":memory:") is None
    task = asyncio.create_task(g.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "guardian:streams" not in {t.get_name() for t in g._tasks}
    assert await g.tiered.step() is None


def test_the_scan_lane_is_research_class():
    g = guardian_with(FakeCcxt())
    lanes = g._lanes()
    assert [j.name for j in lanes["scan"].jobs] == ["scan", "cadence_shadow", "mtf_views"]
    assert weight.class_for("scan", "scan") == weight.RESEARCH == weight.class_for("scan", "cadence_shadow")
    assert gmod.SCAN["scan"][0] <= 15                    # fast events decay in 10-45 min: check often


async def test_housekeeping_prunes_the_stream_store(tmp_path, monkeypatch):
    g = guardian_with(FakeCcxt())
    g.streams_db = tmp_path / "streams.db"
    step = S.TF_MS["15m"]
    now = int(time.time() * 1000) // step * step
    st = S.StreamStore(g.streams_db)
    st.bars("AAA/USDT:USDT", "15m", [[now - k * step, 1, 1, 1, 1, 1] for k in range(S.KEEP["15m"] + 10)], now)
    await g.housekeeping()
    n = st.db.execute("SELECT COUNT(*), MIN(ts) FROM candles").fetchone()
    assert n[0] == S.KEEP["15m"] and n[1] > now - S.KEEP["15m"] * step
    g.streams_db = None
    await g.housekeeping()                                # no store: nothing to prune, no error
