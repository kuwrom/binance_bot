"""Closed-form first-passage probabilities for Brownian motion in log-price (docs/PROBABILITY.md).

X_t = ln(P_t/P_0) = νt + σW_t. σ and T share one time unit (here: hours). Default ν = 0: at 15m-3d horizons drift
is noise, and with ν = 0 these numbers are the NO-EDGE BASELINE (R:R geometry only). Continuous monitoring,
constant σ, no jumps: fat tails and vol clustering are handled by montecarlo.py.
"""

from __future__ import annotations

import math


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def touch_up(p0: float, level: float, sigma: float, t: float, nu: float = 0.0) -> float:
    """P(max price over [0, t] >= level), level > p0 (reflection principle with drift; Shreve SCII §7.2)."""
    if level <= p0:
        return 1.0
    if sigma <= 0 or t <= 0:
        return 0.0
    u, s = math.log(level / p0), sigma * math.sqrt(t)
    return min(1.0, phi((-u + nu * t) / s) + math.exp(2 * nu * u / sigma**2) * phi((-u - nu * t) / s))


def touch_down(p0: float, level: float, sigma: float, t: float, nu: float = 0.0) -> float:
    """P(min price over [0, t] <= level), level < p0 (mirror of touch_up)."""
    if level >= p0:
        return 1.0
    return touch_up(1.0, p0 / level, sigma, t, -nu)


def race_up_first(p0: float, upper: float, lower: float, sigma: float, nu: float = 0.0) -> float:
    """P(hit upper before lower) with no time limit (scale function / gambler's ruin).
    ν = 0: ln(P0/L) / ln(U/L). This is the optional-stopping result: no drift ⇒ no edge from R:R."""
    u, lo = math.log(upper / p0), math.log(lower / p0)
    if abs(nu) < 1e-12 or sigma <= 0:
        return -lo / (u - lo)
    k = 2 * nu / sigma**2
    return (1 - math.exp(-k * lo)) / (math.exp(-k * u) - math.exp(-k * lo))


def three_way(p0: float, upper: float, lower: float, sigma: float, t: float, nu: float = 0.0,
              terms: int = 300) -> dict[str, float]:
    """P(upper first), P(lower first), P(neither by t): eigen-expansion of the killed density (Cox & Miller 1965).
    Verified vs Monte Carlo: σ=100%/yr, T=4h, +3%/-2% -> (0.1655, 0.3442, 0.4902)."""
    if sigma <= 0 or t <= 0:
        return {"up": 0.0, "down": 0.0, "time": 1.0}
    u, lo = math.log(upper / p0), math.log(lower / p0)
    w, y = u - lo, -lo
    s2 = sigma**2
    p_up_inf = race_up_first(p0, upper, lower, sigma, nu)
    c = math.pi * s2 / w**2
    up_sum = dn_sum = 0.0
    for n in range(1, terms + 1):
        kappa = n**2 * math.pi**2 * s2 / (2 * w**2) + nu**2 / (2 * s2)
        decay = math.exp(-kappa * t) / kappa
        sn = math.sin(n * math.pi * y / w)
        up_sum += n * (-1) ** (n + 1) * sn * decay
        dn_sum += n * sn * decay
    p_up = p_up_inf - c * math.exp(nu * (w - y) / s2) * up_sum
    p_dn = (1 - p_up_inf) - c * math.exp(-nu * y / s2) * dn_sum
    p_up, p_dn = max(0.0, min(1.0, p_up)), max(0.0, min(1.0, p_dn))
    return {"up": p_up, "down": p_dn, "time": max(0.0, 1 - p_up - p_dn)}


def bgk_shift(level: float, p0: float, sigma: float, dt: float) -> float:
    """Broadie–Glasserman–Kou: move a barrier AWAY from price so continuous formulas match checks made only at
    discrete times dt (e.g. candle closes). β = −ζ(½)/√(2π) ≈ 0.5826."""
    beta = 0.5826 * sigma * math.sqrt(dt)
    return level * math.exp(beta) if level > p0 else level * math.exp(-beta)


def short_geometry(entry: float, stop: float, target: float, sigma: float, t: float) -> dict[str, float]:
    """No-edge baseline for a SHORT: stop is the upper barrier, target the lower. "No edge" = the PRICE is a
    martingale (log drift ν = −σ²/2; a zero LOG drift would bias price upward by Jensen). Returns P(target first),
    P(stop first), P(time) and the no-edge EV in R (≈ 0 by construction, before costs)."""
    nu = -(sigma**2) / 2
    tw = three_way(entry, stop, target, sigma, t, nu)
    r = (stop - entry) / entry
    reward_r = (entry - target) / entry / r if r > 0 else 0.0
    ev_r = tw["down"] * reward_r - tw["up"] * 1.0
    return {"p_target": tw["down"], "p_stop": tw["up"], "p_time": tw["time"], "reward_R": reward_r,
            "ev_R_no_edge": ev_r, "p_target_no_time_limit": 1 - race_up_first(entry, stop, target, sigma, nu)}
