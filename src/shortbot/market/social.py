"""Social / attention feeds at $0 (docs/SOCIAL_AND_ROBUST_PRICES.md §3). Public data only; read-only; polite.

- CoinGecko /search/trending: top ~15 coins by search (free, 10 min cache upstream)
- Binance web3 social-hype leaderboard (official skills-hub endpoint): hype score, sentiment, AI summary
  for on-chain tokens (BSC / Solana / Base)
- Binance Square hashtag stats (UNDOCUMENTED endpoint, may break; throttled ≥ 3 s/request, guardian-only)
The guardian snapshots these hourly into `social_snapshots`; signals read history from the store so scans never
wait on per-symbol calls. Red lines: no posting, no private/paid 'insider' groups, no scraping behind logins.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx

from .. import clock
from ..untrusted import token, wrap

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) shortbot-research"}
CG_TRENDING = "https://api.coingecko.com/api/v3/search/trending"
HYPE_URL = ("https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/social/hype/"
            "rank/leaderboard/ai")
HYPE_CHAINS = ("56", "CT_501", "8453")
SQUARE_URL = "https://www.binance.com/bapi/composite/v4/friendly/pgc/content/queryByHashtag"
SQUARE_MIN_INTERVAL_S = 3.0
SCHEMA = """
CREATE TABLE IF NOT EXISTS social_snapshots (
    ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL, source TEXT NOT NULL, metric TEXT NOT NULL, value REAL
);
CREATE INDEX IF NOT EXISTS idx_social ON social_snapshots (symbol, metric, ts_ms);
"""
_cache: dict[str, tuple[float, Any]] = {}
_square_lock = asyncio.Lock()
_square_last = 0.0


async def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:  # wall-clock: cache TTL
        return hit[1]
    val = await fn()
    _cache[key] = (time.time(), val)  # wall-clock: cache TTL
    return val


async def coingecko_trending() -> dict[str, int]:
    """{SYMBOL: rank (1 = most searched)}."""
    async def fetch():
        async with httpx.AsyncClient(headers=UA, timeout=15) as c:
            coins = (await c.get(CG_TRENDING)).json().get("coins", [])
        return {x["item"]["symbol"].upper(): i + 1 for i, x in enumerate(coins)}
    return await _cached("cg", 600, fetch)


async def binance_hype() -> dict[str, dict[str, Any]]:
    """{SYMBOL: {hype, sentiment, summary}} across chains (max hype if a symbol appears twice). The AI summary is
    third-party text (wrapped as untrusted data); sentiment is kept only as a short label."""
    async def fetch():
        out: dict[str, dict[str, Any]] = {}
        async with httpx.AsyncClient(headers=UA, timeout=15) as c:
            for chain in HYPE_CHAINS:
                r = await c.get(HYPE_URL, params={"chainId": chain, "targetLanguage": "en", "timeRange": 1})
                for it in ((r.json().get("data") or {}).get("leaderBoardList") or []):
                    sym = (it.get("metaInfo") or {}).get("symbol", "").upper()
                    info = it.get("socialHypeInfo") or {}
                    hype = float(info.get("socialHype") or 0)
                    if sym and hype >= out.get(sym, {}).get("hype", -1):
                        out[sym] = {"hype": hype, "sentiment": token(info.get("sentiment")),
                                    "summary": wrap(info.get("socialSummaryBrief") or "", "binance_hype", 200),
                                    "wash_trading_tag": "Wash Trading Behavior" in (it.get("tagInfoList") or {})}
        return out
    return await _cached("hype", 1800, fetch)


async def square_hashtag(base: str) -> dict[str, float] | None:
    """Binance Square #BASE stats. Throttled globally; call from the guardian, not from scans."""
    global _square_last
    async with _square_lock:
        wait = SQUARE_MIN_INTERVAL_S - (time.time() - _square_last)  # wall-clock: rate limiter
        if wait > 0:
            await asyncio.sleep(wait)
        _square_last = time.time()  # wall-clock: rate limiter
        async with httpx.AsyncClient(headers=UA, timeout=15) as c:
            r = await c.get(SQUARE_URL, params={"hashtag": f"#{base}", "pageIndex": 1, "pageSize": 1,
                                                "orderBy": "LATEST"})
    h = ((r.json().get("data") or {}).get("hashtag")) or {}
    if not h:
        return None
    return {"posts_total": float(h.get("contentCount") or 0), "views_total": float(h.get("viewCount") or 0),
            "posts_7d": float(h.get("contentCount7days") or 0), "posts_30d": float(h.get("contentCount30days") or 0)}


class SocialStore:
    def __init__(self, db_path: Path | str):
        self.db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=15)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def record(self, rows: list[tuple]) -> None:
        """rows: (ts_ms, symbol, source, metric, value)"""
        self.db.executemany("INSERT INTO social_snapshots VALUES (?,?,?,?,?)", rows)
        self.db.commit()

    def series(self, symbol: str, metric: str, since_ms: int) -> list[tuple[int, float]]:
        """Snapshots from since_ms up to now, never after it (a replay pins the clock)."""
        cur = self.db.execute("SELECT ts_ms, value FROM social_snapshots WHERE symbol=? AND metric=? AND ts_ms>=?"
                              " AND ts_ms<=? ORDER BY ts_ms", (symbol, metric, since_ms, clock.now_ms()))
        return [(int(t), float(v)) for t, v in cur.fetchall()]


async def snapshot(store: SocialStore, bases: list[str]) -> int:
    """Guardian job: record one snapshot for each base asset. Returns rows written."""
    now = int(time.time() * 1000)  # wall-clock: recorder timestamp
    rows: list[tuple] = []
    try:
        cg = await coingecko_trending()
    except Exception:  # noqa: BLE001
        cg = {}
    try:
        hype = await binance_hype()
    except Exception:  # noqa: BLE001
        hype = {}
    for base in bases:
        if base in cg:
            rows.append((now, base, "coingecko", "trending_rank", cg[base]))
        if base in hype:
            rows.append((now, base, "binance_hype", "hype", hype[base]["hype"]))
        try:
            sq = await square_hashtag(base)
        except Exception:  # noqa: BLE001 - undocumented endpoint: tolerate failure
            sq = None
        for k, v in (sq or {}).items():
            rows.append((now, base, "square", k, v))
    if rows:
        store.record(rows)
    return len(rows)
