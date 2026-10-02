"""Liquidation feed. Binance publishes forced orders ONLY on the websocket (no REST, no history for USDⓈ-M),
at most 1 order per symbol per second (undercounts bursts). The guardian runs `LiquidationRecorder` so the
store accumulates our own history; signals read it through MarketContext.liquidations().

Side: S=SELL means a LONG was liquidated (forced sell); S=BUY means a SHORT was liquidated.
TLS: uses the OS trust store (truststore), which works behind TLS-intercepting proxies/antivirus on macOS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import ssl
import time
from pathlib import Path
from typing import Any

from .. import clock

log = logging.getLogger("shortbot.liquidations")
STREAM_URL = "wss://fstream.binance.com/market/ws/!forceOrder@arr"  # /market path since 2026-04-23
SCHEMA = """
CREATE TABLE IF NOT EXISTS liquidations (
    ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL, price REAL, notional REAL
);
CREATE INDEX IF NOT EXISTS idx_liq_sym_ts ON liquidations (symbol, ts_ms);
"""
KEEP_DAYS = 30


def raw_to_ccxt(raw: str) -> str:
    return f"{raw[:-4]}/USDT:USDT" if raw.endswith("USDT") else raw


class LiquidationStore:
    def __init__(self, db_path: Path | str):
        self.db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def record(self, rows: list[tuple]) -> None:
        self.db.executemany("INSERT INTO liquidations VALUES (?,?,?,?,?,?)", rows)
        self.db.commit()

    async def recent(self, symbol: str, since_ms: int) -> list[dict[str, Any]]:
        cur = self.db.execute("SELECT * FROM liquidations WHERE symbol=? AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms",
                              (symbol, since_ms, clock.now_ms()))   # never after now (a replay pins the clock)
        return [dict(r) for r in cur.fetchall()]

    def prune(self) -> int:
        n = self.db.execute("DELETE FROM liquidations WHERE ts_ms < ?",
                            (int((time.time() - KEEP_DAYS * 86400) * 1000),)).rowcount  # wall-clock: retention
        self.db.commit()
        return n


def recent_many(db_path: Path | str, symbols: list[str], since_ms: int, now_ms: int) -> dict[str, list[dict]]:
    """{symbol: [{ts_ms, side, notional}, ...]} in [since, now] for these ccxt symbols, read-only from any thread
    (scan tier 1). Symbols without rows are left out; a missing file is {}."""
    p = Path(db_path)
    if not p.exists():
        return {}
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
    try:
        out: dict[str, list[dict]] = {}
        for s in symbols:   # one indexed (symbol, ts_ms) seek each
            rows = db.execute("SELECT ts_ms, side, notional FROM liquidations WHERE symbol=? AND ts_ms BETWEEN ? "
                              "AND ? ORDER BY ts_ms", (s, since_ms, now_ms)).fetchall()
            if rows:
                out[s] = [{"ts_ms": t, "side": side, "notional": n} for t, side, n in rows]
        return out
    except sqlite3.Error:   # no liquidations table yet: no bursts
        return {}
    finally:
        db.close()


def parse(msg: dict[str, Any]) -> tuple | None:
    o = msg.get("o") or {}
    if msg.get("e") != "forceOrder" or not o:
        return None
    qty, px = float(o.get("z") or o.get("q") or 0), float(o.get("ap") or o.get("p") or 0)
    side = "long_liquidated" if o.get("S") == "SELL" else "short_liquidated"
    return (int(o.get("T") or msg.get("E") or 0), raw_to_ccxt(o.get("s", "")), side, qty, px, qty * px)


class LiquidationRecorder:
    """Long-running task: stream -> batch insert every ~2 s. Reconnects with backoff forever."""

    def __init__(self, store: LiquidationStore):
        self.store = store
        self.buffer: list[tuple] = []
        self.received = 0

    async def run(self) -> None:
        import truststore
        import websockets
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        backoff = 1
        while True:
            try:
                async with websockets.connect(STREAM_URL, ssl=ctx, ping_interval=60, max_size=2**20) as ws:
                    log.info("liquidation stream connected")
                    backoff = 1
                    last_flush = time.time()  # wall-clock: recorder flush
                    async for raw in ws:
                        row = parse(json.loads(raw))
                        if row:
                            self.buffer.append(row)
                            self.received += 1
                        if time.time() - last_flush > 2 and self.buffer:  # wall-clock: recorder flush
                            self.store.record(self.buffer)
                            self.buffer, last_flush = [], time.time()  # wall-clock: recorder flush
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - network hiccups: reconnect
                log.warning("liquidation stream error %s: reconnecting in %ss", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
