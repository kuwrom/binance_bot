"""Data layer for the pattern / manipulation event study (patterns.py): hourly perp klines with trade counts and
taker flow, open interest, funding, the premium index and spot klines, for every tradable perp over the last
~10 days. Cached per coin in the journal DB (table study_coins) so re-running the analysis is free. Read-only."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from .exchange import to_raw

SCHEMA = """CREATE TABLE IF NOT EXISTS study_coins (
    symbol TEXT PRIMARY KEY, fetched_ms INTEGER, data TEXT)"""

HOUR = 3_600_000


def _kl(rows: list) -> list[dict[str, float]]:
    """Binance kline rows -> dicts (qv = quote volume, n = trade count, tbq = taker-buy quote volume)."""
    return [{"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
             "qv": float(r[7]), "n": int(r[8]), "tbq": float(r[10])} for r in rows]


async def fetch_coin(ex, sym: str, hours: int, info: dict[str, Any]) -> dict[str, Any]:
    raw = to_raw(sym)
    lim = min(hours, 1500)
    perp, oi, prem, fund = await asyncio.gather(
        ex.public.fapiPublicGetKlines({"symbol": raw, "interval": "1h", "limit": lim}),
        ex.public.fapiDataGetOpenInterestHist({"symbol": raw, "period": "1h", "limit": min(hours, 500)}),
        ex.public.fapiPublicGetPremiumIndexKlines({"symbol": raw, "interval": "1h", "limit": lim}),
        ex.public.fapiPublicGetFundingRate({"symbol": raw, "limit": 100}),
        return_exceptions=True)
    if isinstance(perp, BaseException):
        raise perp
    spot_raw, mult = await ex.spot_symbol(sym)
    spot = []
    if spot_raw:
        try:
            spot = _kl(await ex.spot.publicGetKlines({"symbol": spot_raw, "interval": "1h", "limit": lim}))
        except Exception:  # noqa: BLE001 - spot is optional context
            spot = []
    ok = lambda x: not isinstance(x, BaseException)  # noqa: E731
    return {
        "symbol": sym, "onboard_ms": int(info.get("onboardDate") or 0), "tags": info.get("underlyingSubType") or [],
        "perp": _kl(perp),
        "oi": {int(r["timestamp"]): float(r["sumOpenInterestValue"]) for r in oi} if ok(oi) else {},
        "premium": {int(r[0]): float(r[4]) for r in prem} if ok(prem) else {},
        "funding": [[int(r["fundingTime"]), float(r["fundingRate"])] for r in fund] if ok(fund) else [],
        "spot": [{**b, "c": b["c"] * mult} for b in spot], "spot_mult": mult,
    }


async def fetch_all(ex, journal, days: float = 10, max_age_h: float = 6, top: int | None = None) -> dict[str, Any]:
    """Fetch every tradable perp (refreshing cache rows older than `max_age_h`). Commits per coin so the guardian
    never waits on our write lock."""
    journal.db.execute(SCHEMA)
    hours = int(days * 24) + 1
    perps = await ex.coin_perps()
    syms = sorted(perps)
    if top:
        tickers = await ex.public.fetch_tickers()
        syms = sorted(syms, key=lambda s: (tickers.get(s) or {}).get("quoteVolume") or 0, reverse=True)[:top]
    now = int(time.time() * 1000)
    fresh = {s for s, t in journal.db.execute("SELECT symbol, fetched_ms FROM study_coins")
             if now - t < max_age_h * HOUR}
    todo = [s for s in syms if s not in fresh]
    sem, errors = asyncio.Semaphore(6), {}

    async def one(sym: str) -> None:
        async with sem:
            try:
                d = await fetch_coin(ex, sym, hours, perps[sym]["info"])
            except Exception as e:  # noqa: BLE001 - one coin failing must not stop the study
                errors[sym] = f"{type(e).__name__}: {e}"[:120]
                return
        journal.db.execute("INSERT OR REPLACE INTO study_coins VALUES (?,?,?)", (sym, now, json.dumps(d)))
        journal.db.commit()

    await asyncio.gather(*(one(s) for s in todo))
    return {"universe": len(syms), "fetched": len(todo) - len(errors), "cached": len(fresh & set(syms)),
            "errors": len(errors), "error_sample": dict(list(errors.items())[:3])}


def load(journal, symbols: list[str] | None = None) -> list[dict[str, Any]]:
    journal.db.execute(SCHEMA)
    rows = journal.db.execute("SELECT symbol, data FROM study_coins").fetchall()
    out = [json.loads(d) for s, d in rows if symbols is None or s in symbols]
    for d in out:   # JSON turns int keys into strings
        d["oi"] = {int(k): v for k, v in d["oi"].items()}
        d["premium"] = {int(k): v for k, v in d["premium"].items()}
    return out
