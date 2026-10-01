"""Research engine: label logged scan candidates with what a short would actually have done, and report
which scores / setups / vetoes / features predicted drops. Works on every scan, traded or not, so the
desk learns even on days it doesn't trade. New signal features are reported automatically.

Short returns are positive when price FELL (good for a short): short_ret = (p0 - p_t) / p0 * 100.
max_adverse_24h = worst rise against the short within 24h (squeeze risk); max_favorable_24h = best drop.
"""

from __future__ import annotations

import statistics
from typing import Any

from .exchange import Exchanges

HOUR_MS = 3_600_000
NUMERIC_SKIP = {"funding_interval_h"}


def outcome_from_candles(p0: float, candles: list[list[float]]) -> dict[str, float | None]:
    """candles: 1h OHLCV starting at the candidate's hour."""
    def ret_at(h: int) -> float | None:
        return (p0 - candles[h][4]) / p0 * 100 if len(candles) > h else None
    first24 = candles[:25]
    return {
        "short_ret_4h": ret_at(4), "short_ret_24h": ret_at(24), "short_ret_72h": ret_at(72),
        "max_adverse_24h": (max(c[2] for c in first24) - p0) / p0 * 100 if first24 else None,
        "max_favorable_24h": (p0 - min(c[3] for c in first24)) / p0 * 100 if first24 else None,
    }


async def label_outcomes(ex: Exchanges, journal, now_ms: int, limit: int = 30) -> int:
    """Label candidates at least 72h old (all horizons at once). Returns how many were labelled."""
    rows = journal.unlabelled_candidates(older_than_ms=now_ms - 73 * HOUR_MS, limit=limit)
    n = 0
    for r in rows:
        try:
            candles = await ex.public.fetch_ohlcv(r["symbol"], "1h", since=r["ts_ms"] - r["ts_ms"] % HOUR_MS,
                                                  limit=80)
            outcome = outcome_from_candles(r["price"], candles) if candles and r["price"] else {}
        except Exception:  # noqa: BLE001 - delisted/renamed symbols: label empty so we don't retry forever
            outcome = {}
        journal.label_candidate(r["id"], outcome)
        n += 1
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


def _summary(vals: list[dict[str, Any]]) -> dict[str, Any]:
    r24 = [v["short_ret_24h"] for v in vals if v.get("short_ret_24h") is not None]
    adv = [v["max_adverse_24h"] for v in vals if v.get("max_adverse_24h") is not None]
    if not r24:
        return {"n": 0}
    return {"n": len(r24), "avg_short_ret_24h": round(statistics.mean(r24), 2),
            "median_short_ret_24h": round(statistics.median(r24), 2),
            "hit_rate_pct": round(sum(x > 0 for x in r24) / len(r24) * 100, 1),
            "avg_max_adverse_24h": round(statistics.mean(adv), 2) if adv else None,
            "squeeze_rate_pct": round(sum(a > 15 for a in adv) / len(adv) * 100, 1) if adv else None}


def _tertiles(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    vals = sorted(r["features"][key] for r in rows if isinstance(r["features"].get(key), (int, float))
                  and not isinstance(r["features"].get(key), bool))
    if len(vals) < 9:
        return {}
    lo, hi = vals[len(vals) // 3], vals[2 * len(vals) // 3]
    buckets: dict[str, list[dict[str, Any]]] = {f"<{lo:.3g}": [], f"{lo:.3g}-{hi:.3g}": [], f">{hi:.3g}": []}
    names = list(buckets)
    for r in rows:
        v = r["features"].get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            buckets[names[0] if v < lo else names[2] if v > hi else names[1]].append(r)
    return {k: _summary(v) for k, v in buckets.items()}


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
    """What predicted drops? Grouped by tradable/vetoed, veto reason, setup, score bucket, feature tertiles."""
    rows = [r for r in journal.candidates(labelled_only=True) if r.get("short_ret_24h") is not None]
    if not rows:
        return {"labelled_candidates": 0, "note": "No labelled candidates yet: outcomes are labelled 72h after "
                "each scan by the guardian. Keep scanning."}
    by_veto: dict[str, list] = {}
    by_setup: dict[str, list] = {}
    for r in rows:
        for v in r["vetoes"] or []:
            by_veto.setdefault(v.split(":")[0].split(" (")[0][:40], []).append(r)
        for s in r["setups"] or ["none"]:
            by_setup.setdefault(s, []).append(r)
    score_b: dict[str, list] = {}
    for r in rows:
        b = "0-29" if (r["score"] or 0) < 30 else "30-59" if r["score"] < 60 else "60+"
        score_b.setdefault(b, []).append(r)
    keep = lambda d: {k: s for k, s in ((k, _summary(v)) for k, v in d.items()) if s["n"] >= min_n}  # noqa: E731
    arch = archetypes(rows)
    numeric = sorted({k for r in rows for k, v in r["features"].items()
                      if isinstance(v, (int, float)) and not isinstance(v, bool) and k not in NUMERIC_SKIP})
    return {
        "labelled_candidates": len(rows), "all": _summary(rows),
        "tradable": _summary([r for r in rows if r["tradable"]]),
        "vetoed": _summary([r for r in rows if not r["tradable"]]),
        "by_veto_reason": keep(by_veto), "by_setup": keep(by_setup), "by_score": keep(score_b),
        "by_lifecycle": keep(arch["lifecycle"]), "by_controller": keep(arch["controller"]),
        "by_archetype": keep(arch["archetype"]), "by_session": keep(arch["session"]),
        "by_open_interest": by_liquidity(rows),
        "by_feature_tertile": {k: t for k in numeric if (t := _tertiles(rows, k))},
        "how_to_read": "short_ret > 0 = price fell (short wins). A veto is useful if its vetoed coins show a low "
                       "short_ret or high squeeze_rate. Treat n < 30 as anecdote.",
    }
