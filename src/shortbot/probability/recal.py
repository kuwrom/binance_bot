"""σ-scale recalibration of the no-edge baseline (docs/PROBABILITY.md "Calibration"; research note 2026-10-09: the MLE
on 1,500 scan brackets put the σ scale at 0.85-0.9).

ONE parameter: σ_used = s x σ_forecast. The drift stays the price martingale (ν = −σ²/2): a fitted drift would be one
week's regime sold as a baseline, so it is never fitted. s maximises the three-outcome log-likelihood (target / stop /
time by the horizon) of labelled short brackets, and is APPLIED only when it lowers the log-loss out of sample on
purged walk-forward windows (each test window trains only on brackets whose labels ended before it starts) in most
folds and in total. Below ~1k effective samples a Platt map of P(target) is fitted beside it for comparison (Niculescu-
Mizil & Caruana 2005); it is reported, never applied: only the σ scale keeps the three probabilities consistent."""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any

import numpy as np

log = logging.getLogger("shortbot.probability")

KV = "prob_sigma_scale"
GRID = np.round(np.arange(0.5, 1.5001, 0.025), 3)
BOUNDS = (0.7, 1.3)          # an applied scale outside this band means broken inputs, not a calibration
MIN_ROWS, FOLDS, MAX_ROWS, TERMS = 300, 5, 4000, 120
PLATT_MAX_NEFF = 1000
CACHE_S = 3600.0
EPS = 1e-6
_cache: dict[int, tuple[float, float]] = {}   # id(journal) -> (read at, scale)


def three_way_vec(p0: np.ndarray, up: np.ndarray, lo: np.ndarray, sigma: np.ndarray, t: np.ndarray,
                  terms: int = TERMS) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """baseline.three_way over arrays, with the price-martingale drift ν = −σ²/2: (P up first, P down first, P time)."""
    nu = -(sigma**2) / 2
    u, lw = np.log(up / p0), np.log(lo / p0)
    w, y, s2 = u - lw, -lw, sigma**2
    k = 2 * nu / s2
    p_up_inf = (1 - np.exp(-k * lw)) / (np.exp(-k * u) - np.exp(-k * lw))
    n = np.arange(1, terms + 1)[None, :]
    kappa = n**2 * math.pi**2 * s2[:, None] / (2 * w[:, None] ** 2) + (nu**2 / (2 * s2))[:, None]
    decay = np.exp(-kappa * t[:, None]) / kappa
    sn = np.sin(n * math.pi * (y / w)[:, None])
    c = math.pi * s2 / w**2
    p_up = p_up_inf - c * np.exp(nu * (w - y) / s2) * (n * (-1.0) ** (n + 1) * sn * decay).sum(axis=1)
    p_dn = (1 - p_up_inf) - c * np.exp(-nu * y / s2) * (n * sn * decay).sum(axis=1)
    p_up, p_dn = np.clip(p_up, 0, 1), np.clip(p_dn, 0, 1)
    return p_up, p_dn, np.clip(1 - p_up - p_dn, 0, 1)


def usable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Labelled short brackets with a σ, oldest first; σ is the RAW forecast (the logged one over its scale)."""
    out = []
    for r in rows:
        ctx = r.get("context") or {}
        if (r.get("kind") == "bracket" and r.get("outcome") in ("target", "stop", "time") and r.get("sigma_h")
                and r.get("stop") and r.get("target") and r["stop"] > r["entry"] > r["target"] > 0):
            out.append({**r, "_sig": float(r["sigma_h"]) / float(ctx.get("sigma_scale") or 1.0)})
    out.sort(key=lambda r: r["ts_ms"])
    if len(out) > MAX_ROWS:   # evenly in time: deterministic
        out = [out[int(i)] for i in np.linspace(0, len(out) - 1, MAX_ROWS)]
    return out


class Arrays:
    def __init__(self, rows: list[dict[str, Any]]):
        f = lambda k: np.asarray([float(r[k]) for r in rows])  # noqa: E731
        self.p0, self.stop, self.tgt, self.t, self.sig = f("entry"), f("stop"), f("target"), f("hours"), f("_sig")
        self.idx = np.asarray([{"stop": 0, "target": 1, "time": 2}[r["outcome"]] for r in rows])
        self.n = len(rows)

    def take(self, m: np.ndarray) -> Arrays:
        a = object.__new__(Arrays)
        for k in ("p0", "stop", "tgt", "t", "sig", "idx"):
            setattr(a, k, getattr(self, k)[m])
        a.n = int(m.sum())
        return a

    def logloss(self, s: float) -> float:
        """Mean negative log-likelihood of the observed outcomes at σ x s."""
        if not self.n:
            return float("nan")
        p = np.stack(three_way_vec(self.p0, self.stop, self.tgt, self.sig * s, self.t), axis=1)
        return float(-np.mean(np.log(np.clip(p[np.arange(self.n), self.idx], EPS, 1))))


def fit_scale(a: Arrays) -> float:
    return float(GRID[int(np.argmin([a.logloss(float(s)) for s in GRID]))])


def platt(p_train: np.ndarray, y_train: np.ndarray, iters: int = 50) -> tuple[float, float]:
    """(a, b) of P = 1/(1+exp(-(a·logit p + b))), by Newton's method on the log-loss."""
    x = np.log(np.clip(p_train, EPS, 1 - EPS) / (1 - np.clip(p_train, EPS, 1 - EPS)))
    a, b = 1.0, 0.0
    for _ in range(iters):
        q = 1 / (1 + np.exp(-(a * x + b)))
        g = np.array([np.sum((q - y_train) * x), np.sum(q - y_train)])
        wq = q * (1 - q) + 1e-9
        h = np.array([[np.sum(wq * x * x), np.sum(wq * x)], [np.sum(wq * x), np.sum(wq)]]) + 1e-6 * np.eye(2)
        step = np.linalg.solve(h, g)
        a, b = a - step[0], b - step[1]
        if np.max(np.abs(step)) < 1e-8:
            break
    return float(a), float(b)


def _binary_ll(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(p, EPS, 1 - EPS)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def walk_forward(rows: list[dict[str, Any]], folds: int = FOLDS) -> dict[str, Any]:
    """Expanding-window, purged walk-forward: per test fold, s fitted on the brackets labelled before it began; the
    fold's log-loss at that s vs at 1.0. Platt (P(target) only) the same way while n_eff < PLATT_MAX_NEFF."""
    rows = usable(rows)
    n_eff = len({(r["symbol"], int(r["ts_ms"] // 86_400_000)) for r in rows})
    out: dict[str, Any] = {"rows": len(rows), "n_eff": n_eff, "folds": []}
    if len(rows) < MIN_ROWS:
        return {**out, "applied": False, "scale": 1.0, "why": f"{len(rows)} labelled brackets < {MIN_ROWS}"}
    a = Arrays(rows)
    ts = np.asarray([r["ts_ms"] for r in rows], dtype=float)
    ends = ts + a.t * 3_600_000
    edges = np.linspace(0, len(rows), folds + 2).astype(int)
    for k in range(1, folds + 1):
        test = np.zeros(len(rows), dtype=bool)
        test[edges[k]:edges[k + 1]] = True
        start = ts[edges[k]]
        train = ends < start   # purged: no training label overlaps the test window
        if train.sum() < MIN_ROWS // 2 or not test.any():
            continue
        s = fit_scale(a.take(train))
        te = a.take(test)
        fold: dict[str, Any] = {"train": int(train.sum()), "test": int(test.sum()), "test_start_ms": int(start),
                                "scale": s, "logloss_1": round(te.logloss(1.0), 5),
                                "logloss_s": round(te.logloss(s), 5)}
        if n_eff < PLATT_MAX_NEFF:
            tr = a.take(train)
            p_tr = three_way_vec(tr.p0, tr.stop, tr.tgt, tr.sig, tr.t)[1]
            p_te = three_way_vec(te.p0, te.stop, te.tgt, te.sig, te.t)[1]
            pa, pb = platt(p_tr, (tr.idx == 1).astype(float))
            x = np.log(np.clip(p_te, EPS, 1 - EPS) / (1 - np.clip(p_te, EPS, 1 - EPS)))
            yt = (te.idx == 1).astype(float)
            fold["platt"] = {"a": round(pa, 3), "b": round(pb, 3), "bin_logloss_raw": round(_binary_ll(p_te, yt), 5),
                             "bin_logloss_platt": round(_binary_ll(1 / (1 + np.exp(-(pa * x + pb))), yt), 5)}
        out["folds"].append(fold)
    fs = out["folds"]
    better = sum(f["logloss_s"] < f["logloss_1"] for f in fs)
    gain = sum((f["logloss_1"] - f["logloss_s"]) * f["test"] for f in fs)
    scale = fit_scale(a)
    ok = bool(fs) and better > len(fs) / 2 and gain > 0 and BOUNDS[0] <= scale <= BOUNDS[1]
    why = (f"out of sample better in {better}/{len(fs)} folds, total log-loss gain {gain:.3f}"
           + ("" if BOUNDS[0] <= scale <= BOUNDS[1] else f"; scale {scale} outside {BOUNDS}: inputs suspect"))
    return {**out, "scale": scale if ok else 1.0, "fitted_scale": scale, "applied": ok, "why": why}


def store(journal: Any, res: dict[str, Any]) -> None:
    """Keep a verdict (kv `prob_sigma_scale`): scale_for reads it."""
    journal.set(KV, json.dumps({**res, "fitted_at": int(time.time())}, default=float))
    _cache.pop(id(journal), None)


def refit(journal: Any) -> dict[str, Any]:
    """Fit on every labelled bracket and store the verdict, in one synchronous call (CLI, tests). The guardian runs
    walk_forward off its event loop and stores the result through the journal writer."""
    res = walk_forward(journal.predictions(labelled_only=True))
    store(journal, res)
    return res


def scale_for(journal: Any) -> float:
    """The applied σ scale (1.0 without a passing fit, a journal or a readable verdict); cached for an hour."""
    if journal is None:
        return 1.0
    hit = _cache.get(id(journal))
    if hit and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    try:
        v = json.loads(journal.get(KV) or "{}")
        s = float(v["scale"]) if v.get("applied") else 1.0
        s = s if BOUNDS[0] <= s <= BOUNDS[1] else 1.0
    except Exception as e:  # noqa: BLE001 - a broken verdict never changes σ
        log.warning("σ scale unread (%s: %s): 1.0", type(e).__name__, e)
        s = 1.0
    _cache[id(journal)] = (time.monotonic(), s)
    return s
