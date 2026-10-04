"""Signal age decay and "hotness" (docs/SOCIAL_AND_ROBUST_PRICES.md §2).

weight(family, age_h) = exp(−age·ln2/HL): a signal's points shrink with the age of the event behind it.
heat(series): is something hot right now? velocity z vs its own baseline, acceleration (fast − slow EWMA
change) and the fast/slow ratio (< 1 = cooling: the 'fade' short setups want).
Half-lives are PRIORS; `calibrate_half_life` refits them from labelled outcomes (IC vs signal age)."""

from __future__ import annotations

import math
import statistics

HALF_LIFE_H: dict[str, float] = {        # priors (research table); calibrate from our own labels
    "order_flow": 0.17, "sweep": 0.75, "liquidation": 0.75, "social": 4.0, "funding_oi": 16.0,
    "basis": 24.0, "pump": 36.0, "momentum": 24.0 * 14, "listing": 24.0 * 10,
}


def weight(family: str, age_h: float | None) -> float:
    """1.0 for a fresh signal, 0.5 at one half-life. Unknown age → 1 (don't punish missing timestamps)."""
    if age_h is None or age_h <= 0:
        return 1.0
    hl = HALF_LIFE_H.get(family)
    return math.exp(-age_h * math.log(2) / hl) if hl else 1.0


def decayed_points(points: int, family: str, age_h: float | None) -> int:
    return int(round(points * weight(family, age_h)))


def ewma(values: list[float], half_life: float) -> list[float]:
    if not values:
        return []
    a = 1 - 0.5 ** (1 / max(half_life, 1e-9))
    out, s = [], values[0]
    for v in values:
        s = a * v + (1 - a) * s
        out.append(s)
    return out


def heat(series: list[float], fast_hl: float, slow_hl: float, baseline: int | None = None) -> dict[str, float | None]:
    """series: evenly spaced counts/volumes (oldest first). Returns z of the latest value vs the baseline
    (median/MAD, robust to spikes), fast/slow EWMA ratio, and acceleration (change of fast − slow)."""
    if len(series) < 6:
        return {"z": None, "ratio": None, "accel": None, "hot": False, "cooling": False}
    base = series[-(baseline or len(series)):-1]
    med = statistics.median(base)
    mad = statistics.median([abs(x - med) for x in base]) * 1.4826 or (statistics.pstdev(base) or 1.0)
    z = (series[-1] - med) / mad
    f, s = ewma(series, fast_hl), ewma(series, slow_hl)
    ratio = f[-1] / s[-1] if s[-1] else None
    accel = (f[-1] - s[-1]) - (f[-2] - s[-2])
    peak = max(f[-12:])
    # cooling = past the peak: fast activity > 25% below its recent peak and still decelerating (a falling
    # fast/slow ratio; the ratio itself can stay > 1 for hours after a spike)
    cooling = accel < 0 and peak > 0 and f[-1] < 0.75 * peak
    return {"z": round(z, 2), "ratio": round(ratio, 3) if ratio is not None else None, "accel": accel,
            "hot": z > 2 and accel > 0, "cooling": cooling}


def calibrate_half_life(ages_h: list[float], ics: list[float], prior_h: float, k: float = 30.0) -> float | None:
    """Fit IC(age) = IC0·e^{−age/τ} by log-linear least squares on positive ICs, shrink toward the prior:
    HL = (n·HL_fit + k·HL_prior)/(n + k). Returns None if the fit is impossible."""
    pts = [(a, math.log(ic)) for a, ic in zip(ages_h, ics, strict=True) if ic > 0]
    if len(pts) < 3:
        return None
    ma = sum(a for a, _ in pts) / len(pts)
    ml = sum(v for _, v in pts) / len(pts)
    var = sum((a - ma) ** 2 for a, _ in pts)
    if not var:
        return None
    slope = sum((a - ma) * (v - ml) for a, v in pts) / var
    if slope >= 0:
        return prior_h
    hl_fit = math.log(2) / -slope
    n = len(pts)
    return (n * hl_fit + k * prior_h) / (n + k)
