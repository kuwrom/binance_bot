"""Token supply facts for the controller profile (docs/COIN_PROFILE.md): circulating / total / max supply,
market cap and FDV from CoinGecko's free /coins/markets (top 2,000 by market cap, 8 calls, refreshed weekly;
"Data provided by CoinGecko", https://www.coingecko.com/en/api). Symbol collisions resolve to the largest market
cap. Also a tiny read-only KV reader so signals can use guardian/analyst caches from the shared DB."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import ssl
import time
from typing import Any

import httpx
import truststore

MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
KV_KEY, TTL_S, PAGES = "coin_supply", 7 * 86_400, 8


class KVReader:
    def __init__(self, db_path):
        self.db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=15)

    def get(self, key: str) -> Any:
        try:
            row = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        except sqlite3.Error:
            return None
        return json.loads(row[0]) if row else None


async def fetch_supply(pages: int = PAGES) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=30, verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)) as c:
        for page in range(1, pages + 1):
            r = await c.get(MARKETS_URL, params={"vs_currency": "usd", "order": "market_cap_desc",
                                                 "per_page": 250, "page": page})
            if r.status_code == 429:
                await asyncio.sleep(60)
                r = await c.get(MARKETS_URL, params={"vs_currency": "usd", "order": "market_cap_desc",
                                                     "per_page": 250, "page": page})
            r.raise_for_status()
            for x in r.json():
                sym = (x.get("symbol") or "").upper()
                if sym and sym not in out:          # sorted by market cap: the first hit is the big one
                    out[sym] = {k: x.get(k) for k in ("id", "market_cap", "fully_diluted_valuation",
                                                      "circulating_supply", "total_supply", "max_supply")}
            await asyncio.sleep(2.5)                # keyless tier: stay far under ~10-30 calls/min
    return out


async def refresh(journal, force: bool = False) -> dict[str, Any]:
    cur = journal.get(KV_KEY)
    cached = json.loads(cur) if cur else {}
    if not force and cached and time.time() - cached.get("ts", 0) < TTL_S:
        return {"cached": True, "coins": len(cached.get("coins", {}))}
    coins = await fetch_supply()
    journal.set(KV_KEY, json.dumps({"ts": time.time(), "coins": coins}))
    return {"cached": False, "coins": len(coins)}


def supply_facts(s: dict[str, Any] | None) -> dict[str, float | None]:
    """float % (circulating / max-or-total) and FDV / market cap."""
    if not s:
        return {"float_pct": None, "fdv_mc": None, "mcap_usd": None}
    denom = s.get("max_supply") or s.get("total_supply")
    circ, mc, fdv = s.get("circulating_supply"), s.get("market_cap"), s.get("fully_diluted_valuation")
    return {"float_pct": round(circ / denom * 100, 1) if circ and denom else None,
            "fdv_mc": round(fdv / mc, 2) if fdv and mc else None, "mcap_usd": mc}
