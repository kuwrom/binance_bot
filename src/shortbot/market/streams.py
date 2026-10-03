"""Tier 0 of the scanner (docs/SCANNING.md): Binance market streams into a local store, at zero REST weight.

Measured 2026-10-02 on wss://fstream.binance.com/market/stream:
- `!ticker@arr`: ~1 msg/s with the symbols that changed;
- `!markPrice@arr@1s`: mark, index, funding rate and next funding time for all contracts;
- `<sym>@kline_<tf>`: a 250 ms update of the live bar, whose `x` flag marks the close.

Binance docs (websocket-market-streams/Connect, read 2026-10-02):
- a connection carries at most 1024 streams and lives 24 h;
- the server pings every 3 min and drops a connection that sends no pong within 10 min (websockets pongs on its own);
- clients may send 10 messages/s (we send none: streams go in the URL);
- "IPs that are repeatedly disconnected may be banned", hence the exponential backoff.

Timeframes: every one the live code reads (docs/SCANNING.md "Timeframes"): 1m, 5m, 15m, 1h, 4h and 1d are
streamed; 30m is built from 5m on read (stream_read.py, exact). ~182 coins x 6 streams ≈ 4,200 msg/s, measured at
3.2 us each: ~13 ms of CPU per second, on the collector's own thread (stream_task.py).
Updates are buffered in memory and written once per second in a single transaction. Store: data/streams.db, its own
WAL file, with one writer (the collector) and readers that tolerate a missing file (stream_read.py). Rows are keyed
by the Binance id (BTCUSDT); readers also accept ccxt symbols (BTC/USDT:USDT). Bars carry the quote volume and the
taker-buy base and quote volumes, so the flow features read the store too.
After each (re)connect, a (symbol, tf) whose last closed bar is older than the previous bar is backfilled once over
REST, to the depth its readers use (BACKFILL, weight 1-2 per call).

    readers (market/stream_read.py): candles(symbol, tf, n), flow_candles(symbol, tf, n) -> rows or None
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import ssl
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("shortbot.streams")
BASE = "wss://fstream.binance.com/market/stream?streams="
DB_PATH = "data/streams.db"
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000,
         "1d": 86_400_000}
TFS = ("1m", "5m", "15m", "1h", "4h", "1d")   # streamed
DERIVED = {"30m": "5m"}                       # built on read from the base timeframe
READABLE = TFS + tuple(DERIVED)
# backfill depth = the deepest live reader, +margin (klines weight: < 100 bars 1, 100-499 2): flow 1m x60, flow and
# mark 5m x288, 15m x120 (tier 1) / x96, 1h x170 (market map), 4h x60, 1d x200 (BTC x400, risk budget)
BACKFILL = {"1m": 99, "5m": 300, "15m": 130, "1h": 200, "4h": 150, "1d": 400}
KEEP = {"1m": 2000, "5m": 900, "15m": 500, "30m": 0, "1h": 500, "4h": 300, "1d": 500}   # bars kept per timeframe
ALL_MARKET = ("!ticker@arr", "!markPrice@arr@1s")
MAX_STREAMS_PER_CONN = 200   # Binance allows 1024; two sockets halve what one drop loses
RECONNECT_S = 23 * 3600      # before Binance's 24 h cut, at a time of our choosing
RECV_TIMEOUT_S = 60          # markPrice@arr@1s ticks every second: a minute of silence is a dead socket
COLS = "symbol, tf, ts, o, h, l, c, v, q, tbq, closed, tbv"
SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (symbol TEXT, tf TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,
    q REAL, tbq REAL, closed INTEGER, tbv REAL, PRIMARY KEY (symbol, tf, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS latest (kind TEXT, symbol TEXT, ts INTEGER, data TEXT, PRIMARY KEY (kind, symbol))
    WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fresh (symbol TEXT, stream TEXT, ts INTEGER, PRIMARY KEY (symbol, stream)) WITHOUT ROWID;
"""


def raw_id(symbol: str) -> str:
    """BTC/USDT:USDT or btcusdt -> BTCUSDT."""
    return symbol.split(":")[0].replace("/", "").upper()


class StreamStore:
    """The writer (one per process: the collector). handle() fills buffers; flush() writes them in one commit."""

    def __init__(self, path: str | Path = DB_PATH):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=15)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        if "tbv" not in {r[1] for r in self.db.execute("PRAGMA table_info(candles)")}:   # a pre-2026-10-03 store
            self.db.execute("ALTER TABLE candles ADD COLUMN tbv REAL")
        self.bars_buf: dict[tuple[str, str, int], tuple] = {}
        self.latest_buf: dict[tuple[str, str], tuple[int, str]] = {}
        self.fresh_buf: dict[tuple[str, str], int] = {}

    def kline(self, k: dict[str, Any], now_ms: int) -> None:
        s = k["s"]
        self.bars_buf[(s, k["i"], int(k["t"]))] = (float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]),
                                                   float(k["v"]), float(k["q"]), float(k.get("Q") or 0),
                                                   int(bool(k["x"])), float(k.get("V") or 0))
        self.fresh_buf[(s, f"kline_{k['i']}")] = now_ms

    def latest(self, kind: str, symbol: str, data: dict[str, Any], now_ms: int) -> None:
        self.latest_buf[(kind, symbol)] = (now_ms, json.dumps(data))
        self.fresh_buf[(symbol, kind)] = now_ms

    def bars(self, symbol: str, tf: str, rows: list[list[Any]], now_ms: int) -> int:
        """REST rows written at once: all but a still-forming bar are closed. Raw Binance klines (12 fields, strings)
        carry the quote and taker volumes; ccxt OHLCV rows [ts, o, h, l, c, v] don't, and then a bar the stream
        already holds keeps its own."""
        step, sym = TF_MS[tf], raw_id(symbol)
        for r in rows:
            ts, ohlcv, closed = int(r[0]), tuple(float(x) for x in r[1:6]), int(int(r[0]) + step <= now_ms)
            if len(r) >= 11:
                self.db.execute(f"INSERT OR REPLACE INTO candles ({COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                (sym, tf, ts, *ohlcv, float(r[7]), float(r[10]), closed, float(r[9])))
            else:
                self.db.execute(f"INSERT INTO candles ({COLS}) VALUES (?,?,?,?,?,?,?,?,NULL,NULL,?,NULL) ON CONFLICT "
                                "DO UPDATE SET o=excluded.o, h=excluded.h, l=excluded.l, c=excluded.c, v=excluded.v, "
                                "closed=excluded.closed", (sym, tf, ts, *ohlcv, closed))
        self.db.commit()
        return len(rows)

    def flush(self) -> int:
        n = len(self.bars_buf) + len(self.latest_buf)
        if not n:
            return 0
        with self.db:
            self.db.executemany(f"INSERT OR REPLACE INTO candles ({COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                [(*key, *row) for key, row in self.bars_buf.items()])
            self.db.executemany("INSERT OR REPLACE INTO latest VALUES (?,?,?,?)",
                                [(*key, *row) for key, row in self.latest_buf.items()])
            self.db.executemany("INSERT OR REPLACE INTO fresh VALUES (?,?,?)",
                                [(*key, ts) for key, ts in self.fresh_buf.items()])
        self.bars_buf, self.latest_buf, self.fresh_buf = {}, {}, {}
        return n

    def last_closed(self, symbol: str, tf: str) -> int | None:
        return self.db.execute("SELECT MAX(ts) FROM candles WHERE symbol=? AND tf=? AND closed=1",
                               (raw_id(symbol), tf)).fetchone()[0]

    def prune(self, now_ms: int) -> int:
        """Keep the last KEEP[tf] bars per timeframe (by time); returns the bars deleted."""
        with self.db:
            return sum(self.db.execute("DELETE FROM candles WHERE tf=? AND ts < ?",
                                       (tf, now_ms - KEEP[tf] * TF_MS[tf])).rowcount for tf in TFS)


def handle(store: StreamStore, msg: dict[str, Any], now_ms: int) -> str | None:
    """One combined-stream message ({"stream", "data"}) into the store's buffers; returns the stream kind."""
    stream, data = msg.get("stream") or "", msg.get("data")
    if stream == "!ticker@arr" and isinstance(data, list):
        for t in data:
            store.latest("ticker", t["s"], {"pct": float(t["P"]), "last": float(t["c"]), "qv": float(t["q"]),
                                            "h": float(t["h"]), "l": float(t["l"])}, now_ms)
        return "ticker"
    if stream.startswith("!markPrice@arr") and isinstance(data, list):
        for m in data:
            mark, idx = float(m["p"]), float(m["i"])
            store.latest("mark", m["s"], {"mark": mark, "index": idx, "premium": mark / idx - 1 if idx else None,
                                          "funding": float(m["r"]) if m.get("r") not in (None, "") else None,
                                          "next_funding": int(m.get("T") or 0)}, now_ms)
        return "mark"
    if "@kline_" in stream and isinstance(data, dict) and "k" in data:
        store.kline(data["k"], now_ms)
        return "kline_" + data["k"]["i"]
    return None


def groups(symbols: list[str], tfs: tuple[str, ...] = TFS) -> list[list[str]]:
    """Stream names packed into connections of <= MAX_STREAMS_PER_CONN; the all-market streams ride the first."""
    names = list(ALL_MARKET) + [f"{raw_id(s).lower()}@kline_{tf}" for s in symbols for tf in tfs]
    return [names[i:i + MAX_STREAMS_PER_CONN] for i in range(0, len(names), MAX_STREAMS_PER_CONN)]


def gaps(store: StreamStore, symbols: list[str], tfs: tuple[str, ...], now_ms: int) -> list[tuple[str, str, int]]:
    """(symbol, tf, since_ms) whose last closed bar is older than the previous bar. `since` covers the missing bars,
    capped to the last BACKFILL[tf] bars (an older hole stays outside every window the readers serve)."""
    out = []
    for s in symbols:
        for tf in tfs:
            step = TF_MS[tf]
            current = now_ms // step * step
            last = store.last_closed(s, tf)
            if last is None or last < current - step:
                floor = current - (BACKFILL[tf] - 1) * step
                out.append((s, tf, max(last + step, floor) if last is not None else floor))
    return out


class StreamCollector:
    """Keeps every connection alive: exponential backoff after a drop, a fresh connection every RECONNECT_S. After
    each (re)connect, the connection's own symbols get one bounded backfill, run beside the receive loop so the
    socket is never left unread. `connect`, `backfill` and `clock` are injectable (tests)."""

    def __init__(self, store: StreamStore, symbols: list[str], tfs: tuple[str, ...] = TFS,
                 connect: Callable[[str], Any] | None = None,
                 backfill: Callable[[str, str, int, int], Awaitable[list[list[float]]]] | None = None,
                 clock: Callable[[], float] = time.time):
        self.store, self.symbols, self.tfs = store, list(symbols), tfs
        self.connect, self.backfill, self.clock = connect, backfill, clock
        self.counts: dict[str, int] = {}
        self.backfilled: set[tuple[str, str, int]] = set()

    def _symbols_of(self, names: list[str]) -> list[str]:
        ids = {n.split("@")[0].upper() for n in names if "@kline_" in n}
        return [s for s in self.symbols if raw_id(s) in ids]

    async def fill_gaps(self, symbols: list[str]) -> int:
        """Backfill each gap once (a failed call isn't retried: candles() stays None and the caller uses REST)."""
        if not self.backfill:
            return 0
        n = 0
        for s, tf, since in gaps(self.store, symbols, self.tfs, int(self.clock() * 1000)):
            if (s, tf, since) in self.backfilled:
                continue
            self.backfilled.add((s, tf, since))
            try:
                n += self.store.bars(s, tf, await self.backfill(s, tf, since, BACKFILL[tf]),
                                     int(self.clock() * 1000))
            except Exception as e:  # noqa: BLE001 - research data: log, leave the gap visible
                log.warning("backfill %s %s failed: %s", s, tf, e)
        return n

    async def run_one(self, names: list[str], lifetime_s: float = RECONNECT_S) -> None:
        """One connection, until it drops or lifetime_s passes."""
        started = self.clock()
        async with self._connect(BASE + "/".join(names)) as ws:
            bf = asyncio.create_task(self.fill_gaps(self._symbols_of(names)))
            last_flush = self.clock()
            try:
                while self.clock() - started < lifetime_s:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), RECV_TIMEOUT_S))
                    kind = handle(self.store, msg, int(self.clock() * 1000))
                    if kind:
                        self.counts[kind] = self.counts.get(kind, 0) + 1
                    if self.clock() - last_flush >= 1:
                        self.store.flush()
                        last_flush = self.clock()
                await bf
            finally:
                bf.cancel()
                self.store.flush()

    def _connect(self, url: str):
        if self.connect:
            return self.connect(url)
        import websockets
        return websockets.connect(url, ssl=_ssl(), ping_interval=60, max_size=2 ** 24, open_timeout=15)

    async def keep(self, names: list[str]) -> None:
        backoff = 1.0
        while True:
            try:
                await self.run_one(names)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - network drop: reconnect, then backfill the gap
                log.warning("stream connection dropped (%s: %s); reconnecting in %.0fs", type(e).__name__, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def run(self) -> None:
        await asyncio.gather(*(self.keep(g) for g in groups(self.symbols, self.tfs)))


def _ssl() -> ssl.SSLContext:
    import truststore   # the OS trust store, like market/liquidations.py
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
