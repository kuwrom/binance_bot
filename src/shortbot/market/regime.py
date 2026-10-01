"""Market regime for alt shorts: BTC trend, alt breadth, Fear & Greed. Fails closed ('unknown')."""

from __future__ import annotations

import asyncio

from .. import indicators as ind
from .. import news
from ..exchange import Exchanges


async def market_regime(ex: Exchanges) -> dict:
    btc = "BTC/USDT:USDT"
    perps = await ex.coin_perps()
    (tickers, c4h, fng) = await asyncio.gather(
        ex.public.fetch_tickers(), ex.public.fetch_ohlcv(btc, "4h", limit=60),
        news.fear_greed(), return_exceptions=True)
    errors = [n for n, v in (("tickers", tickers), ("btc_4h", c4h), ("fear_greed", fng)) if isinstance(v, BaseException)]
    fng = fng if isinstance(fng, dict) else {}
    closes = ind.closes(c4h) if isinstance(c4h, list) else []
    t = tickers.get(btc, {}) if isinstance(tickers, dict) else {}
    changes = [x.get("percentage") or 0 for s, x in (tickers.items() if isinstance(tickers, dict) else [])
               if s in perps]
    breadth = sum(1 for c in changes if c > 0) / len(changes) * 100 if changes else None
    ema20, ema50 = (ind.ema(closes, 20)[-1], ind.ema(closes, 50)[-1]) if closes else (None, None)
    btc_chg = t.get("percentage") or 0
    warnings = []
    if btc_chg > 3:
        warnings.append(f"BTC +{btc_chg:.1f}% 24h: alts pump with BTC, shorts get squeezed")
    if closes and closes[-1] > ema20 > ema50:
        warnings.append("BTC 4h uptrend (price > EMA20 > EMA50)")
    if breadth and breadth > 70:
        warnings.append(f"{breadth:.0f}% of alts green: broad risk-on")
    if fng.get("value", 0) >= 75:
        warnings.append(f"Extreme greed ({fng['value']}): tops form here, but trends persist")
    verdict = "hostile" if len(warnings) >= 2 else "caution" if warnings else "favorable"
    if "tickers" in errors or "btc_4h" in errors:
        verdict = "unknown"  # fail closed: never call it favorable without BTC/breadth data
    return {"verdict_for_shorts": verdict, "warnings": warnings, "errors": errors,
            "btc": {"last": t.get("last"), "change_24h": round(btc_chg, 2),
                    "ema20_4h": round(ema20, 1) if ema20 else None, "ema50_4h": round(ema50, 1) if ema50 else None},
            "breadth_pct_green": round(breadth, 1) if breadth else None, "fear_greed": fng}
