"""Cluster-robust inference on a POOLED mean (#31): the t of the number we report (avg R), not of equal-weighted cluster
means. Trades on one day share the market; trades on one coin share the coin.

- One-way (day, coin): research_stats' CV3 jackknife variance, floored at the iid variance, t(G-1) (Cameron & Miller
  2015 §VI; the same estimator the registry's forward test uses).
- Two-way (day x coin): Cameron, Gelbach & Miller (2011, JBES; NBER t0327 §2.2 eq. 2.14-2.15)
  V = V_day + V_coin - V_day∩coin, each a one-way cluster-robust variance of the mean with the Stata-style factor
  c = G/(G-1) (N-1)/(N-K) of its own G (day count, coin count, count of non-empty day-coin cells; K = 1). Critical
  values t(min(G, H) - 1) (CGM §3.1). CGM p.9: if V comes out negative, fall back on the larger one-way variance;
  we apply that floor always (V_2way >= max(V_day, V_coin, V_iid)), so t_2way is never above either one-way t.
- n_eff = s^2 / V_2way (Kish design effect, <= n): the iid sample with the same precision. The Deflated Sharpe uses
  it as the sample length T (Bailey & López de Prado 2012/2014 assume IID or stationary-ergodic returns; trades that
  share a day are neither).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Hashable, Sequence
from typing import Any

from . import research_stats as rs


def _cr1(u: Sequence[float], clusters: Sequence[Hashable], n: int) -> tuple[float, int]:
    """(G/(G-1) * sum over clusters of (sum of residuals)^2 / n^2, G). nan with fewer than 2 clusters."""
    sums: dict[Hashable, float] = {}
    for x, c in zip(u, clusters):
        sums[c] = sums.get(c, 0.0) + x
    g = len(sums)
    if g < 2:
        return math.nan, g
    return g / (g - 1) * math.fsum(s * s for s in sums.values()) / n ** 2, g


def two_way_cr1(values: Sequence[float], g: Sequence[Hashable], h: Sequence[Hashable]) -> dict[str, Any]:
    """CGM (2011) eq. 2.15 for a regression on a constant, unfloored: v may be negative (or nan, < 2 clusters)."""
    n = len(values)
    m = math.fsum(values) / n
    u = [x - m for x in values]
    vg, ng = _cr1(u, g, n)
    vh, nh = _cr1(u, h, n)
    vgh, ngh = _cr1(u, list(zip(g, h)), n)
    if ngh < 2:   # one cell: the intersection term has no estimate; nothing to subtract
        vgh = 0.0
    return {"v": vg + vh - vgh, "v_g": vg, "v_h": vh, "v_gh": vgh, "g": ng, "h": nh, "i": ngh}


def _t(m: float, v: float) -> float | None:
    return m / math.sqrt(v) if v > 0 and math.isfinite(v) else None


def one_way_t(values: Sequence[float], clusters: Sequence[Hashable]) -> tuple[float | None, int]:
    """(t of the pooled mean, CV3 by `clusters` floored at iid; number of clusters). None below 2 clusters."""
    n = len(values)
    v3, g = rs._cv3(values, clusters) if n else (math.nan, 0)
    if g < 2:
        return None, g
    m = math.fsum(values) / n
    return _t(m, max(v3, statistics.variance(values) / n)), g


def mean_inference(values: Sequence[float], days: Sequence[Hashable], coins: Sequence[Hashable]) -> dict[str, Any]:
    """Pooled mean with iid, day-, coin- and two-way-clustered standard errors and t's (None when undefined: fewer
    than 2 clusters, or no variance). df_* are the t degrees of freedom; fallback = the two-way floor was binding."""
    n = len(values)
    out: dict[str, Any] = {"n": n, "mean": None, "days": 0, "coins": 0, "n_eff": None, "fallback": False}
    for k in ("iid", "day", "coin", "2way"):
        out[f"se_{k}"] = out[f"t_{k}"] = out[f"df_{k}"] = None
    if not n:
        return out
    m = math.fsum(values) / n
    s2 = statistics.variance(values) if n > 1 else 0.0
    v_iid = s2 / n
    out.update(mean=m, se_iid=math.sqrt(v_iid), t_iid=_t(m, v_iid), df_iid=n - 1, n_eff=float(n))
    v: dict[str, float] = {}
    for k, cl in (("day", days), ("coin", coins)):
        v3, g = rs._cv3(values, cl)
        out["days" if k == "day" else "coins"] = g
        if g >= 2:
            v[k] = max(v3, v_iid)
            out.update({f"se_{k}": math.sqrt(v[k]), f"t_{k}": _t(m, v[k]), f"df_{k}": g - 1})
    if "day" in v and "coin" in v:
        raw = two_way_cr1(values, days, coins)["v"]
        floor = max(v["day"], v["coin"])
        v2 = max(raw, floor) if math.isfinite(raw) else floor
        out.update(se_2way=math.sqrt(v2), t_2way=_t(m, v2), df_2way=min(out["days"], out["coins"]) - 1,
                   fallback=not math.isfinite(raw) or raw < floor, n_eff=min(float(n), s2 / v2) if v2 > 0 else float(n))
    return out
