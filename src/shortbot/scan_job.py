"""Tiers 2-3 of the guardian's research lane (scan_tiered.py, #72). The guardian never opens trades: this logs every
deep-scored coin as a research candidate (labelled 72h later), exactly like the desk's scan_shorts tool, and returns
alerts for candidates worth a trading session's look.

Alert rule:
- tradable (no vetoes), score >= ALERT_MIN_SCORE, a detected setup, a tape stance that allows an entry;
- a regime that is not hostile;
- one alert per symbol per RENOTIFY_S, unless its score rose by SCORE_BUMP.
"""

from __future__ import annotations

import time
from typing import Any

MIN_CHANGE = 10.0   # the pump threshold (scan_triggers)
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
    """Tiers 2-3 (docs/SCANNING.md, #72): deep-score these coins as TIER2 (its own weight cap) and pick alerts; the
    batch's rows are written by `journal_batch`, in one unit with its scan window (scan_tiered.py, #79).
    A coin whose inputs the weight guard held back (data_deferred) isn't logged: it comes back in `deferred` for the
    caller to requeue. Alert dedup lives in kv (KV_SEEN), so a guardian restart doesn't re-alert within RENOTIFY_S."""
    from . import scanner, weight
    with weight.tagged(weight.TIER2, wait_s=TIER2_WAIT_S):
        evals, rows = await scanner.evaluate_tickers(ex, tickers)
    done = [(ev, row) for ev, row in zip(evals, rows, strict=True) if not ev.features.get("data_deferred")]
    now = now_s if now_s is not None else time.time()
    seen = load_seen(journal, [row["symbol"] for _, row in done], now)
    alerts = pick_alerts([row for _, row in done], regime, seen, now)
    return {"scored": [ev.symbol for ev, _ in done], "alerts": alerts,
            "deferred": [ev.symbol for ev in evals if ev.features.get("data_deferred")],
            "log": {"evals": [ev for ev, _ in done], "rows": [row for _, row in done], "regime": regime, "scan": scan,
                    "by_symbol": by_symbol, "now": now}}


def journal_batch(journal, res: dict[str, Any]) -> None:
    """A scored batch's rows, inside the caller's unit: each complete row a research candidate (features.scan = the
    window's wake plus the coin's triggers) with its research bracket, and each alert's dedup marker."""
    log = res.get("log")
    if not log:
        return
    from . import scanner
    scanner.log_scan(journal, log["evals"], log["rows"], log["regime"], log["scan"], log["by_symbol"])
    for a in res["alerts"]:
        journal.set(KV_SEEN + a["symbol"], f"{log['now']} {a['score']}")
