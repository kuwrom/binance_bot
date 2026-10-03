"""Sector taxonomy for the market map (docs/MARKET_MAP.md): Binance product tags (undocumented bapi endpoint,
refreshed weekly, cached in the journal kv), one PRIMARY sector per coin by a fixed priority. Generic tags
(Seed, pos, Launchpool, zones...) carry no narrative and are ignored; coins with none fall into 'other'."""

from __future__ import annotations

import json
import ssl
import time
from typing import Any

import httpx
import truststore

PRODUCTS_URL = "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products?includeEtf=true"
# Narrative first (a meme or AI coin trades with its narrative before its chain)
PRIORITY = ("Meme", "AI", "RWA", "Gaming", "NFT", "defi", "Payments", "storage-zone", "fan_token", "Solana", "BSC",
            "Layer1_Layer2", "Infrastructure")
LABEL = {"Layer1_Layer2": "L1/L2", "defi": "DeFi", "storage-zone": "Storage", "fan_token": "Fan", "Infrastructure": "Infra"}
KV_KEY, TTL_S = "sector_tags", 7 * 86_400


def primary_sector(tags: list[str]) -> str:
    for t in PRIORITY:
        if t in tags:
            return LABEL.get(t, t)
    return "other"


def base_of(symbol: str) -> str:
    base = symbol.split("/")[0]
    for p in ("1000000", "1000", "1M"):
        if base.startswith(p) and len(base) > len(p) + 1:
            return base[len(p):]
    return base


async def fetch_tags() -> dict[str, list[str]]:
    async with httpx.AsyncClient(timeout=20, verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)) as c:
        r = await c.get(PRODUCTS_URL)
        r.raise_for_status()
    out: dict[str, list[str]] = {}
    for row in r.json().get("data") or []:
        if row.get("tags"):
            out.setdefault(row["b"], [])
            out[row["b"]] = sorted(set(out[row["b"]]) | {t for t in row["tags"] if t})
    return out


async def sector_map(journal) -> dict[str, str]:
    """base -> primary sector, from the weekly cache (refetched when stale; stale cache beats nothing)."""
    raw = journal.get(KV_KEY)
    cached: dict[str, Any] = json.loads(raw) if raw else {}
    if not cached or time.time() - cached.get("ts", 0) > TTL_S:  # wall-clock: cache TTL
        try:
            cached = {"ts": time.time(), "tags": await fetch_tags()}  # wall-clock: cache TTL
            v = json.dumps(cached)
            journal.mark(KV_KEY, v)   # a cache: read back at once, or each build refetches it during a hold (#82)
        except Exception:  # noqa: BLE001 - taxonomy is context; keep the old one
            pass
    return {b: primary_sector(t) for b, t in (cached.get("tags") or {}).items()}
