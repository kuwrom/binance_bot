"""Honest sample sizes for research on scan outcomes (#27). Scans repeat every ~30 min and each candidate carries an
overlapping 24h outcome, so rows are not independent draws:

- episodes: one observation per symbol-episode. An episode opens at a symbol's first appearance and absorbs every
  appearance within the outcome horizon (their outcome windows overlap); the next appearance after that opens a new
  one. Non-overlapping observations: unbiased with valid tests, at some loss of efficiency (Harri & Brorsen 2009).
- scan_excess: outcome minus the median of the same scan, a time fixed effect that absorbs the shared market move.
- clustered_mean / clustered_wilson: intervals clustered by UTC day, because candidates of one day share the market.
  CV3 (delete-one-day jackknife) variance with t(G-1) critical values (Cameron & Miller 2015 §VI.B/D: near-nominal size
  at G = 6-50 in their Table 2; MacKinnon, Nielsen & Webb 2023 §9). Proportions use Wilson with the df-adjusted
  effective n (Dean & Pagano 2015; Korn & Graubard 1998). n_eff is Kish's: the iid sample with the same precision.
  Truncation: a clustered variance below the iid variance of the observations (chance, or days that happen to
  agree) is raised to it (design effect >= 1, so n_eff <= n), and t(G-1) is kept even then: the more conservative of
  Dean & Pagano's two variants, because with few days a small between-day spread is no evidence of independence.
- Below MIN_DAYS days the intervals are descriptive only ("few" clusters: < 20-50, Cameron & Miller §VI.A).
- t_sf / t_ppf: Student's t tail and quantile (regularized incomplete beta) for the registry's forward test, whose
  Bonferroni hurdle (alpha / M) needs quantiles far beyond t975 (registry_forward.py).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Hashable, Sequence
from datetime import UTC, datetime
from statistics import NormalDist
from typing import Any

HOUR_MS = 3_600_000
MIN_DAYS = 20          # fewer days than this: intervals are descriptive only
MIN_SCAN_SYMBOLS = 5   # a same-scan median needs at least this many symbols
Z975 = NormalDist().inv_cdf(0.975)
T975 = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
        2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042)


def t975(df: int) -> float:
    """97.5% quantile of Student's t: table to df 30, Cornish-Fisher expansion beyond (error < 1e-4)."""
    if df <= len(T975):
        return T975[max(df, 1) - 1]
    z = Z975
    return z + (z ** 3 + z) / (4 * df) + (5 * z ** 5 + 16 * z ** 3 + 3 * z) / (96 * df ** 2)


def day_of(ts_ms: int | None) -> str | None:
    return None if ts_ms is None else datetime.fromtimestamp(ts_ms / 1000, UTC).strftime("%Y-%m-%d")


def days_of(rows: Sequence[dict[str, Any]], ts: str = "ts_ms") -> list[Hashable]:
    """UTC day of each row; a row without a timestamp is its own cluster (treated as independent)."""
    return [day_of(r[ts]) if r.get(ts) is not None else ("row", i) for i, r in enumerate(rows)]


def symbol_key(r: dict[str, Any]) -> Hashable | None:
    return r.get("symbol")


def episodes(rows: Sequence[dict[str, Any]], horizon_h: float | Callable[[dict[str, Any]], float] = 24.0,
             key: Callable[[dict[str, Any]], Hashable | None] = symbol_key, ts: str = "ts_ms") -> list[dict[str, Any]]:
    """The first row of every episode, in time order. horizon_h: hours, or a function of the episode's first row
    (e.g. a prediction's own horizon). Rows without a key or a timestamp are always kept: nothing shows they
    overlap (days_of likewise makes each its own cluster)."""
    out: list[dict[str, Any]] = []
    open_until: dict[Hashable, float] = {}
    for r in sorted(rows, key=lambda x: x.get(ts) or 0):
        k, t = key(r), r.get(ts)
        if k is not None and t is not None:
            if k in open_until and t < open_until[k]:
                continue
            h = horizon_h(r) if callable(horizon_h) else horizon_h
            open_until[k] = t + h * HOUR_MS
        out.append(r)
    return out


def scan_excess(rows: Sequence[dict[str, Any]], field: str = "short_ret_24h", scan: str = "ts_ms",
                min_symbols: int = MIN_SCAN_SYMBOLS) -> list[float | None]:
    """Per row: field minus the median of its scan (one value per distinct symbol). None when the scan has fewer
    than min_symbols symbols with a value, or the row has none."""
    by_scan: dict[Any, dict[Hashable, float]] = {}
    for r in rows:
        if r.get(field) is not None:
            by_scan.setdefault(r.get(scan), {}).setdefault(r.get("symbol"), r[field])
    med = {s: statistics.median(v.values()) for s, v in by_scan.items() if len(v) >= min_symbols}
    return [None if r.get(field) is None or r.get(scan) not in med else r[field] - med[r.get(scan)] for r in rows]


def _cv3(values: Sequence[float], clusters: Sequence[Hashable]) -> tuple[float, int]:
    """(CV3 variance of the mean, number of clusters): (G-1)/G * sum over clusters of (leave-one-out mean - mean)^2."""
    sums: dict[Hashable, list[float]] = {}
    for v, c in zip(values, clusters, strict=True):
        s = sums.setdefault(c, [0.0, 0.0])
        s[0] += v
        s[1] += 1
    n, tot, g = len(values), math.fsum(values), len(sums)
    if g < 2:
        return math.nan, g
    m = tot / n
    return (g - 1) / g * math.fsum(((tot - s) / (n - k) - m) ** 2 for s, k in sums.values()), g


def clustered_mean(values: Sequence[float], clusters: Sequence[Hashable]) -> dict[str, Any]:
    """Mean with a 95% CI clustered by `clusters` (usually days): CV3 floored at the iid variance, t(G-1).
    n_eff = s^2 / V (<= n). ci95 and n_eff are None with fewer than 2 clusters."""
    n = len(values)
    if not n:
        return {"mean": None, "n": 0, "days": 0, "n_eff": None, "ci95": None, "se": None}
    m = math.fsum(values) / n
    v3, g = _cv3(values, clusters)
    out: dict[str, Any] = {"mean": m, "n": n, "days": g, "n_eff": None, "ci95": None, "se": None}
    if g < 2:
        return out
    s2 = statistics.variance(values)
    v = max(v3, s2 / n)
    half = t975(g - 1) * math.sqrt(v)
    out.update(ci95=(m - half, m + half), n_eff=s2 / v if v > 0 else float(n), se=math.sqrt(v))
    return out


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction of the incomplete beta function (modified Lentz; Press et al., Numerical Recipes §6.4)."""
    tiny = 1e-300
    c, d = 1.0, 1.0 - (a + b) * x / (a + 1)
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):
        for aa in (m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m)),
                   -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))):
            d = 1.0 + aa * d
            d = 1.0 / (d if abs(d) > tiny else tiny)
            c = 1.0 + aa / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1.0) < 1e-15:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0 or x >= 1:
        return 0.0 if x <= 0 else 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1 - x) / b


def t_sf(t: float, df: float) -> float:
    """P(T > t) for Student's t with df degrees of freedom (one-sided p of a mean > 0)."""
    tail = 0.5 * betainc(df / 2, 0.5, df / (df + t * t))
    return tail if t >= 0 else 1.0 - tail


def t_ppf(q: float, df: float) -> float:
    """q-quantile of Student's t (bisection on t_sf), e.g. t_ppf(1 - alpha / M, G - 1) for a Bonferroni hurdle."""
    if q < 0.5:
        return -t_ppf(1 - q, df)
    lo, hi = 0.0, 1.0
    while t_sf(hi, df) > 1 - q:
        lo, hi = hi, hi * 2
    for _ in range(100):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if t_sf(mid, df) > 1 - q else (lo, mid)
    return (lo + hi) / 2


def wilson(k: float, n: float, z: float = Z975) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0.0, c - h), 4), round(min(1.0, c + h), 4))


def clustered_wilson(hits: Sequence[float], clusters: Sequence[Hashable]) -> dict[str, Any]:   # hits: 0/1, or bools
    """Share of hits with a Wilson 95% CI on the df-adjusted effective n: n* = n_eff (z / t_{G-1})^2 with n_eff
    = p(1-p) / V truncated at n (Dean & Pagano 2015 §2-3). ci95 is None with fewer than 2 clusters."""
    ys = [1.0 if h else 0.0 for h in hits]
    n = len(ys)
    if not n:
        return {"p": None, "n": 0, "days": 0, "n_eff": None, "ci95": None}
    p = math.fsum(ys) / n
    v3, g = _cv3(ys, clusters)
    out: dict[str, Any] = {"p": p, "n": n, "days": g, "n_eff": None, "ci95": None}
    if g < 2:
        return out
    n_eff = min(float(n), p * (1 - p) / v3) if v3 > 0 and 0 < p < 1 else float(n)
    n_star = n_eff * (Z975 / t975(g - 1)) ** 2
    out.update(n_eff=n_eff, ci95=wilson(p * n_star, n_star))
    return out


def inference_note(days: int) -> str:
    base = "95% intervals clustered by UTC day: CV3 jackknife, t(days-1) (Cameron & Miller 2015)"
    if days < MIN_DAYS:
        return f"descriptive only: {days} day(s) < {MIN_DAYS}. {base}"
    return f"{base}; any bucket with < {MIN_DAYS} days is descriptive only"
