"""Calibration and expectancy from labelled predictions (docs/PROBABILITY.md):
- Brier score + Murphy decomposition (reliability, resolution, uncertainty) for the model and for the analyst
- Brier skill score of the analyst vs the no-edge baseline (is our judgement adding information?)
- expectancy tables by group: observed P(target first) vs the no-edge baseline p. observed − baseline = the
  measured edge (drift); its CI says whether it is real yet.
Overlap (#27): a research bracket is logged on every scan, so one coin's 24h brackets overlap. Expectancy counts one
observation per (source, symbol) episode; every interval is a Wilson CI clustered by UTC day (research_stats)."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from ..research_stats import MIN_DAYS, clustered_wilson, days_of, episodes, inference_note


def brier(rows: list[dict], key: str) -> dict[str, Any] | None:
    have = [r for r in rows if r.get(key) is not None]
    xs = [(r[key], 1.0 if r["_y"] else 0.0, d) for r, d in zip(have, days_of(have), strict=True)]
    if not xs:
        return None
    n = len(xs)
    bs = sum((p - y) ** 2 for p, y, _ in xs) / n
    base = sum(y for _, y, _ in xs) / n
    buckets: dict[int, list[tuple[float, float, Any]]] = {}
    for p, y, d in xs:
        buckets.setdefault(min(int(p * 10), 9), []).append((p, y, d))
    rel = sum(len(b) * (sum(x[0] for x in b) / len(b) - sum(x[1] for x in b) / len(b)) ** 2 for b in buckets.values()) / n
    res = sum(len(b) * (sum(x[1] for x in b) / len(b) - base) ** 2 for b in buckets.values()) / n
    diagram = [{"bucket": f"{k / 10:.1f}-{(k + 1) / 10:.1f}", "n": len(b), "days": len({x[2] for x in b}),
                "mean_p": round(sum(x[0] for x in b) / len(b), 3), "observed": round(sum(x[1] for x in b) / len(b), 3),
                "ci": clustered_wilson([x[1] for x in b], [x[2] for x in b])["ci95"]}
               for k, b in sorted(buckets.items())]
    return {"n": n, "brier": round(bs, 4), "reliability": round(rel, 4), "resolution": round(res, 4),
            "uncertainty": round(base * (1 - base), 4), "reliability_diagram": diagram}


def _y(r: dict) -> bool:
    return r["outcome"] in ("target", "hit")


def _episode_key(r: dict) -> tuple | None:
    return (r.get("source"), r["symbol"]) if r.get("symbol") else None


def expectancy(rows: list[dict], group: Callable[[dict], Any], min_n: int = 5) -> dict[str, Any]:
    """Per group: observed P(target first) vs mean no-edge baseline p, and realized R. n = (source, symbol)
    episodes (a bracket's own horizon is the window), rows = raw predictions; ci95 clustered by day."""
    groups: dict[Any, list[dict]] = {}
    for r in rows:
        try:
            groups.setdefault(group(r), []).append(r)
        except (KeyError, TypeError):
            continue
    out = {}
    for g, grs in groups.items():
        eps = episodes(grs, lambda r: r.get("hours") or 24, key=_episode_key)
        if len(eps) < min_n:
            continue
        c = clustered_wilson([_y(r) for r in eps], days_of(eps))
        base = [r["p_model"] for r in eps if r.get("p_model") is not None]
        rr = []
        for r in eps:
            reward = (r["entry"] - r["target"]) / (r["stop"] - r["entry"]) if r.get("stop") and r["stop"] > r["entry"] else 0
            rr.append(reward if r["outcome"] == "target" else -1.0 if r["outcome"] == "stop" else 0.0)
        obs, ci = c["p"], c["ci95"]
        b = sum(base) / len(base) if base else None
        out[str(g)] = {"n": len(eps), "rows": len(grs), "days": c["days"],
                       "n_eff": round(c["n_eff"], 1) if c["n_eff"] is not None else None,
                       "p_target_observed": round(obs, 3), "ci95": ci,
                       "p_baseline": round(b, 3) if b is not None else None,
                       "edge_pp": round((obs - b) * 100, 1) if b is not None else None,
                       "edge_significant": bool(b is not None and ci and (ci[0] > b or ci[1] < b)),
                       "avg_R_ex_time": round(sum(rr) / len(rr), 3)}
        if c["days"] < MIN_DAYS:
            out[str(g)]["descriptive_only"] = True
    return out


SIGMA_KEYS = {"used": lambda r: r.get("sigma_h"), "ewma": lambda r: r["context"].get("sigma_ewma"),
              "har": lambda r: r["context"].get("sigma_har")}


def qlike(rows: list[dict]) -> dict[str, Any] | None:
    """σ forecasts scored against the realized variance of their own window (Patton 2011: QLIKE is robust to a noisy
    variance proxy): L = RV/σ² − ln(RV/σ²) − 1, 0 when exact; lower wins. Each estimator is scored on the rows where
    every estimator logged a value, so the comparison is paired. median_rv_ratio < 1 = the forecast is too wide."""
    have = [r for r in rows if r.get("rv_h") and all((f(r) or 0) > 0 for f in SIGMA_KEYS.values())]
    if not have:
        have = [r for r in rows if r.get("rv_h") and (r.get("sigma_h") or 0) > 0]
        keys = {"used": SIGMA_KEYS["used"]}
    else:
        keys = SIGMA_KEYS
    if not have:
        return None
    out: dict[str, Any] = {"n": len(have), "days": len({d for d in days_of(have) if isinstance(d, str)}),
                           "paired": keys is SIGMA_KEYS}
    for name, f in keys.items():
        ratios = sorted(r["rv_h"] / f(r) ** 2 for r in have)
        out[name] = {"qlike": round(sum(x - math.log(x) - 1 for x in ratios) / len(ratios), 4),
                     "median_rv_ratio": round(ratios[len(ratios) // 2], 3)}
    return out


def barrier_calibration(rows: list[dict]) -> dict[str, Any]:
    """The other two outcomes of each bracket: P(stop first) and P(time) logged in context vs what happened."""
    out = {}
    for key, outcome in (("p_stop", "stop"), ("p_time", "time")):
        xs = [{**r, key: r["context"][key], "_y": r["outcome"] == outcome} for r in rows
              if r["context"].get(key) is not None]
        out[outcome] = brier(xs, key)
    return out


def report(journal) -> dict[str, Any]:
    rows = [r for r in journal.predictions(labelled_only=True) if r["kind"] == "bracket"]
    touch = [r for r in journal.predictions(labelled_only=True) if r["kind"] == "touch"]
    for r in rows + touch:
        r["_y"] = _y(r)
    if not rows and not touch:
        return {"labelled": 0, "note": "no labelled predictions yet (labels arrive after each prediction's horizon)"}
    model, analyst = brier(rows, "p_model"), brier(rows, "p_analyst")
    both = [r for r in rows if r.get("p_analyst") is not None]
    bss = None
    if both:
        bm, ba = brier(both, "p_model"), brier(both, "p_analyst")
        bss = round(1 - ba["brier"] / bm["brier"], 3) if ba and bm and bm["brier"] else None   # (ba: never None here)
    ctx = lambda k: (lambda r: r["context"].get(k, "n/a"))  # noqa: E731
    days = len({d for d in days_of(rows) if isinstance(d, str)})
    return {
        "labelled_brackets": len(rows), "labelled_touches": len(touch), "days": days,
        "inference": inference_note(days) + "; Wilson on the day-clustered, df-adjusted effective n",
        "model_calibration": model, "analyst_calibration": analyst,
        "analyst_skill_vs_baseline": bss,
        "fill_model_calibration": brier(touch, "p_model"),
        "barrier_calibration": barrier_calibration(rows),
        "sigma_qlike": qlike(rows),
        "sigma_scale": _scale_verdict(journal),
        "expectancy_by_setup": expectancy(rows, ctx("setup")),
        "expectancy_by_tape_state": expectancy(rows, ctx("tape_state")),
        "expectancy_by_regime": expectancy(rows, ctx("regime")),
        "expectancy_by_source": expectancy(rows, lambda r: r["source"]),
        "how_to_read": "edge_pp = observed P(target first) − no-edge baseline. Only edge_significant groups show real "
                       "drift; everything else is geometry. n = (source, symbol) episodes: overlapping brackets of one "
                       f"coin count once; descriptive_only = fewer than {MIN_DAYS} days. Sizing may use p only after "
                       "the gate in docs/PROBABILITY.md.",
    }


def _scale_verdict(journal) -> dict[str, Any] | None:
    """The stored σ-scale walk-forward verdict (recal.py), without its per-fold detail."""
    import json

    from .recal import KV
    try:
        v = json.loads(journal.get(KV) or "null")
    except Exception:  # noqa: BLE001 - a report never fails on a stored verdict
        return None
    keys = ("applied", "scale", "fitted_scale", "rows", "n_eff", "why", "fitted_at")
    return {k: v.get(k) for k in keys} if v else None
