"""Tier 0 of the scanner as a guardian task (docs/SCANNING.md, #72): the stream collector (market/streams.py) over the
liquid universe, wired like the LiquidationRecorder (a supervised task) but on its own thread and event loop.

Why a thread: the collector's work is synchronous. It parses ~1,400 kline messages a second and commits the store
once a second, and a SQLite busy-wait can block for up to 15 s. On the guardian's loop any of that stalls every lane,
the protective one included (guardian_loop.py). In its own thread a crash, a dead socket or a blocked store
stays there.

- Universe: coin perps with 24h quote volume >= LIQUID_MIN_QV ($5M, ~183 coins; below that the backtest loses,
  docs/BACKTEST.md). It is refreshed every UNIVERSE_REFRESH_S, and the collector restarts only when the set changed.
  A failed refresh keeps the current set.
- Backfill after each (re)connect: raw `/fapi/v1/klines` (taker volumes included), tagged research. ccxt's client lives on the guardian's
  loop, so the call is sent there (run_coroutine_threadsafe) and the collector's thread awaits the answer.
- The store (data/streams.db beside the journal) is opened inside the thread; housekeeping prunes it (`prune`).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import functools
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from . import weight
from .market.streams import StreamCollector, StreamStore, raw_id

log = logging.getLogger("shortbot.streams")
LIQUID_MIN_QV = 5e6
UNIVERSE_REFRESH_S = 6 * 3600   # each change reconnects both sockets: at most 4 times a day
UNIVERSE_RETRY_S = 60
BACKFILL_WAIT_S = 60            # a backfill call yields at most this long to protective calls, then the gap waits
STOP_WAIT_S = 30                # a replaced collector's last flush, before the next one opens the store


async def liquid_universe(ex) -> list[str]:
    """Coin perps by 24h quote volume, descending, at or above LIQUID_MIN_QV (ccxt symbols)."""
    with weight.tagged(weight.RESEARCH):
        perps = await ex.coin_perps()
        tickers = await ex.public.fetch_tickers()
    vol = {s: float(t.get("quoteVolume") or 0) for s, t in tickers.items() if s in perps}
    return [s for s in sorted(vol, key=lambda s: -vol[s]) if vol[s] >= LIQUID_MIN_QV]


class ThreadLoop:
    """One coroutine on its own event loop in a daemon thread. `done` (a concurrent future) resolves when it ends,
    with its exception if it failed; `stop()` cancels it from any thread and never waits."""

    def __init__(self, name: str, factory: Callable[[], Coroutine[Any, Any, Any]]):
        self.done: concurrent.futures.Future = concurrent.futures.Future()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._stopped = False
        self.thread = threading.Thread(target=self._main, args=(factory,), name=name, daemon=True)
        self.thread.start()

    def _main(self, factory: Callable[[], Coroutine[Any, Any, Any]]) -> None:
        loop = asyncio.new_event_loop()
        try:
            with self._lock:
                if self._stopped:
                    self.done.set_result(None)
                    return
                self._loop, self._task = loop, loop.create_task(factory())
            loop.run_until_complete(self._task)
            self.done.set_result(None)
        except asyncio.CancelledError:
            self.done.set_result(None)
        except BaseException as e:  # noqa: BLE001 - handed to the owner (supervised: alert and restart)
            self.done.set_exception(e)
        finally:
            with self._lock:
                self._loop = None
            _close(loop)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            if self._loop is not None and self._task is not None:
                with contextlib.suppress(RuntimeError):   # the loop is closing anyway
                    self._loop.call_soon_threadsafe(self._task.cancel)


def _close(loop: asyncio.AbstractEventLoop) -> None:
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        loop.close()


class StreamTask:
    """The guardian's long-running tier-0 task: universe -> collector thread -> refresh -> restart on change. A
    collector that dies raises here, so `supervised` alerts and restarts it; a dropped socket is the collector's
    own backoff and never surfaces."""

    def __init__(self, ex, path: Path | str, collector: Callable[..., Any] | None = None,
                 refresh_s: float = UNIVERSE_REFRESH_S):
        self.ex, self.path, self.refresh_s = ex, Path(path), refresh_s
        self.collector = collector or StreamCollector
        self.symbols: list[str] = []

    async def run(self) -> None:
        main = asyncio.get_running_loop()
        self.symbols = await self._first_universe()
        while True:
            log.info("stream collector: %d coins", len(self.symbols))
            th = ThreadLoop("streams", functools.partial(self._collect, list(self.symbols), main))
            done = asyncio.wrap_future(th.done)
            try:
                while True:
                    finished, _ = await asyncio.wait({done}, timeout=self.refresh_s)
                    if finished:
                        done.result()   # its error, for supervised
                        raise RuntimeError("stream collector stopped")
                    new = await self._universe()
                    if new and set(new) != set(self.symbols):
                        log.info("stream universe changed: +%s -%s", sorted(set(new) - set(self.symbols)),
                                 sorted(set(self.symbols) - set(new)))
                        self.symbols = new
                        break
            finally:
                th.stop()
            await asyncio.wait({done}, timeout=STOP_WAIT_S)

    async def _collect(self, symbols: list[str], main: asyncio.AbstractEventLoop) -> None:
        """In the collector's thread: its own store connection, backfill through the guardian's loop."""
        store = StreamStore(self.path)
        try:
            await self.collector(store, symbols, backfill=self._bridge(main)).run()
        finally:
            store.db.close()

    def _bridge(self, main: asyncio.AbstractEventLoop):
        async def backfill(symbol: str, tf: str, since_ms: int, limit: int) -> list[list[float]]:
            fut = asyncio.run_coroutine_threadsafe(self._fetch(symbol, tf, since_ms, limit), main)
            return await asyncio.wrap_future(fut)
        return backfill

    async def _fetch(self, symbol: str, tf: str, since_ms: int, limit: int) -> list[list[Any]]:
        """On the guardian's loop (ccxt's client is bound to it): research, yielding to protective calls. Raw klines,
        so backfilled bars carry their quote and taker-buy volumes (the flow features read them)."""
        with weight.tagged(weight.RESEARCH, wait_s=BACKFILL_WAIT_S):
            return await self.ex.public.fapiPublicGetKlines({"symbol": raw_id(symbol), "interval": tf,
                                                             "startTime": since_ms, "limit": limit})

    async def _universe(self) -> list[str] | None:
        try:
            return await liquid_universe(self.ex)
        except Exception as e:  # noqa: BLE001 - keep streaming the current set
            log.warning("stream universe refresh failed (%s: %s)", type(e).__name__, e)
            return None

    async def _first_universe(self) -> list[str]:
        while True:
            syms = await self._universe()
            if syms:
                return syms
            await asyncio.sleep(UNIVERSE_RETRY_S)


def prune(path: Path | str | None, now_ms: int | None = None) -> int:
    """Housekeeping: drop bars older than KEEP[tf] on a connection of its own (run it in a worker thread)."""
    if path is None or not Path(path).exists():
        return 0
    st = StreamStore(path)
    try:
        return st.prune(now_ms if now_ms is not None else int(time.time() * 1000))  # wall-clock: retention
    finally:
        st.db.close()
