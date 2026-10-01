"""Statistics for the backtest report: trade summaries in R, cluster-robust t (by day / coin), drawdown, and the
Deflated Sharpe Ratio (Bailey & López de Prado 2014) over the whole trial ledger.

DSR = PSR(SR*) with SR* = sqrt(V[SR_trials]) * ((1 - g) * Z(1 - 1/N) + g * Z(1 - 1/(N e))), g = Euler-Mascheroni,
PSR(SR*) = Phi((SR - SR*) sqrt(T - 1) / sqrt(1 - skew SR + (kurt - 1)/4 SR^2)). SR is per trade (not annualised),
T = number of trades, N = every param set tried (placebos excluded: they are controls, not candidates)."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from datetime import datetime, timezone
from statistics import NormalDist
from typing import Any

EULER = 0.5772156649015329
ND = NormalDist()
SQUEEZE_OVERRUN_R = 2.0   # stopped out by a bar that ran >= 2R past entry: a squeeze that would run any sane stop


def day_of(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")


def sharpe(xs: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    sd = statistics.stdev(xs)
    return statistics.fmean(xs) / sd if sd else None


def moments(xs: list[float]) -> tuple[float, float]:
    """(skew, kurtosis) - kurtosis NOT excess (normal = 3)."""
    n, mu = len(xs), statistics.fmean(xs)
    sd = statistics.pstdev(xs)
    if n < 4 or not sd:
        return 0.0, 3.0
    return (sum((x - mu) ** 3 for x in xs) / n / sd ** 3, sum((x - mu) ** 4 for x in xs) / n / sd ** 4)


def psr(sr: float, t: int, skew: float, kurt: float, sr0: float) -> float | None:
    den = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if t < 3 or den <= 0:
        return None
    return ND.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(den))


def expected_max_sr(n_trials: int, var_sr: float) -> float:
    if n_trials < 2 or var_sr <= 0:
        return 0.0
    return math.sqrt(var_sr) * ((1 - EULER) * ND.inv_cdf(1 - 1 / n_trials)
                                + EULER * ND.inv_cdf(1 - 1 / (n_trials * math.e)))


def dsr(xs: list[float], n_trials: int, var_sr: float) -> dict[str, Any]:
    sr = sharpe(xs)
    if sr is None:
        return {"sr": None, "sr0": None, "psr0": None, "dsr": None}
    sk, ku = moments(xs)
    sr0 = expected_max_sr(n_trials, var_sr)
    return {"sr": round(sr, 3), "sr0": round(sr0, 3), "psr0": _r(psr(sr, len(xs), sk, ku, 0.0), 3),
            "dsr": _r(psr(sr, len(xs), sk, ku, sr0), 3)}


def _r(x: float | None, n: int = 3) -> float | None:
    return None if x is None else round(x, n)


def cluster_t(trades: list[dict], key: str) -> tuple[float | None, int]:
    """t of the mean R when trades are first averaged within clusters (day or coin): events that share a day or
    a coin are not independent, so the effective N is the number of clusters."""
    g: dict[str, list[float]] = {}
    for t in trades:
        g.setdefault(t[key], []).append(t["r"])
    means = [statistics.fmean(v) for v in g.values()]
    if len(means) < 3:
        return None, len(means)
    sd = statistics.stdev(means)
    return (round(statistics.fmean(means) / (sd / math.sqrt(len(means))), 2) if sd else None), len(means)


def max_dd_r(trades: list[dict]) -> float:
    """Worst peak-to-trough of cumulative R, trades in exit order (1 R each, no overlap netting)."""
    s = peak = dd = 0.0
    for t in sorted(trades, key=lambda x: (x["exit_ts"], x["symbol"])):
        s += t["r"]
        peak = max(peak, s)
        dd = min(dd, s - peak)
    return round(dd, 2)


def welch_t(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3 or len(b) < 3:
        return None
    va, vb = statistics.variance(a) / len(a), statistics.variance(b) / len(b)
    return round((statistics.fmean(a) - statistics.fmean(b)) / math.sqrt(va + vb), 2) if va + vb else None


def summarize(trades: list[dict]) -> dict[str, Any]:
    if not trades:
        return {"n": 0}
    rs = [t["r"] for t in trades]
    wins, losses = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    t_day, n_day = cluster_t(trades, "day")
    t_coin, n_coin = cluster_t(trades, "symbol")
    return {
        "n": len(rs), "coins": n_coin, "days": n_day, "win_pct": round(sum(r > 0 for r in rs) / len(rs) * 100, 1),
        "avg_r": round(statistics.fmean(rs), 3), "median_r": round(statistics.median(rs), 3),
        "avg_r_gross": round(statistics.fmean(t["r_gross"] for t in trades), 3),
        "avg_pnl_pct": round(statistics.fmean(t["pnl_pct"] for t in trades), 3),
        "avg_cost_pct": round(statistics.fmean(t["fees_pct"] + t["slip_pct"] for t in trades), 3),
        "avg_funding_pct": round(statistics.fmean(t["funding_pct"] for t in trades), 4),
        "avg_risk_pct": round(statistics.fmean(t["risk_pct"] for t in trades), 2),
        "pf": round(wins / losses, 2) if losses else None, "max_dd_r": max_dd_r(trades),
        "squeeze_pct": round(sum(t.get("overrun_r", 0) >= SQUEEZE_OVERRUN_R for t in trades) / len(rs) * 100, 1),
        "mae_p90_pct": round(sorted(t["mae_pct"] for t in trades)[int(0.9 * (len(rs) - 1))], 2),
        "avg_bars": round(statistics.fmean(t["bars"] for t in trades), 1),
        "t_day": t_day, "t_coin": t_coin,
        "exits": dict(Counter(t["reason"] for t in trades).most_common()),
    }


def paired_edge(trades: list[dict], ctl: list[dict], split_ts: int) -> dict[str, Any]:
    """Each trade minus the mean of ITS OWN placebo entries (same hour / same coin), t clustered by day, and the
    edge in each half. This is the relative (beta-neutral-ish) edge; a short-only book only earns it when the
    absolute R is also > 0. The `*_gross` keys repeat it on R before fees, slippage and funding (r_gross): a donor
    that is more volatile than its placebo pays fewer R of the same costs, which inflates the NET edge only."""
    by: dict[tuple, list[dict]] = {}
    for c in ctl:
        d = (c.get("meta") or {}).get("donor")
        if d:
            by.setdefault(tuple(d), []).append(c)
    diffs = []
    for t in trades:
        ps = by.get((t["symbol"], t["signal_i"]))
        if ps:
            diffs.append({"r": t["r"] - statistics.fmean(p["r"] for p in ps), "day": t["day"],
                          "g": t.get("r_gross", t["r"]) - statistics.fmean(p.get("r_gross", p["r"]) for p in ps),
                          "entry_ts": t["entry_ts"]})
    if len(diffs) < 3:
        return {"n": len(diffs)}
    t_day, n_days = cluster_t(diffs, "day")
    t_g, _ = cluster_t([{"r": x["g"], "day": x["day"]} for x in diffs], "day")
    half = lambda a, k="r": [x[k] for x in diffs if (x["entry_ts"] < split_ts) == a]  # noqa: E731
    mean = lambda xs: _r(statistics.fmean(xs), 3) if xs else None  # noqa: E731
    return {"n": len(diffs), "edge": round(statistics.fmean(x["r"] for x in diffs), 3), "t_day": t_day,
            "days": n_days, "is": mean(half(True)), "oos": mean(half(False)),
            "edge_gross": round(statistics.fmean(x["g"] for x in diffs), 3), "t_day_gross": t_g,
            "is_gross": mean(half(True, "g")), "oos_gross": mean(half(False, "g"))}
