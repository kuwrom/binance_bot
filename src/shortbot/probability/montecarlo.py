"""Filtered historical simulation (Barone-Adesi et al. 1999) for first-passage probabilities:
1. standardise the coin's own 5m returns by their EWMA vol  -> residuals with the real tail shape
2. bootstrap of the residuals. Default mean_block=1 (i.i.d.): the NO-EDGE baseline must be a martingale, and
   block bootstrap (Politis & Romano 1994, mean_block>1) replays the coin's own serial pattern (e.g. a
   blow-off's mean reversion), which is a historical drift structure = an unvalidated "signal", not a baseline
3. rescale by the CURRENT vol forecast and simulate 5m steps
4. Brownian-bridge correction between steps (Glasserman 2004 §6.4) so intrabar touches aren't missed
Residuals are de-meaned: no drift is assumed (this is the no-edge baseline with realistic tails)."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .vol import BARS_PER_HOUR_5M, ewma_sigma_series, log_returns


def standardized_residuals(closes_5m: list[float]) -> np.ndarray:
    r = log_returns(closes_5m)
    if len(r) < 60:
        return np.zeros(0)
    s = ewma_sigma_series(r)
    z = r / np.where(s > 0, s, np.nan)
    z = z[np.isfinite(z)]
    return (z - z.mean()) / (z.std() or 1.0)


def stationary_bootstrap(z: np.ndarray, n_paths: int, n_steps: int, mean_block: float,
                         rng: np.random.Generator) -> np.ndarray:
    """(n_paths, n_steps) draws: blocks of geometric length (mean `mean_block`) with random starts, wrapping."""
    n = len(z)
    idx = np.empty((n_paths, n_steps), dtype=np.int64)
    idx[:, 0] = rng.integers(0, n, n_paths)
    restart = rng.random((n_paths, n_steps)) < 1.0 / mean_block
    starts = rng.integers(0, n, (n_paths, n_steps))
    for t in range(1, n_steps):
        idx[:, t] = np.where(restart[:, t], starts[:, t], (idx[:, t - 1] + 1) % n)
    return z[idx]


# Memory cap: every (paths x steps) array costs ~100 B/cell across the temporaries (int64 index, shocks, log paths,
# prices, bridge draws). A 750 h unlock time stop at 10k paths was 90M cells (~5 GB) and the host OOM-killed the
# gateway mid-entry (2026-10-04, KITE). Paths shrink first (down to MC_MIN_PATHS), then the horizon is cut; the
# closed-form baseline (baseline.py) still covers the full horizon.
MC_MAX_CELLS = 5_000_000
MC_MIN_PATHS = 2_000


def mc_grid(n_paths: int, steps: int) -> tuple[int, int]:
    """(paths, steps) with paths * steps <= MC_MAX_CELLS: fewer paths first, then a shorter horizon."""
    if n_paths * steps <= MC_MAX_CELLS:
        return n_paths, steps
    n_paths = max(min(n_paths, MC_MIN_PATHS), MC_MAX_CELLS // steps)
    return n_paths, max(1, min(steps, MC_MAX_CELLS // n_paths))


def simulate_short(closes_5m: list[float], entry: float, stop: float, targets: list[float], hours: float,
                   sigma_hourly: float, n_paths: int = 10_000, mean_block: float = 1.0,
                   seed: int | None = 7, bridge: bool = True) -> dict:
    """For a short at `entry`: P(each target is reached before the stop within `hours`), P(stop first), P(time).
    Targets are checked independently against the same paths (each 'target before stop')."""
    z = standardized_residuals(closes_5m)
    if len(z) == 0 or sigma_hourly <= 0:
        return {"ok": False, "error": "not enough 5m history for simulation"}
    rng = np.random.default_rng(seed)
    steps = max(1, int(round(hours * BARS_PER_HOUR_5M)))
    n_paths, steps = mc_grid(n_paths, steps)
    hours_simulated = steps / BARS_PER_HOUR_5M
    sig_step = sigma_hourly / math.sqrt(BARS_PER_HOUR_5M)
    shocks = stationary_bootstrap(z, n_paths, steps, mean_block, rng) * sig_step
    # exact price-martingale correction for the EMPIRICAL residuals: E[exp(shock − c)] = 1. (−σ²/2 is only right
    # for Gaussian shocks; skewed/fat-tailed residuals would otherwise inject a spurious drift = fake edge.)
    shocks -= math.log(float(np.mean(np.exp(z * sig_step))))
    x = np.concatenate([np.zeros((n_paths, 1)), np.cumsum(shocks, axis=1)], axis=1)   # log price paths
    u = math.log(stop / entry)

    def first_hit(level_log: float, upper: bool) -> np.ndarray:
        """Index of the first step whose bridge crosses the level (steps+1 if never)."""
        a, b = x[:, :-1], x[:, 1:]
        if upper:
            crossed = b >= level_log
            da, db = np.maximum(level_log - a, 0), np.maximum(level_log - b, 0)
        else:
            crossed = b <= level_log
            da, db = np.maximum(a - level_log, 0), np.maximum(b - level_log, 0)
        if not bridge:
            hit = crossed
        else:
            p_bridge = np.exp(-2 * da * db / sig_step**2)
            hit = crossed | (rng.random(a.shape) < p_bridge)
        return np.where(hit.any(axis=1), hit.argmax(axis=1), steps + 1)

    t_stop = first_hit(u, True)
    r_unit = stop - entry
    rows = np.arange(n_paths)
    price = entry * np.exp(x)

    def exit_price(t_idx: np.ndarray, level: float, upper: bool) -> np.ndarray:
        """Market fill after trigger: the price at the end of the crossing step, never better than the level
        (fat-tail gaps overshoot: that overshoot is the stop slippage)."""
        at = price[rows, np.minimum(t_idx + 1, steps)]
        return np.maximum(at, level) if upper else np.minimum(at, level)

    out: dict[str, Any]
    out = {"ok": True, "paths": n_paths, "hours": hours, "hours_simulated": round(hours_simulated, 2),
           "horizon_capped": hours_simulated < hours - 1e-9, "sigma_hourly": sigma_hourly,
           "p_stop_within_T": float(np.mean(t_stop <= steps)), "targets": []}
    for tgt in sorted(targets, reverse=True):
        t_tgt = first_hit(math.log(tgt / entry), False)
        win = (t_tgt < t_stop) & (t_tgt <= steps)
        lose = (t_stop < t_tgt) & (t_stop <= steps)
        pnl = np.where(win, entry - exit_price(t_tgt, tgt, False),
                       np.where(lose, entry - exit_price(t_stop, stop, True), entry - price[:, -1])) / r_unit
        stop_fill = exit_price(t_stop, stop, True)[lose]
        out["targets"].append({
            "target": tgt, "p_target_first": float(np.mean(win)), "p_stop_first": float(np.mean(lose)),
            "p_neither": float(np.mean(~win & ~lose)),
            "ev_R": float(np.mean(pnl)), "ev_R_se": float(np.std(pnl) / math.sqrt(n_paths)),
            "avg_stop_loss_R": float(np.mean((stop_fill - entry) / r_unit)) if len(stop_fill) else None,
            "se": float(math.sqrt(max(np.mean(win), 1e-9) * (1 - np.mean(win)) / n_paths))})
    return out
