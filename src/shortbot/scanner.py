"""Scanner: rank futures movers, evaluate each with the signal plug-ins (via the DataHub), log research
candidates, and build per-coin snapshots. The scanner orchestrates only; signals own the logic."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import clock, news
from . import indicators as ind
from .exchange import Exchanges, to_symbol
from .market import DataHub
from .market.regime import market_regime
from .market.source import MarketSource
from .risk import FALLBACK_MAINT_MARGIN_PCT, max_leverage_for_stop
from .setups import detect_all
from .signals import Evaluation, evaluate
from .tape import classify

log = logging.getLogger("shortbot.scanner")


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


async def evaluate_tickers(ex: MarketSource, tickers: list[dict], groups: tuple[str, ...] | None = None
                           ) -> tuple[list[Evaluation], list[dict[str, Any]]]:
    """Evaluate these coins (ccxt-shaped tickers) with the signal plug-ins: (evaluations, rows). Shared by the desk's
    scan and the guardian's tier-2 deep score (scan_job.score_and_alert)."""
    hub = DataHub(ex)
    await hub.funding_tables()  # warm the shared cache once before fanning out
    evals = list(await asyncio.gather(*(evaluate(hub.context(t["symbol"], t), groups) for t in tickers)))
    await add_har(hub, evals)
    return evals, [_row(ev) for ev in evals]


async def add_har(hub: DataHub, evals: list[Evaluation], hours: float = 24.0) -> int:
    """The horizon-matched σ (logged beside the used one, so QLIKE can compare them) for the coins that reach the
    probability stage: tradable (no veto) and complete. Not for every scanned coin: its 1h history costs a fetch per
    coin per hour (vol_har.closed_history caches it per bar; the stream store first). Returns coins done."""
    from .probability.vol_har import forecast
    todo = [ev for ev in evals if ev.tradable and not ev.features.get("data_deferred")
            and ev.features.get("sigma_h_har") is None]

    async def one(ev: Evaluation) -> bool:
        try:
            hz, _ = await forecast(hub.context(ev.symbol), hours, clock.now_ms())
        except Exception as e:  # noqa: BLE001 - research logging never breaks a scan
            log.warning("HAR σ for %s failed (%s: %s)", ev.symbol, type(e).__name__, e)
            return False
        ev.features["sigma_h_har"] = (hz or {}).get("sigma_h")
        return ev.features["sigma_h_har"] is not None
    return sum(await asyncio.gather(*(one(ev) for ev in todo)))


def log_scan(journal, evals: list[Evaluation], rows: list[dict[str, Any]], regime: str | None, scan: dict[str, Any],
             by_symbol: dict[str, dict[str, Any]] | None = None) -> int:
    """Every evaluated coin becomes a research candidate (labelled 72h later) with its scan's arguments in
    features.scan (a tier-2 row adds its triggers via by_symbol), plus a research bracket. Returns rows logged."""
    if not evals:
        return 0
    now = clock.now_dt()
    journal.log_candidates(now.isoformat(timespec="seconds"), int(now.timestamp() * 1000), [
        {**row, "price": ev.features.get("last"),
         "features": {**ev.features, "tape_state": row["tape"]["state"],
                      "scan": {**scan, **(by_symbol or {}).get(ev.symbol, {})}}}
        for ev, row in zip(evals, rows, strict=True)],
        regime)   # tape state as classified NOW (the registry needs scan-time)
    _log_research_brackets(journal, evals, rows, regime)
    return len(evals)


async def scan_shorts(ex: MarketSource, min_change: float = 10.0, top_n: int = 10, depth: int = 25,
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
    evals, rows = await evaluate_tickers(ex, movers, groups)
    if journal is not None:
        log_scan(journal, evals, rows, regime, {"direction": direction, "min_change": min_change, "depth": depth,
                                                "top_n": top_n, "groups": list(groups) if groups else None})
    tradable = sorted((r for r in rows if not r["vetoes"]), key=lambda r: r["score"], reverse=True)
    vetoed = [{"symbol": r["symbol"], "change_24h": r["change_24h"], "vetoes": r["vetoes"]} for r in rows if r["vetoes"]]
    now_ms = clock.now_ms()
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
    # all nine at once, as two gathers of typed results (typeshed types a gather of at most six awaitables)
    (c1h, c15, c4h, ls, fhist), (nws, oi, tk, fund) = await asyncio.gather(
        asyncio.gather(ctx.candles("1h", 60), ctx.candles("15m", 96), ctx.candles("4h", 60), ctx.long_short(),
                       ctx.funding_history(10)),
        asyncio.gather(ctx.news(), ctx.oi_history("1h", 25), ctx.taker_ratio("1h", 6), ctx.funding()))
    last = await ctx.last()
    atr1h = ind.atr(c1h) or 0
    r = lambda v, n=4: round(v, n) if isinstance(v, (int, float)) and not isinstance(v, bool) else v  # noqa: E731
    lim = ex.limits

    def stop_option(price: float, method: str) -> dict:
        dist = (price - last) / last * 100 if last else None
        ok = dist is not None and lim.min_stop_distance_pct <= dist <= lim.max_stop_distance_pct
        return {"price": r(price), "distance_pct": r(dist, 2), "method": method, "allowed": ok,
                "max_leverage": (max_leverage_for_stop(dist, lim.liq_buffer_pct, lim.max_leverage)
                                 if ok and dist is not None else None),   # (ok: a distance within the limits)
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
            "hint": f"Wider stop => smaller size (auto-sized to the risk budget). max_leverage: the highest leverage "
                    f"whose estimated liquidation (at the conservative {FALLBACK_MAINT_MARGIN_PCT:g}% maintenance "
                    f"rate) stays {lim.liq_buffer_pct:g}% beyond that stop; open_short re-checks at the symbol's own "
                    "leverage bracket."},
        "news": news.for_agent(nws),
        **({"signal_errors": ev.errors} if ev.errors else {}),
    }


def _sweep_proof(ex, last, high_24h, atr, stop_option) -> dict[str, Any]:
    """Stop measured from our own level-sweep study (sweeps.py), when it has enough events."""
    from .sessions import session_bucket
    from .sweeps import guide, sweep_proof_stop
    try:
        g = guide(ex.settings.db_path, session_bucket(clock.now_dt()))
        sp = sweep_proof_stop(last, high_24h, atr, g) if last else None
    except Exception:  # noqa: BLE001 - research aid only
        sp = None
    return {"sweep_proof": {**stop_option(sp["price"], sp["basis"]), "level": sp["level"]}} if sp else {}


K_STOPS, REWARDS = (0.5, 1.0, 1.5), (0.5, 1.0, 2.0)   # research bracket shapes, drawn per coin and scan


def _log_research_brackets(journal, evals, rows, regime) -> None:
    """Every evaluated coin gets a vol-scaled 24h short research bracket with its no-edge baseline odds. The geometry
    is drawn per coin and scan (stop k in K_STOPS x σ√T, target REWARDS x the stop distance), so calibration covers
    more than one bracket shape (research note 2026-10-09: one shape left a single populated reliability bucket);
    P(stop) and P(time) are logged too. Labelled 24h later, these measure where real drift exists (by setup, tape
    state, regime) and score the σ forecasts (QLIKE)."""
    import random

    from . import clock
    from .probability import vol_scaled_bracket
    from .probability.baseline import short_geometry
    from .probability.recal import policy
    from .probability.vol import clamp_sigma
    (scale, model), minute = policy(journal), clock.now_ms() // 60_000
    for ev, row in zip(evals, rows, strict=True):
        f = ev.features
        raw, used = ((f.get("sigma_h_har"), "har") if model == "har" and f.get("sigma_h_har") else
                     (f.get("sigma_h_ewma"), "ewma") if f.get("sigma_h_ewma") else (f.get("sigma_h"), "parkinson"))
        price, s = f.get("last"), clamp_sigma((raw or 0) * scale)
        if not price or not s:
            continue
        rng = random.Random(f"{ev.symbol}:{minute}")   # reproducible draw
        k, reward = rng.choice(K_STOPS), rng.choice(REWARDS)
        try:
            stop, target = vol_scaled_bracket(price, s, 24.0, k, reward)
            g = short_geometry(price, stop, target, s, 24.0)
        except (ValueError, ZeroDivisionError, OverflowError):
            continue  # research logging must never break a scan
        journal.log_prediction("scan", ev.symbol, price, stop, target, 24.0, "bracket", g["p_target"], sigma_h=s,
                               context={"setup": (row["setups"] or ["none"])[0], "setups": row["setups"],
                                        "tape_state": row["tape"]["state"], "regime": regime, "score": ev.score,
                                        "tradable": not ev.vetoes, "k_stop": k, "reward": reward,
                                        "p_stop": round(g["p_stop"], 4), "p_time": round(g["p_time"], 4),
                                        "sigma_ewma": f.get("sigma_h_ewma"), "sigma_har": f.get("sigma_h_har"),
                                        "sigma_model": used, "sigma_scale": scale})
