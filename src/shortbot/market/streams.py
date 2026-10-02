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

Updates are buffered in memory and written once per second in a single transaction, so ~1400 kline msg/s cost
one commit. Store: data/streams.db, its own WAL file, with one writer (the collector) and readers that tolerate a
missing file. Rows are keyed by the Binance id (BTCUSDT); readers also accept ccxt symbols (BTC/USDT:USDT).
After each (re)connect, a (symbol, tf) whose last closed bar is older than the previous bar is backfilled once over
REST. That is bounded: the last BACKFILL_LIMIT bars, at weight 1 per call.

    reader: candles(symbol, tf, n) -> [[ts, o, h, l, c, v], ...] or None when not fresh or gapped
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

from .. import clock as market_clock   # market time (a replay pins it)

log = logging.getLogger("shortbot.streams")
BASE = "wss://fstream.binance.com/market/stream?streams="
DB_PATH = "data/streams.db"
TF_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
TFS = ("15m", "1h")
ALL_MARKET = ("!ticker@arr", "!markPrice@arr@1s")
MAX_STREAMS_PER_CONN = 200   # Binance allows 1024; two sockets halve what one drop loses
RECONNECT_S = 23 * 3600      # before Binance's 24 h cut, at a time of our choosing
RECV_TIMEOUT_S = 60          # markPrice@arr@1s ticks every second: a minute of silence is a dead socket
BACKFILL_LIMIT = 99          # klines weight is 1 below 100 bars
KEEP_BARS = 500
SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (symbol TEXT, tf TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,
    q REAL, tbq REAL, closed INTEGER, PRIMARY KEY (symbol, tf, ts)) WITHOUT ROWID;
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
        self.bars_buf: dict[tuple[str, str, int], tuple] = {}
        self.latest_buf: dict[tuple[str, str], tuple[int, str]] = {}
        self.fresh_buf: dict[tuple[str, str], int] = {}

    def kline(self, k: dict[str, Any], now_ms: int) -> None:
        s = k["s"]
        self.bars_buf[(s, k["i"], int(k["t"]))] = (float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]),
                                                   float(k["v"]), float(k["q"]), float(k.get("Q") or 0),
                                                   int(bool(k["x"])))
        self.fresh_buf[(s, f"kline_{k['i']}")] = now_ms

    def latest(self, kind: str, symbol: str, data: dict[str, Any], now_ms: int) -> None:
        self.latest_buf[(kind, symbol)] = (now_ms, json.dumps(data))
        self.fresh_buf[(symbol, kind)] = now_ms

    def bars(self, symbol: str, tf: str, rows: list[list[float]], now_ms: int) -> int:
        """REST rows [ts, o, h, l, c, v] (ccxt shape), written at once: all but a still-forming bar are closed. A bar
        the stream already holds keeps its taker volumes."""
        step, sym = TF_MS[tf], raw_id(symbol)
        for r in rows:
            self.db.execute("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,NULL,NULL,?) ON CONFLICT DO UPDATE SET "
                            "o=excluded.o, h=excluded.h, l=excluded.l, c=excluded.c, v=excluded.v, "
                            "closed=excluded.closed", (sym, tf, int(r[0]), *map(float, r[1:6]),
                                                       int(r[0] + step <= now_ms)))
        self.db.commit()
        return len(rows)

    def flush(self) -> int:
        n = len(self.bars_buf) + len(self.latest_buf)
        if not n:
            return 0
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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

    def prune(self, now_ms: int) -> None:
        """Keep the last KEEP_BARS bars per timeframe (by time)."""
        with self.db:
            for tf, step in TF_MS.items():
                self.db.execute("DELETE FROM candles WHERE tf=? AND ts < ?", (tf, now_ms - KEEP_BARS * step))


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
    capped to the last BACKFILL_LIMIT (an older hole stays outside every window candles() serves)."""
    out = []
    for s in symbols:
        for tf in tfs:
            step = TF_MS[tf]
            current = now_ms // step * step
            last = store.last_closed(s, tf)
            if last is None or last < current - step:
                floor = current - (BACKFILL_LIMIT - 1) * step
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
        self.backfill_calls = 0

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
            self.backfill_calls += 1
            try:
                n += self.store.bars(s, tf, await self.backfill(s, tf, since, BACKFILL_LIMIT),
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


# ------------------------------------------------------------------ readers (any process; missing file -> None)
def _ro(path: str | Path) -> sqlite3.Connection | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None


def candles(symbol: str, tf: str, n: int, path: str | Path = DB_PATH,
            now_ms: int | None = None) -> list[list[float]] | None:
    """The last n bars [ts, o, h, l, c, v], closed plus the forming one (like REST), when fresh: the last closed bar
    is at most 1 bar old and the closed bars have no gap. Otherwise None, and the caller falls back to REST."""
    db = _ro(path)
    if db is None or tf not in TF_MS or n < 1:
        return None
    try:
        rows = db.execute("SELECT ts, o, h, l, c, v, closed FROM candles WHERE symbol=? AND tf=? ORDER BY ts DESC "
                          "LIMIT ?", (raw_id(symbol), tf, n + 2)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    step = TF_MS[tf]
    now = now_ms if now_ms is not None else market_clock.now_ms()
    closed = [r for r in rows if r[6]]
    if not closed or closed[0][0] < now // step * step - 2 * step:   # last closed bar more than 1 bar old
        return None
    forming = [r for r in rows if not r[6] and r[0] > closed[0][0]]
    if len(forming) > 1 or (forming and forming[0][0] != closed[0][0] + step):
        return None                                                  # a missed close: that bar is incomplete
    window = list(reversed(forming + closed))[-n:]
    if len(window) < n or any(b[0] - a[0] != step for a, b in zip(window, window[1:])):
        return None                                                  # too short, or a gap inside the window
    return [list(r[:6]) for r in window]


def freshness(path: str | Path = DB_PATH, now_ms: int | None = None) -> dict[str, Any] | None:
    """Per stream kind: {"symbols": n, "oldest_s": age, "newest_s": age} of the last updates (None: no file)."""
    db = _ro(path)
    if db is None:
        return None
    now = now_ms if now_ms is not None else market_clock.now_ms()
    try:
        return {stream: {"symbols": n, "oldest_s": round((now - lo) / 1000), "newest_s": round((now - hi) / 1000)}
                for stream, n, lo, hi in db.execute("SELECT stream, COUNT(*), MIN(ts), MAX(ts) FROM fresh "
                                                    "GROUP BY stream")}
    except sqlite3.Error:
        return None
    finally:
        db.close()


def stale(stream: str, max_age_s: float, path: str | Path = DB_PATH, now_ms: int | None = None) -> list[str]:
    """Symbols whose last `stream` update is older than max_age_s."""
    db = _ro(path)
    if db is None:
        return []
    now = now_ms if now_ms is not None else market_clock.now_ms()
    try:
        return [s for (s,) in db.execute("SELECT symbol FROM fresh WHERE stream=? AND ts < ? ORDER BY symbol",
                                         (stream, now - max_age_s * 1000))]
    finally:
        db.close()


def latest(kind: str, path: str | Path = DB_PATH, max_age_s: float = 120,
           now_ms: int | None = None) -> dict[str, dict[str, Any]]:
    """{symbol: data} of the ticker / mark rows younger than max_age_s ({} when the file is missing)."""
    db = _ro(path)
    if db is None:
        return {}
    now = now_ms if now_ms is not None else market_clock.now_ms()
    try:
        return {s: json.loads(d) for s, d in db.execute("SELECT symbol, data FROM latest WHERE kind=? AND ts >= ?",
                                                        (kind, now - max_age_s * 1000))}
    finally:
        db.close()
