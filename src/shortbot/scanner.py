"""Scanner: rank futures movers, evaluate each with the signal plug-ins (via the DataHub), log research
candidates, and build per-coin snapshots. The scanner orchestrates only; signals own the logic."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from . import indicators as ind
from .exchange import Exchanges, to_symbol
from .market import DataHub
from .market.regime import market_regime  # noqa: F401  (re-exported: callers import it from here)
from .setups import detect_all
from .tape import classify
from .signals import Evaluation, evaluate


def _row(ev: Evaluation) -> dict[str, Any]:
    f = ev.features
    r = lambda x, n=2: round(x, n) if isinstance(x, (int, float)) and not isinstance(x, bool) else x  # noqa: E731
    return {
        "symbol": ev.symbol, "score": ev.score, "change_24h": r(f.get("change_24h"), 1),
        "vol_24h_musd": r((f.get("quote_volume") or 0) / 1e6, 1),
        "funding_8h_pct": r(f["funding_8h"] * 100, 4) if f.get("funding_8h") is not None else None,
        "oi_musd": r(f["oi_value"] / 1e6, 1) if f.get("oi_value") else None, "oi_chg_24h": r(f.get("oi_change_24h"), 1),
        "rsi_1h": r(f.get("rsi_1h"), 0), "vwap_z": r(f.get("vwap_z"), 1), "off_high_pct": r(f.get("off_high_pct"), 1),
        "listing_age_d": r(f.get("listing_age_days"), 0), "setups": detect_all(f), "tape": classify(f),
        "signals": ev.signals, "vetoes": ev.vetoes,
    }


async def scan_shorts(ex: Exchanges, min_change: float = 10.0, top_n: int = 10, depth: int = 25,
                      journal=None, regime: str | None = None, direction: str = "gainers",
                      groups: tuple[str, ...] | None = None) -> dict:
    """Rank Binance futures by 24h change (gainers / losers / both), evaluate the top `depth` with the signal
    plug-ins, return the best `top_n` tradable + vetoed. With a journal, every evaluated coin becomes a research
    candidate (outcome labelled 72h later)."""
    if direction not in ("gainers", "losers", "both", "now"):
        raise ValueError("direction must be gainers, losers, both or now")
    perps = await ex.coin_perps()
    tickers = await ex.public.fetch_tickers()
    listed = [t for s, t in tickers.items() if s in perps and t.get("percentage") is not None]
    gainers = sorted((t for t in listed if t["percentage"] >= min_change), key=lambda t: -t["percentage"])
    losers = sorted((t for t in listed if t["percentage"] <= -min_change), key=lambda t: t["percentage"])
    if direction == "now":   # what is moving NOW: biggest |4h| moves in the liquid map universe (guardian cache)
        from .market.market_map import cached
        mcoins = (cached(journal, 3600) if journal is not None else None) or {}
        ranked = sorted(((s, c["r4h"]) for s, c in (mcoins.get("coins") or {}).items() if c.get("r4h") is not None),
                        key=lambda x: -abs(x[1]))
        movers = [tickers[s] for s, r in ranked if s in tickers and abs(r) >= min_change / 2][:depth]
    else:
        movers = {"gainers": gainers[:depth], "losers": losers[:depth],
                  "both": gainers[:depth // 2 + 1] + losers[:depth // 2 + 1]}[direction]
    hub = DataHub(ex)
    await hub.funding_tables()  # warm the shared cache once before fanning out
    evals = await asyncio.gather(*(evaluate(hub.context(t["symbol"], t), groups) for t in movers))
    rows = [_row(ev) for ev in evals]
    if journal is not None and evals:
        now = datetime.now(timezone.utc)
        journal.log_candidates(now.isoformat(timespec="seconds"), int(now.timestamp() * 1000), [
            {**row, "price": ev.features.get("last"), "features": {**ev.features, "tape_state": row["tape"]["state"]}}
            for ev, row in zip(evals, rows)], regime)   # tape state as classified NOW (the registry needs scan-time)
        _log_research_brackets(journal, evals, rows, regime)
    tradable = sorted((r for r in rows if not r["vetoes"]), key=lambda r: r["score"], reverse=True)
    vetoed = [{"symbol": r["symbol"], "change_24h": r["change_24h"], "vetoes": r["vetoes"]} for r in rows if r["vetoes"]]
    now_ms = time.time() * 1000
    new_listings = sorted(
        ({"symbol": s, "age_d": round((now_ms - int(m["info"]["onboardDate"])) / 864e5, 1),
          "change_24h": round(tickers.get(s, {}).get("percentage") or 0, 1)}
         for s, m in perps.items() if m["info"].get("onboardDate") and now_ms - int(m["info"]["onboardDate"]) < 30 * 864e5),
        key=lambda x: x["age_d"])
    errors = {ev.symbol: ev.errors for ev in evals if ev.errors}
    return {"universe": len(perps), "direction": direction, "movers_above_threshold": len(movers),
            "min_change": min_change, "candidates": tradable[:top_n], "vetoed": vetoed[:10],
            "new_listings_30d": new_listings[:10], "logged_for_research": len(rows) if journal is not None else 0,
            **({"signal_errors": errors} if errors else {})}


async def entry_checks(ex: Exchanges, symbol: str, ticker: dict) -> dict[str, Any]:
    """Server-side re-check before any short: signal vetoes + market regime + features stored on the trade."""
    ev, reg = await asyncio.gather(evaluate(DataHub(ex).context(symbol, ticker)), market_regime(ex))
    return {"vetoes": ev.vetoes, "regime": reg["verdict_for_shorts"], "setups_detected": detect_all(ev.features),
            "features": {**ev.features, "regime_warnings": reg["warnings"]}}


async def coin_snapshot(ex: Exchanges, symbol: str) -> dict:
    """Deep look at one coin: every signal plus multi-timeframe structure, positioning, news, stop options."""
    symbol = to_symbol(symbol)
    ctx = DataHub(ex).context(symbol)
    ev = await evaluate(ctx)
    c1h, c15, c4h, ls, fhist, nws, oi, tk, fund = await asyncio.gather(
        ctx.candles("1h", 60), ctx.candles("15m", 96), ctx.candles("4h", 60), ctx.long_short(),
        ctx.funding_history(10), ctx.news(), ctx.oi_history("1h", 25), ctx.taker_ratio("1h", 6), ctx.funding())
    last = await ctx.last()
    atr1h = ind.atr(c1h) or 0
    r = lambda v, n=4: round(v, n) if isinstance(v, (int, float)) and not isinstance(v, bool) else v  # noqa: E731
    lim = ex.limits

    def stop_option(price: float, method: str) -> dict:
        dist = (price - last) / last * 100 if last else None
        ok = dist is not None and lim.min_stop_distance_pct <= dist <= lim.max_stop_distance_pct
        return {"price": r(price), "distance_pct": r(dist, 2), "method": method, "allowed": ok,
                "note": None if ok else f"outside {lim.min_stop_distance_pct}-{lim.max_stop_distance_pct}% limit"}

    return {
        **_row(ev), "last": last, "mark": fund.get("mark"), "next_funding": fund.get("next"),
        "funding_history_pct": [r(f["fundingRate"] * 100, 4) for f in fhist],
        "structure": {
            "rsi_15m": r(ind.rsi(ind.closes(c15)), 0), "rsi_4h": r(ind.rsi(ind.closes(c4h)), 0),
            "lower_high_15m": ind.lower_high(c15, 4), "lower_high_1h": ev.features.get("lower_high"),
            "ema20_1h": r(ind.ema(ind.closes(c1h), 20)[-1]) if c1h else None,
            "high_24h": r(max(c[2] for c in c1h[-24:])) if c1h else None,
            "high_7d": r(max(c[2] for c in c4h[-42:])) if c4h else None,
            "low_7d": r(min(c[3] for c in c4h[-42:])) if c4h else None,
            "atr_1h": r(atr1h), "atr_1h_pct": r(atr1h / last * 100, 2) if last else None,
            "last_6h_1h_candles_ohlc": [[r(v) for v in c[1:5]] for c in c1h[-6:]],
        },
        "positioning": {**ls, "taker_buy_sell_1h": [r(t, 3) for t in tk],
                        "oi_last_6h_musd": [r(o["oi_value"] / 1e6, 1) for o in oi[-6:]]},
        "listing": await ctx.market_info(),
        "features": {k: r(v) for k, v in ev.features.items()},
        "suggested_stops": {
            "tight": stop_option(max(ind.swing_high(c1h, 4) if c1h else last, last) + 1.0 * atr1h,
                                 "last 4h swing high + 1x ATR(1h)"),
            "wide": stop_option(max(ind.swing_high(c1h, 12) if c1h else last, last) + 1.5 * atr1h,
                                "12h swing high + 1.5x ATR(1h)"),
            **_sweep_proof(ex, last, max(c[2] for c in c1h[-24:]) if c1h else None, atr1h, stop_option),
            "hint": f"Wider stop => smaller size (auto-sized to the risk budget). Max leverage for a stop is "
                    f"~100/(stop% + {lim.liq_buffer_pct + 1})."},
        "news": nws,
        **({"signal_errors": ev.errors} if ev.errors else {}),
    }


def _sweep_proof(ex, last, high_24h, atr, stop_option) -> dict[str, Any]:
    """Stop measured from our own level-sweep study (sweeps.py), when it has enough events."""
    from datetime import datetime, timezone

    from .sessions import session_bucket
    from .sweeps import guide, sweep_proof_stop
    try:
        g = guide(ex.settings.db_path, session_bucket(datetime.now(timezone.utc)))
        sp = sweep_proof_stop(last, high_24h, atr, g) if last else None
    except Exception:  # noqa: BLE001 - research aid only
        sp = None
    return {"sweep_proof": {**stop_option(sp["price"], sp["basis"]), "level": sp["level"]}} if sp else {}


def _log_research_brackets(journal, evals, rows, regime) -> None:
    """Every evaluated coin gets a standard vol-scaled 24h short bracket (stop +1σ√T, target −2σ√T) with its
    no-edge baseline p. Labelled 24h later, these measure where real drift exists (by setup, tape state, regime)."""
    from .probability import vol_scaled_bracket
    from .probability.baseline import short_geometry
    for ev, row in zip(evals, rows):
        price, s = ev.features.get("last"), ev.features.get("sigma_h")
        if not price or not s:
            continue
        try:
            stop, target = vol_scaled_bracket(price, s)
            g = short_geometry(price, stop, target, s, 24.0)
        except (ValueError, ZeroDivisionError, OverflowError):
            continue  # research logging must never break a scan
        journal.log_prediction("scan", ev.symbol, price, stop, target, 24.0, "bracket", g["p_target"], sigma_h=s,
                               context={"setup": (row["setups"] or ["none"])[0], "setups": row["setups"],
                                        "tape_state": row["tape"]["state"], "regime": regime, "score": ev.score,
                                        "tradable": not ev.vetoes})
