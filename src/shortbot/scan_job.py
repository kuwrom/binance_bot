"""Continuous scan for the guardian's research lane (task #31). The guardian never opens trades: this logs every
scanned coin as a research candidate (labelled 72h later), exactly like the desk's scan_shorts tool, and returns
alerts for candidates worth a trading session's look.

Cadence (measured 2026-10-02): one scan is ~325 requests and ~800 of Binance's 2,400 weight per minute per IP
(exchangeInfo rateLimits).
- Every 5 minutes that is ~7% of the IP budget, and it still catches every 15m close (the triggers' bars) within
  5 minutes.
- Every minute would use ~1/3 of the budget continuously, alongside the guardian and the desk. Overruns earn 418
  bans, so EVERY_S = 300.

Alert rule:
- tradable (no vetoes), score >= ALERT_MIN_SCORE, a detected setup, a tape stance that allows an entry;
- a regime that is not hostile;
- one alert per symbol per RENOTIFY_S, unless its score rose by SCORE_BUMP.

The guardian runs the tiered scan (scan_tiered.py, #72), whose tiers 2-3 are `score_and_alert`; `run` is the REST
"both" scan, kept for a one-off pass.
"""

from __future__ import annotations

import time
from typing import Any

EVERY_S = 300
MIN_CHANGE, TOP_N, DEPTH = 10.0, 12, 25
ALERT_MIN_SCORE, RENOTIFY_S, SCORE_BUMP = 30, 7200, 10
ENTRY_STANCES = ("fade", "short_bounces", "wait_for_trigger")
KV_SEEN = "scan_alert_seen:"   # + symbol -> "<time> <score>": dedup that survives a guardian restart (time first,
                               # so journal.prune_kv's timestamp cast reads it; housekeeping prunes after RENOTIFY_S)
TIER2_WAIT_S = 60.0            # a tier-2 call over its cap yields this long, then budget_wait: the coin is requeued


def pick_alerts(candidates: list[dict[str, Any]], regime: str, seen: dict[str, dict[str, float]],
                now_s: float) -> list[dict[str, Any]]:
    """Candidates worth an analyst's look; `seen` ({symbol: {"t", "score"}}) is updated in place (dedup)."""
    if regime == "hostile":
        return []
    out = []
    for c in candidates:
        stance = (c.get("tape") or {}).get("stance")
        if c.get("vetoes") or (c.get("score") or 0) < ALERT_MIN_SCORE or not c.get("setups") \
                or stance not in ENTRY_STANCES:
            continue
        prev = seen.get(c["symbol"])
        if prev and now_s - prev["t"] < RENOTIFY_S and c["score"] < prev["score"] + SCORE_BUMP:
            continue
        seen[c["symbol"]] = {"t": now_s, "score": c["score"]}
        out.append({"symbol": c["symbol"], "score": c["score"], "setups": c.get("setups"), "stance": stance,
                    "change_24h": c.get("change_24h"), "regime": regime,
                    "text": f"{c['symbol']} score {c['score']} {'/'.join(c.get('setups') or [])} ({stance}, "
                            f"{c.get('change_24h')}% 24h, regime {regime}): run a trading session to review"})
    return out


async def run(ex, journal, seen: dict[str, dict[str, float]], now_s: float | None = None) -> dict[str, Any]:
    """One pass: regime, a logged 'both'-direction scan, alerts. Raises on exchange errors (the lane records it)."""
    from .scanner import market_regime, scan_shorts
    reg = await market_regime(ex)
    verdict = reg["verdict_for_shorts"]
    res = await scan_shorts(ex, min_change=MIN_CHANGE, top_n=TOP_N, depth=DEPTH, journal=journal, regime=verdict,
                            direction="both")
    alerts = pick_alerts(res.get("candidates") or [], verdict, seen, now_s if now_s is not None else time.time())
    return {"regime": verdict, "logged": res.get("logged_for_research", 0),
            "candidates": len(res.get("candidates") or []), "alerts": alerts}


def load_seen(journal, symbols: list[str], now_s: float) -> dict[str, dict[str, float]]:
    """The persisted alert dedup of these symbols still inside RENOTIFY_S."""
    out = {}
    for sym in symbols:
        try:
            t, score = (float(x) for x in (journal.get(KV_SEEN + sym) or "").split())
        except ValueError:   # never alerted (or an unreadable marker): no dedup
            continue
        if now_s - t < RENOTIFY_S:
            out[sym] = {"t": t, "score": score}
    return out


async def score_and_alert(ex, journal, tickers: list[dict[str, Any]], regime: str, scan: dict[str, Any],
                          by_symbol: dict[str, dict[str, Any]] | None = None,
                          now_s: float | None = None) -> dict[str, Any]:
    """Tiers 2-3 (docs/SCANNING.md, #72): deep-score these coins as TIER2 (its own weight cap), log each complete row
    as a research candidate (features.scan = the window's wake plus the coin's triggers) and pick alerts like `run`.
    A coin whose inputs the weight guard held back (data_deferred) isn't logged: it comes back in `deferred` for the
    caller to requeue. Alert dedup lives in kv (KV_SEEN), so a guardian restart doesn't re-alert within RENOTIFY_S."""
    from . import scanner, weight
    with weight.tagged(weight.TIER2, wait_s=TIER2_WAIT_S):
        evals, rows = await scanner.evaluate_tickers(ex, tickers)
    done = [(ev, row) for ev, row in zip(evals, rows) if not ev.features.get("data_deferred")]
    scanner.log_scan(journal, [ev for ev, _ in done], [row for _, row in done], regime, scan, by_symbol)
    now = now_s if now_s is not None else time.time()
    seen = load_seen(journal, [row["symbol"] for _, row in done], now)
    alerts = pick_alerts([row for _, row in done], regime, seen, now)
    for a in alerts:
        journal.set(KV_SEEN + a["symbol"], f"{now} {a['score']}")
    return {"scored": [ev.symbol for ev, _ in done], "alerts": alerts,
            "deferred": [ev.symbol for ev in evals if ev.features.get("data_deferred")]}
