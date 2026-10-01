"""Calibration and expectancy from labelled predictions (docs/PROBABILITY.md):
- Brier score + Murphy decomposition (reliability, resolution, uncertainty) for the model and for the analyst
- Brier skill score of the analyst vs the no-edge baseline (is our judgement adding information?)
- expectancy tables by group with Wilson intervals: observed P(target first) vs the no-edge baseline p.
  observed − baseline = the measured edge (drift); its CI says whether it is real yet."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0.0, c - h), 4), round(min(1.0, c + h), 4))


def brier(rows: list[dict], key: str) -> dict[str, Any] | None:
    xs = [(r[key], 1.0 if r["_y"] else 0.0) for r in rows if r.get(key) is not None]
    if not xs:
        return None
    n = len(xs)
    bs = sum((p - y) ** 2 for p, y in xs) / n
    base = sum(y for _, y in xs) / n
    buckets: dict[int, list[tuple[float, float]]] = {}
    for p, y in xs:
        buckets.setdefault(min(int(p * 10), 9), []).append((p, y))
    rel = sum(len(b) * (sum(p for p, _ in b) / len(b) - sum(y for _, y in b) / len(b)) ** 2 for b in buckets.values()) / n
    res = sum(len(b) * (sum(y for _, y in b) / len(b) - base) ** 2 for b in buckets.values()) / n
    diagram = [{"bucket": f"{k / 10:.1f}-{(k + 1) / 10:.1f}", "n": len(b), "mean_p": round(sum(p for p, _ in b) / len(b), 3),
                "observed": round(sum(y for _, y in b) / len(b), 3), "ci": wilson(int(sum(y for _, y in b)), len(b))}
               for k, b in sorted(buckets.items())]
    return {"n": n, "brier": round(bs, 4), "reliability": round(rel, 4), "resolution": round(res, 4),
            "uncertainty": round(base * (1 - base), 4), "reliability_diagram": diagram}


def _y(r: dict) -> bool:
    return r["outcome"] in ("target", "hit")


def expectancy(rows: list[dict], group: Callable[[dict], Any], min_n: int = 5) -> dict[str, Any]:
    """Per group: observed P(target first) with Wilson CI vs mean no-edge baseline p, and realized R."""
    groups: dict[Any, list[dict]] = {}
    for r in rows:
        try:
            groups.setdefault(group(r), []).append(r)
        except (KeyError, TypeError):
            continue
    out = {}
    for g, rs in groups.items():
        if len(rs) < min_n:
            continue
        k = sum(1 for r in rs if _y(r))
        base = [r["p_model"] for r in rs if r.get("p_model") is not None]
        rr = []
        for r in rs:
            reward = (r["entry"] - r["target"]) / (r["stop"] - r["entry"]) if r.get("stop") and r["stop"] > r["entry"] else 0
            rr.append(reward if r["outcome"] == "target" else -1.0 if r["outcome"] == "stop" else 0.0)
        obs, ci = k / len(rs), wilson(k, len(rs))
        b = sum(base) / len(base) if base else None
        out[str(g)] = {"n": len(rs), "p_target_observed": round(obs, 3), "ci95": ci,
                       "p_baseline": round(b, 3) if b is not None else None,
                       "edge_pp": round((obs - b) * 100, 1) if b is not None else None,
                       "edge_significant": b is not None and (ci[0] > b or ci[1] < b),
                       "avg_R_ex_time": round(sum(rr) / len(rr), 3)}
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
        bss = round(1 - ba["brier"] / bm["brier"], 3) if bm and bm["brier"] else None
    ctx = lambda k: (lambda r: r["context"].get(k, "n/a"))  # noqa: E731
    return {
        "labelled_brackets": len(rows), "labelled_touches": len(touch),
        "model_calibration": model, "analyst_calibration": analyst,
        "analyst_skill_vs_baseline": bss,
        "fill_model_calibration": brier(touch, "p_model"),
        "expectancy_by_setup": expectancy(rows, ctx("setup")),
        "expectancy_by_tape_state": expectancy(rows, ctx("tape_state")),
        "expectancy_by_regime": expectancy(rows, ctx("regime")),
        "expectancy_by_source": expectancy(rows, lambda r: r["source"]),
        "how_to_read": "edge_pp = observed P(target first) − no-edge baseline. Only edge_significant groups show real "
                       "drift; everything else is geometry. Sizing may use p only after the gate in docs/PROBABILITY.md.",
    }
