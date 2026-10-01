"""Evidence-gated size tilts (docs/RISK_GOVERNANCE.md). Every tilt starts neutral and only moves once the
journal proves it; conviction x cell is capped at `max_conviction_mult`.

- conviction  C 0.5x, B 1x, A 1.5x. A needs >= 30 closed trades of that setup with the lower 90% bound of
              mean R > 0, else it is downgraded to B. M = moonshot (sized from its own bucket, risk_budget.py)
- cell        regime cell = BTC trend (up/down) x BTC vol (high/low), fixed in advance. A setup's cell mean R is
              shrunk to the setup mean with weight n/(n+30); cut to 0.5x only if its 80% upper bound < 0
- streak      the hot-hand test (report only, no multiplier): permutation p-value of lag-1 R autocorrelation
              and E[R|prev win] - E[R|prev loss]. Needs 200+ trades to mean anything (Miller-Sanjurjo)
"""

from __future__ import annotations

import math
import random
import statistics
from typing import Any

CONVICTION = {"C": 0.5, "B": 1.0, "A": 1.5}
TIERS = ("C", "B", "A", "M")
MIN_A_TRADES = 30
CELL_PRIOR_N = 30


def regime_cell(btc_daily_closes: list[float], vol_now: float | None, vol_target: float | None) -> str | None:
    """Pre-declared cells: trend = last close vs 20-day mean; vol = 30d realized vs its 1y median."""
    if len(btc_daily_closes) < 20 or not vol_now or not vol_target:
        return None
    trend = "up" if btc_daily_closes[-1] > statistics.fmean(btc_daily_closes[-20:]) else "down"
    return f"{trend}/{'high' if vol_now > vol_target else 'low'}vol"


def _mean_se(xs: list[float]) -> tuple[float, float]:
    m = statistics.fmean(xs)
    return m, (statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else float("inf"))


def conviction_mult(tier: str, setup_rs: list[float]) -> tuple[float, str, str]:
    """(multiplier, effective tier, note)."""
    tier = (tier or "B").upper()
    if tier not in CONVICTION:
        raise ValueError(f"conviction must be one of {TIERS}")
    if tier != "A":
        return CONVICTION[tier], tier, ""
    if len(setup_rs) < MIN_A_TRADES:
        return 1.0, "B", f"A needs >= {MIN_A_TRADES} closed trades of this setup (have {len(setup_rs)}): sized as B"
    m, se = _mean_se(setup_rs)
    lo = m - 1.645 * se
    if lo <= 0:
        return 1.0, "B", f"A needs the setup's 90% lower bound of R > 0 (is {lo:+.2f}R): sized as B"
    return CONVICTION["A"], "A", f"A: setup mean {m:+.2f}R, 90% lower bound {lo:+.2f}R over {len(setup_rs)} trades"


def cell_mult(cell_rs: list[float], setup_rs: list[float]) -> tuple[float, str]:
    n = len(cell_rs)
    if n < CELL_PRIOR_N or len(setup_rs) < 2:
        return 1.0, f"cell has {n} trades (< {CELL_PRIOR_N}): neutral"
    w = n / (n + CELL_PRIOR_N)
    cm, cse = _mean_se(cell_rs)
    shrunk = w * cm + (1 - w) * statistics.fmean(setup_rs)
    upper = shrunk + 0.842 * cse * w
    if upper < 0:
        return 0.5, f"cell shrunk mean {shrunk:+.2f}R, 80% upper {upper:+.2f}R < 0: half size"
    return 1.0, f"cell shrunk mean {shrunk:+.2f}R: neutral"


def streak_test(rs: list[float], n_perm: int = 5000, seed: int = 7) -> dict[str, Any]:
    """Is there any evidence that outcomes cluster? Permutation test keeps the R distribution and breaks order."""
    n = len(rs)
    if n < 10:
        return {"n": n, "verdict": "too few trades"}

    def lag1(xs: list[float]) -> float:
        m = statistics.fmean(xs)
        den = sum((x - m) ** 2 for x in xs)
        return sum((a - m) * (b - m) for a, b in zip(xs, xs[1:])) / den if den else 0.0

    def cond_gap(xs: list[float]) -> float | None:
        aw = [b for a, b in zip(xs, xs[1:]) if a > 0]
        al = [b for a, b in zip(xs, xs[1:]) if a <= 0]
        return statistics.fmean(aw) - statistics.fmean(al) if aw and al else None

    obs, gap = lag1(rs), cond_gap(rs)
    rng, xs, hits = random.Random(seed), list(rs), 0
    for _ in range(n_perm):
        rng.shuffle(xs)
        hits += lag1(xs) >= obs
    p = (hits + 1) / (n_perm + 1)
    ok = n >= 200 and p < 0.01 and gap is not None and gap >= 0.2
    return {"n": n, "lag1_autocorr": round(obs, 3), "perm_p_value": round(p, 4),
            "r_after_win_minus_after_loss": None if gap is None else round(gap, 3),
            "verdict": "candidate hot hand: replicate on the next 100 trades before any multiplier" if ok
            else "no usable streak effect: keep sizing independent of the last result"}
