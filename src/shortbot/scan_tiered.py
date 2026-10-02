"""Tiers 1-3 of the scanner in the guardian's scan lane (docs/SCANNING.md, #72).

Each tick (guardian.SCAN):
- At a new 15m close (CLOSE_GRACE_MS after it, so the closing bar is in the store), tier 1 evaluates every rule
  (scan_triggers.evaluate) on the liquid universe the collector is streaming.
- Between closes, only the fast events run (scan_triggers.fast_events: liquidation bursts, premium spikes, the BTC/ETH
  impulse). Each (coin, rule) fires once per window. An impulse re-checks every rule on every coin, and the impulse
  itself is a wake-up, not a coin to score.
- select() merges per coin, ranks, caps at 20 (TIER2_CAP) and skips coins rescored with no new rule within RESCORE_S.
  A close also carries the last queue, at half strength.
- Tier 2 deep-scores as many selected coins as fit weight.tier2_room() at the measured weight per coin (an EWMA from
  the prior EST_COIN_WEIGHT). The rest queue for the next close, and so does a coin the weight guard starved
  (data_deferred).
- Tier 3: scan_job.score_and_alert logs the research candidates and picks alerts.
- Every close, and every fast window that fired, writes a scan_windows row to the journal DB: counts per rule,
  selected, scored, queued, skipped and the tier-2 weight spent. The triggers are evaluated from these rows.

Tier-1 data comes from the stream store and the recorded liquidations, read in a worker thread: no exchange calls.
When the streams are stale (collector down), tier 1 pauses and one alert goes out; there is no REST fallback scan.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import clock, scan_job, weight
from . import scan_triggers as TR
from .exchange import NO_DELIVERY, to_symbol
from .guardian_loop import send_safe
from .market import streams
from .market.liquidations import raw_to_ccxt, recent_many

log = logging.getLogger("shortbot.scan")
M15, H1 = streams.TF_MS["15m"], streams.TF_MS["1h"]
BARS15, BARS15_MIN = 120, 96   # the rates were measured on 96 returns + 29 prior hours; 96 fits a cold start's 99
FRESH_S = 90                   # a coin whose 15m kline stream is quieter than this is out of the universe
CLOSE_GRACE_MS = 5_000
FAST_BARS = ("BTCUSDT", "ETHUSDT")
EST_COIN_WEIGHT = 29           # ccxt cost model 2026-10-02 (docs/SCANNING.md); then measured per window
WARM_WEIGHT = 2.0              # the funding tables, once per batch
EWMA = 0.3
QUEUE_MAX = 3 * TR.TIER2_CAP
REGIME_TTL_S, INTERVALS_TTL_S, UNLOCK_DAYS = 300, 3600, 30
STALE_ALERT_S, STALE_RENOTIFY_S = 600, 6 * 3600
SCHEMA = ("CREATE TABLE IF NOT EXISTS scan_windows (ts_ms INTEGER NOT NULL, kind TEXT NOT NULL, coins INTEGER, "
          "counts TEXT, selected TEXT, scored TEXT, queued TEXT, skipped TEXT, tier2_weight REAL)")


def snapshot(streams_db: Path | str, journal_db: Path | str | None, now_ms: int,
             bars_for: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Tier-1 inputs as of now_ms (blocking reads: run it in a thread). The universe is the coins whose 15m kline
    stream is live; bars_for limits the bar reads (the fast path needs BTC and ETH only)."""
    universe = streams.fresh_symbols("kline_15m", FRESH_S, streams_db, now_ms)
    live = set(universe)
    bars15 = {}
    for s in (universe if bars_for is None else [s for s in bars_for if s in live]):
        b = (streams.candles(s, "15m", BARS15, streams_db, now_ms, FRESH_S)
             or streams.candles(s, "15m", BARS15_MIN, streams_db, now_ms, FRESH_S))
        if b:
            bars15[s] = b

    def pick(kind: str) -> dict[str, Any]:
        return {s: v for s, v in streams.latest(kind, streams_db, FRESH_S, now_ms).items() if s in live}
    liqs = recent_many(journal_db, [raw_to_ccxt(s) for s in universe], now_ms - H1, now_ms) if journal_db else {}
    return {"universe": universe, "bars15": bars15, "ticker": pick("ticker"), "mark": pick("mark"), "liqs": liqs}


class TieredScan:
    def __init__(self, ex, journal, notifier, streams_db: Path | None, journal_db: Path | str | None):
        self.ex, self.j, self.n = ex, journal, notifier
        self.streams_db, self.journal_db = streams_db, journal_db
        self.now_ms: Callable[[], int] = clock.now_ms
        self.window: int | None = None                  # the last 15m window evaluated in full
        self.fired: set[tuple[str, str]] = set()        # (coin, rule) already fired in that window
        self.queued: list[dict[str, Any]] = []
        self.scored: dict[str, tuple[int, set[str]]] = {}   # coin -> (when, rules) of its last deep score
        self.est = float(EST_COIN_WEIGHT)
        self.stale_since: int | None = None
        self._regime_at: tuple[float, str | None] = (-math.inf, None)
        self._intervals_at: tuple[float, dict[str, int]] = (-math.inf, {})
        self._events: Any = None   # EventStore on the journal DB, opened at the first close

    async def step(self) -> dict[str, Any] | None:
        if self.streams_db is None:
            return None
        now = self.now_ms()
        w = now // M15
        close = self.window is None or (w > self.window and now - w * M15 >= CLOSE_GRACE_MS)
        snap = await asyncio.to_thread(snapshot, self.streams_db, self.journal_db, now, None if close else FAST_BARS)
        if not snap["universe"]:
            await self._stale(now)
            return None
        self.stale_since = None
        if close:
            self.window, kind = w, "close"
            trig = await self._evaluate(snap, now)
            self.fired = {(t.symbol, t.rule) for t in trig}
        else:
            trig = [t for t in TR.fast_events(snap["bars15"], snap["mark"], snap["liqs"], now)
                    if (t.symbol, t.rule) not in self.fired]
            if not trig:
                return None
            kind = "fast"
            if any(t.rule == "btc_impulse" for t in trig):   # the market moved: every rule on every coin
                kind = "impulse"
                snap = await asyncio.to_thread(snapshot, self.streams_db, self.journal_db, now, None)
                trig = _dedupe(trig + await self._evaluate(snap, now))
            self.fired |= {(t.symbol, t.rule) for t in trig}
        return await self._deep(snap, trig, kind, now, close)

    async def _evaluate(self, snap: dict[str, Any], now: int) -> list[TR.Trigger]:
        ctx = await self._context(snap["universe"])
        return await asyncio.to_thread(functools.partial(TR.evaluate, snap["bars15"], snap["ticker"], snap["mark"],
                                                         snap["liqs"], now, **ctx))

    async def _deep(self, snap: dict[str, Any], trig: list[TR.Trigger], kind: str, now: int,
                    close: bool) -> dict[str, Any]:
        """Tiers 2-3 for one window: select, fit the tier-2 room, score, requeue, record."""
        counts = dict(Counter(t.rule for t in trig))
        sel = TR.select([t for t in trig if t.rule != "btc_impulse"], self.queued if close else None,
                        self._recent(now))
        fit = max(0, int((weight.tier2_room() - WARM_WEIGHT) // self.est))
        go, wait = sel["selected"][:fit], sel["selected"][fit:]
        res: dict[str, Any] = {"scored": [], "deferred": [], "alerts": []}
        spent = 0.0
        if go:
            before = weight.tier2_usage()
            tickers = await self._tickers([to_symbol(c["symbol"]) for c in go], snap["ticker"])
            res = await scan_job.score_and_alert(
                self.ex, self.j, tickers, await self._regime(), {"tier": 2, "wake": kind},
                {to_symbol(c["symbol"]): {"rules": c["rules"], "strength": c["strength"],
                                          "carried": bool(c.get("carried"))} for c in go}, now_s=now / 1000)
            after = weight.tier2_usage()
            if after[0] == before[0]:   # same 15m window: the batch's own spend
                spent = after[1] - before[1]
                if spent > WARM_WEIGHT:
                    self.est = round((1 - EWMA) * self.est + EWMA * (spent - WARM_WEIGHT) / len(go), 2)
        done_ids = {streams.raw_id(s) for s in res["scored"]}
        done = [c for c in go if c["symbol"] in done_ids]
        later = sel["queued"] + wait + [c for c in go if c["symbol"] not in done_ids]   # starved or unpriced
        self.queued = _queue(later if close else self.queued + later)
        for c in done:
            prev = self.scored.get(c["symbol"])
            kept = prev[1] if prev and now - prev[0] < TR.RESCORE_S * 1000 else set()
            self.scored[c["symbol"]] = (now, kept | set(c["rules"]))
        for a in res["alerts"]:
            await send_safe(self.n, "scan_alert", a["text"], a["symbol"], cooldown_s=0)
        row = {"ts_ms": now, "kind": kind, "coins": len(snap["universe"]), "counts": counts,
               "selected": [{k: c[k] for k in ("symbol", "strength", "rules")} for c in sel["selected"]],
               "scored": [c["symbol"] for c in done], "queued": [c["symbol"] for c in later],
               "skipped": sel["skipped"], "tier2_weight": spent}
        self._record(row)
        log.info("scan %s: %d coins, triggers %s; selected %d, scored %d, queued %d, skipped %d; tier 2 spent %.0f "
                 "weight (~%.1f/coin)", kind, row["coins"], counts or "none", len(sel["selected"]), len(done),
                 len(later), len(sel["skipped"]), spent, self.est)
        return {**row, "alerts": len(res["alerts"])}

    def _record(self, row: dict[str, Any]) -> None:
        self.j.db.execute(SCHEMA)
        self.j.db.execute("INSERT INTO scan_windows VALUES (?,?,?,?,?,?,?,?,?)",
                          (row["ts_ms"], row["kind"], row["coins"], *(json.dumps(row[k]) for k in
                           ("counts", "selected", "scored", "queued", "skipped")), row["tier2_weight"]))
        self.j.db.commit()

    def _recent(self, now: int) -> dict[str, set[str]]:
        self.scored = {s: v for s, v in self.scored.items() if now - v[0] < TR.RESCORE_S * 1000}
        return {s: rules for s, (_, rules) in self.scored.items()}

    async def _stale(self, now: int) -> None:
        self.stale_since = now if self.stale_since is None else self.stale_since
        if now - self.stale_since >= STALE_ALERT_S * 1000:
            await send_safe(self.n, "streams_stale", f"No live market streams for {(now - self.stale_since) / 60000:.0f}"
                            " min: tier-1 triggers and tier-2 deep scores are paused (desk scans read REST). Check the "
                            "guardian's stream task (logger shortbot.streams).", cooldown_s=STALE_RENOTIFY_S)

    async def _tickers(self, symbols: list[str], stream_ticker: dict[str, dict]) -> list[dict[str, Any]]:
        """ccxt-shaped tickers from the stream (zero weight); the rest from one REST call (research)."""
        out, missing = [], []
        for s in symbols:
            t = stream_ticker.get(streams.raw_id(s))
            if t:
                out.append({"symbol": s, "percentage": t["pct"], "last": t["last"], "quoteVolume": t["qv"],
                            "high": t["h"], "low": t["l"]})
            else:
                missing.append(s)
        if missing:
            try:
                with weight.tagged(weight.RESEARCH):
                    rest = await self.ex.public.fetch_tickers()
                out += [rest[s] for s in missing if s in rest]
            except Exception as e:  # noqa: BLE001 - those coins wait for the next window
                log.warning("tickers for %s: %s: %s", missing, type(e).__name__, e)
        return out

    async def _regime(self) -> str:
        at, verdict = self._regime_at
        if verdict is None or time.monotonic() - at >= REGIME_TTL_S:   # wall-clock: cache TTL
            from . import scanner
            with weight.tagged(weight.RESEARCH):
                verdict = (await scanner.market_regime(self.ex))["verdict_for_shorts"]
            self._regime_at = (time.monotonic(), verdict)   # wall-clock: cache TTL
        return verdict

    async def _context(self, universe: list[str]) -> dict[str, Any]:
        """Listing ages, events (a scheduled delisting, an unlock within UNLOCK_DAYS), funding intervals."""
        perps, now = await self.ex.coin_perps(), self.now_ms()
        ages, events = {}, {}
        for raw in universe:
            info = (perps.get(to_symbol(raw)) or {}).get("info") or {}
            if info.get("onboardDate"):
                ages[raw] = (now - int(info["onboardDate"])) / 86_400_000
            if int(info.get("deliveryDate") or NO_DELIVERY) != NO_DELIVERY:
                events[raw] = "delisting scheduled"
        by_base = {raw[:-4].removeprefix("1000"): raw for raw in universe}
        for u in self._unlocks():
            raw = by_base.get(u["base"])
            if raw:
                events.setdefault(raw, f"unlock in {u['days_to_unlock']:.1f}d ({u.get('pct_supply')}% of supply)")
        return {"listing_age_d": ages, "events": events, "interval_h": await self._intervals()}

    def _unlocks(self) -> list[dict[str, Any]]:
        if not self.journal_db or str(self.journal_db) == ":memory:":
            return []
        try:
            if self._events is None:
                from .market.events import EventStore
                self._events = EventStore(self.journal_db)
            return self._events.upcoming(days=UNLOCK_DAYS, include_past_days=0)
        except Exception:  # noqa: BLE001 - no unlock triggers this window
            log.exception("unlocks unreadable")
            return []

    async def _intervals(self) -> dict[str, int]:
        at, cached = self._intervals_at
        if time.monotonic() - at >= INTERVALS_TTL_S:   # wall-clock: cache TTL
            try:
                with weight.tagged(weight.RESEARCH):
                    rows = await self.ex.funding_intervals()
                self._intervals_at = (time.monotonic(), {s: r["interval_h"] for s, r in rows.items()})  # wall-clock
            except Exception as e:  # noqa: BLE001 - 8h for all until the next try
                log.warning("funding intervals: %s: %s", type(e).__name__, e)
        return self._intervals_at[1]


def _dedupe(trig: list[TR.Trigger]) -> list[TR.Trigger]:
    best: dict[tuple[str, str], TR.Trigger] = {}
    for t in trig:
        if (t.symbol, t.rule) not in best or t.strength > best[(t.symbol, t.rule)].strength:
            best[(t.symbol, t.rule)] = t
    return list(best.values())


def _queue(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per coin (the strongest), strongest first, bounded."""
    best: dict[str, dict[str, Any]] = {}
    for c in items:
        if c["symbol"] not in best or c["strength"] > best[c["symbol"]]["strength"]:
            best[c["symbol"]] = c
    return sorted(best.values(), key=lambda c: -c["strength"])[:QUEUE_MAX]
