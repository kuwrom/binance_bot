"""Research engine: label logged scan candidates with what a short would actually have done, and report
which scores / setups / vetoes / features predicted drops. Works on every scan, traded or not, so the
desk learns even on days it doesn't trade. New signal features are reported automatically.

Short returns are positive when price FELL (good for a short): short_ret = (p0 - p_t) / p0 * 100.
max_adverse_24h = worst rise against the short within 24h (squeeze risk); max_favorable_24h = best drop.

Since 2026-10-01 (#27) the report counts one observation per symbol-episode (rescans of a coin within the 24h
outcome horizon are the same bet), compares groups against the median of their own scan, subtracts costs and gives
95% intervals clustered by day (research_stats). Labels start at the scan time, not the scan's floored hour.
"""

from __future__ import annotations

import re
import statistics
from typing import Any

from . import research_stats as rs
from .exchange import Exchanges
from .journal_research import LABEL_MAX_TRIES, LABEL_VERSION

HOUR_MS = 3_600_000
RETRY_H = 2             # a failed label fetch is retried after 2h, 4h, 8h, 16h (~30h in all), then given up
NUMERIC_SKIP = {"funding_interval_h"}
HORIZON_H = 24          # outcome horizon of short_ret_24h / max_adverse_24h: the episode window
BAR_MS = 300_000        # label bars: 5m, the first one opening at/after the scan (weight 5 per 864-bar request)
BARS_PER_H = 12
COST_PCT = 0.1          # round-trip fees + slippage, % (as patterns.COST_PCT)
SQUEEZE_PCT = 15
BASIS = "episodes (since 2026-10-01)"
ALL_ROWS = 10_000_000   # report on every labelled row: a capped sample would silently drop the oldest days


def outcome_from_candles(p0: float, candles: list[list[float]], bars_per_hour: int = 1) -> dict[str, float | None]:
    """candles: OHLCV bars opening at (or just after) the scan time, `bars_per_hour` per hour, so no price action
    from before the scan leaks in. The close of bar h * bars_per_hour - 1 is the price h hours later."""
    def ret_at(h: int) -> float | None:
        i = h * bars_per_hour - 1
        return (p0 - candles[i][4]) / p0 * 100 if len(candles) > i else None
    first24 = candles[:24 * bars_per_hour]
    return {
        "short_ret_4h": ret_at(4), "short_ret_24h": ret_at(24), "short_ret_72h": ret_at(72),
        "max_adverse_24h": (max(c[2] for c in first24) - p0) / p0 * 100 if first24 else None,
        "max_favorable_24h": (p0 - min(c[3] for c in first24)) / p0 * 100 if first24 else None,
    }


async def _outcome(ex: Exchanges, r: dict[str, Any], held: bool) -> dict[str, float | None]:
    """The row's outcome by the current method. Raises when there is nothing to write: no bars, no scan price, or
    (for a row holding an older label) no 24h close, so a label is never traded for NULLs."""
    start = -(-r["ts_ms"] // BAR_MS) * BAR_MS                  # first 5m bar opening at/after the scan
    candles = await ex.public.fetch_ohlcv(r["symbol"], "5m", since=start, limit=72 * BARS_PER_H)
    if not candles or not r["price"]:
        raise LookupError("no bars" if not candles else "no scan price")
    outcome = outcome_from_candles(r["price"], candles, BARS_PER_H)
    if held and outcome["short_ret_24h"] is None:
        raise LookupError(f"no 24h close ({len(candles)} bars)")
    return outcome


async def label_outcomes(ex: Exchanges, journal, now_ms: int, limit: int = 120) -> int:
    """Label candidates at least 72h old (all horizons at once), and relabel ones labelled by an older method.
    A failed fetch (delisted symbol, 429, timeout, no bars) writes nothing: retried after RETRY_H, doubling. After
    LABEL_MAX_TRIES failures an older label is kept as it is (label_v unchanged) and a never-labelled row is labelled
    empty; either way it leaves the queue. 120 per hourly run keeps ahead of ~50 logged rows an hour.
    Returns how many labels were written."""
    rows = journal.unlabelled_candidates(older_than_ms=now_ms - 73 * HOUR_MS, limit=limit, version=LABEL_VERSION,
                                         now_ms=now_ms)
    n = 0
    for r in rows:
        held = r["short_ret_24h"] is not None                   # an older method's label, in the reports today
        try:
            outcome, error = await _outcome(ex, r, held), None
        except Exception as e:  # noqa: BLE001 - recorded on the row, retried
            outcome, error = {}, f"{type(e).__name__}: {e}"
        tries = (r["label_tries"] or 0) + 1
        if error is None or (tries >= LABEL_MAX_TRIES and not held):
            journal.label_candidate(r["id"], outcome, LABEL_VERSION)
            n += 1
        if error is not None:
            retry = None if tries >= LABEL_MAX_TRIES else now_ms + RETRY_H * HOUR_MS * 2 ** (tries - 1)
            journal.label_failed(r["id"], error, tries, retry)
    return n


async def label_post_exit(ex: Exchanges, journal, now_ms: int, limit: int = 10) -> int:
    """For closed trades >= 24h old: how far did price keep going our way AFTER we exited? Measures whether our
    exits systematically leave the move on the table (hold runners longer) or ONDO-style runs were luck."""
    from datetime import datetime
    n = 0
    for t in journal.trades(200):
        if t["status"] != "closed" or t.get("post_exit") or not t.get("closed_at") or not t.get("exit_price"):
            continue
        closed_ms = int(datetime.fromisoformat(t["closed_at"]).timestamp() * 1000)
        if now_ms - closed_ms < 25 * HOUR_MS:
            continue
        try:
            c = await ex.public.fetch_ohlcv(t["symbol"], "1h", since=closed_ms - closed_ms % HOUR_MS, limit=26)
        except Exception:  # noqa: BLE001
            c = []
        px = t["exit_price"]
        journal.update_trade(t["id"], post_exit={
            "further_drop_24h_pct": round((px - min(x[3] for x in c[:25])) / px * 100, 2) if c else None,
            "rise_against_24h_pct": round((max(x[2] for x in c[:25]) - px) / px * 100, 2) if c else None,
            "ret_24h_pct": round((px - c[24][4]) / px * 100, 2) if len(c) > 24 else None})
        n += 1
        if n >= limit:
            break
    return n


def funding_carry_24h(features: dict[str, Any]) -> float:
    """Estimated funding a short collects over 24h (%), holding the scan-time rate: positive funding pays shorts."""
    rate, iv = features.get("funding_rate"), features.get("funding_interval_h") or 8
    return rate * 100 * 24 / iv if isinstance(rate, (int, float)) and not isinstance(rate, bool) else 0.0


def _r(x: float | None, n: int = 2) -> float | None:
    return None if x is None else round(x, n)


def _ci(c: dict[str, Any]) -> list[float] | None:
    return [round(c["ci95"][0], 2), round(c["ci95"][1], 2)] if c and c.get("ci95") else None


COMPACT_DROP = ("n_eff", "ci95_short_ret_24h", "avg_net_ret_24h", "descriptive_only")


def _summary(vals: list[dict[str, Any]], compact: bool = False) -> dict[str, Any]:
    """One observation per symbol-episode; existing keys keep their names and units, on that basis. compact (the
    exploratory feature tertiles): existing keys + rows, days and the excess return with its CI."""
    rows = [v for v in vals if v.get("short_ret_24h") is not None]
    obs = rs.episodes(rows, HORIZON_H)
    if not obs:
        return {"n": 0, "rows": 0}
    r24 = [o["short_ret_24h"] for o in obs]
    adv = [o["max_adverse_24h"] for o in obs if o.get("max_adverse_24h") is not None]
    days = rs.days_of(obs)
    raw = rs.clustered_mean(r24, days)
    ex = [(o["excess_ret_24h"], d) for o, d in zip(obs, days) if o.get("excess_ret_24h") is not None]
    exc = rs.clustered_mean([e for e, _ in ex], [d for _, d in ex]) if ex else None
    net = [o["short_ret_24h"] - COST_PCT + funding_carry_24h(o.get("features") or {}) for o in obs]
    out = {"n": len(obs), "rows": len(rows), "days": raw["days"], "n_eff": _r(raw["n_eff"], 1),
           "avg_short_ret_24h": round(raw["mean"], 2), "ci95_short_ret_24h": _ci(raw),
           "median_short_ret_24h": round(statistics.median(r24), 2),
           "hit_rate_pct": round(sum(x > 0 for x in r24) / len(r24) * 100, 1),
           "avg_excess_ret_24h": _r(exc and exc["mean"]), "ci95_excess_ret_24h": _ci(exc),
           "avg_net_ret_24h": round(statistics.fmean(net), 2),
           "avg_max_adverse_24h": round(statistics.mean(adv), 2) if adv else None,
           "squeeze_rate_pct": round(sum(a > SQUEEZE_PCT for a in adv) / len(adv) * 100, 1) if adv else None}
    if raw["days"] < rs.MIN_DAYS:
        out["descriptive_only"] = True
    return {k: v for k, v in out.items() if k not in COMPACT_DROP} if compact else out


def _rows_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The pre-2026-10-01 basis: every scan row counted, so n overstates the evidence. Kept for comparison."""
    r24 = [r["short_ret_24h"] for r in rows]
    adv = [r["max_adverse_24h"] for r in rows if r.get("max_adverse_24h") is not None]
    return {"n": len(r24), "avg_short_ret_24h": round(statistics.mean(r24), 2),
            "hit_rate_pct": round(sum(x > 0 for x in r24) / len(r24) * 100, 1),
            "squeeze_rate_pct": round(sum(a > SQUEEZE_PCT for a in adv) / len(adv) * 100, 1) if adv else None,
            "note": "row-based (every rescan counted): overstates n; compare, don't cite"}


def label_mix(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Which label method the reported rows carry, so a half-relabelled dataset is visible."""
    legacy = [r for r in rows if r.get("label_v") is None]
    out = {"legacy_labels": len(legacy), f"v{LABEL_VERSION}_labels": sum(r.get("label_v") == LABEL_VERSION
                                                                           for r in rows),
           "legacy_kept": sum((r.get("label_tries") or 0) >= LABEL_MAX_TRIES for r in legacy)}
    if legacy and len(legacy) < len(rows):
        out["label_note"] = (f"mixed: legacy = 1h bars from the scan's floored hour, v{LABEL_VERSION} = 5m bars from "
                             f"the scan; legacy_kept = relabel gave up after {LABEL_MAX_TRIES} failed fetches")
    return out


def veto_key(v: str) -> str:
    """Veto reason without its numbers, so 'OI $4.4M < $30M: too thin' and 'OI $8.1M ...' are one veto."""
    return re.sub(r"-?\d+(?:[.,]\d+)*", "#", v.split(":")[0].split(" (")[0])[:40]


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _tertiles(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    """Cut points from one value per symbol-episode, so rescans of a coin don't move them."""
    have = [r for r in rows if _num(r["features"].get(key))]
    vals = sorted(r["features"][key] for r in rs.episodes(have, HORIZON_H))
    if len(vals) < 9:
        return {}
    lo, hi = vals[len(vals) // 3], vals[2 * len(vals) // 3]
    buckets: dict[str, list[dict[str, Any]]] = {f"<{lo:.3g}": [], f"{lo:.3g}-{hi:.3g}": [], f">{hi:.3g}": []}
    names = list(buckets)
    for r in have:
        v = r["features"][key]
        buckets[names[0] if v < lo else names[2] if v > hi else names[1]].append(r)
    return {k: _summary(v, compact=True) for k, v in buckets.items()}


def archetype_of(features: dict[str, Any]) -> str:
    """lifecycle stage x the game on the tape. 'unclassified' = no known script fits: logged, not forced into one,
    so new scripts (including a coin's own habits) can surface from the data."""
    from .tape import classify
    stage = features.get("lifecycle_stage") or "unknown_stage"
    game = classify(features)["state"]
    if game == "no_edge" and stage in ("mature", "young", "unknown_stage"):
        return "unclassified"
    return f"{stage}|{game}"


def archetypes(rows: list[dict[str, Any]]) -> dict[str, dict[str, list]]:
    out: dict[str, dict[str, list]] = {"lifecycle": {}, "controller": {}, "archetype": {}, "session": {}}
    for r in rows:
        f = r["features"]
        out["lifecycle"].setdefault(f.get("lifecycle_stage") or "untagged", []).append(r)
        out["controller"].setdefault(f.get("controller_label") or "untagged", []).append(r)
        out["archetype"].setdefault(archetype_of(f), []).append(r)
        out["session"].setdefault(f.get("session_bucket") or "untagged", []).append(r)
    return out


OI_BUCKETS_M = (0, 5, 10, 20, 30, 60, 150, float("inf"))


def by_liquidity(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """How low into the trash zone can we go? Outcomes by open-interest bucket (USD millions), vetoed coins included,
    so the $30M floor gets judged by data: it pays below a bucket only if shorts there fall (avg_short_ret > costs,
    ~0.1%) WITHOUT a high squeeze_rate (max adverse move > 15% within 24h)."""
    out: dict[str, list] = {}
    for r in rows:
        oi = r["features"].get("oi_value")
        if not isinstance(oi, (int, float)):
            continue
        m = oi / 1e6
        i = next(k for k in range(len(OI_BUCKETS_M) - 1) if m < OI_BUCKETS_M[k + 1])
        lo, hi = OI_BUCKETS_M[i], OI_BUCKETS_M[i + 1]
        out.setdefault(f"{lo:g}-{hi:g}M" if hi != float("inf") else f">{lo:g}M", []).append(r)
    return {k: _summary(v) for k, v in sorted(out.items(), key=lambda kv: float(kv[0].strip(">").split("-")[0].rstrip("M")))}


def report(journal, min_n: int = 5) -> dict[str, Any]:
    """What predicted drops? Grouped by tradable/vetoed, veto reason, setup, score bucket, feature tertiles.
    n = symbol-episodes (min_n applies to them); rows = raw scan rows."""
    rows = [r for r in journal.candidates(labelled_only=True, limit=ALL_ROWS) if r.get("short_ret_24h") is not None]
    if not rows:
        return {"labelled_candidates": 0, "note": "No labelled candidates yet: outcomes are labelled 72h after "
                "each scan by the guardian. Keep scanning."}
    for r, e in zip(rows, rs.scan_excess(rows)):
        r["excess_ret_24h"] = e
    by_veto: dict[str, list] = {}
    by_setup: dict[str, list] = {}
    for r in rows:
        for v in r["vetoes"] or []:
            by_veto.setdefault(veto_key(v), []).append(r)
        for s in r["setups"] or ["none"]:
            by_setup.setdefault(s, []).append(r)
    score_b: dict[str, list] = {}
    for r in rows:
        b = "0-29" if (r["score"] or 0) < 30 else "30-59" if r["score"] < 60 else "60+"
        score_b.setdefault(b, []).append(r)
    keep = lambda d: {k: s for k, s in ((k, _summary(v)) for k, v in d.items()) if s["n"] >= min_n}  # noqa: E731
    arch = archetypes(rows)
    numeric = sorted({k for r in rows for k, v in r["features"].items() if _num(v) and k not in NUMERIC_SKIP})
    tert = {k: t for k in numeric if (t := _tertiles(rows, k))}
    n_buckets = sum(1 for t in tert.values() for b in t.values() if b["n"])
    allx = _summary(rows)
    return {
        "labelled_candidates": len(rows), "basis": BASIS,
        "sample": {"rows": len(rows), "episodes": allx["n"], "days": allx["days"], "n_eff": allx["n_eff"],
                   **label_mix(rows),
                   "episode_rule": f"a symbol's first appearance plus its rescans within {HORIZON_H}h (overlapping "
                                   "outcome windows) = one observation"},
        "baseline": {"kind": "same_scan_median", "field": "short_ret_24h", "min_symbols": rs.MIN_SCAN_SYMBOLS,
                     "note": "excess = short_ret_24h minus the median of the candidates in the same scan: removes "
                             "the shared market move. 'all' excess is ~0 by construction: judge the pool's absolute "
                             "edge by avg_short_ret_24h and its ci95"},
        "costs": {"round_trip_pct": COST_PCT, "funding": "estimated carry: scan-time rate held 24h (shorts collect "
                  "positive funding)", "field": "avg_net_ret_24h"},
        "inference": rs.inference_note(allx["days"]),
        "all": allx, "all_rows": _rows_summary(rows),
        "tradable": _summary([r for r in rows if r["tradable"]]),
        "vetoed": _summary([r for r in rows if not r["tradable"]]),
        "by_veto_reason": keep(by_veto), "by_setup": keep(by_setup), "by_score": keep(score_b),
        "by_lifecycle": keep(arch["lifecycle"]), "by_controller": keep(arch["controller"]),
        "by_archetype": keep(arch["archetype"]), "by_session": keep(arch["session"]),
        "by_open_interest": by_liquidity(rows),
        "feature_mining": {"features": len(tert), "buckets": n_buckets,
                           "expected_false_hits_95": round(n_buckets * 0.05, 1),
                           "note": "exploratory: every numeric feature is mined into tertiles, so ~5% of buckets "
                                   "show a ci95 excluding 0 by chance alone. Confirm on later data before acting. "
                                   "Buckets are compact (no n_eff, raw-return CI or net): dig with shortbot.lab."},
        "by_feature_tertile": tert,
        "how_to_read": "short_ret > 0 = price fell (short wins). n = independent symbol-episodes (rescans of a coin "
                       "within 24h count once), rows = raw scan rows, days = distinct UTC days. Compare groups by "
                       "avg_excess_ret_24h (vs the same scan's median) and its ci95; descriptive_only = fewer than "
                       f"{rs.MIN_DAYS} days. avg_net_ret_24h = after costs. A veto is useful if its vetoed coins "
                       "show a low short_ret or high squeeze_rate. Treat n < 30 episodes as anecdote.",
    }
