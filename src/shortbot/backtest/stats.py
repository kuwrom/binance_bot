"""Statistics for the backtest report: trade summaries in R, cluster-robust t (by day / coin / both), drawdown, and
the Deflated Sharpe Ratio (Bailey & López de Prado 2014) over the whole trial ledger.

t (#31): every t is the t of the reported pooled mean (avg R), clustered by day, by coin and two-way (cluster_se.py:
CV3 one-way, Cameron-Gelbach-Miller 2011 two-way, floored at the larger one-way). Before #31 it was a t over
equal-weighted cluster means, a different number than the avg R it stood next to ("pre-#31 estimator").

DSR = PSR(SR*) with SR* = sqrt(V[SR_trials]) * ((1 - g) * Z(1 - 1/N) + g * Z(1 - 1/(N e))), g = Euler-Mascheroni,
PSR(SR*) = Phi((SR - SR*) sqrt(T - 1) / sqrt(1 - skew SR + (kurt - 1)/4 SR^2)). SR is per trade (not annualised).
T = the trades' effective number (n_eff = s^2 / V_2way, Kish), not the trade count: PSR assumes IID or
stationary-ergodic observations (Bailey & López de Prado 2012 §1-2), and trades that share a day are neither.
N = independent trials (trials_eff.py; the trial register, trial_register.py; placebos excluded: they are controls)."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from datetime import UTC, datetime
from statistics import NormalDist
from typing import Any

from .. import cluster_se as C

EULER = 0.5772156649015329
ND = NormalDist()
SQUEEZE_OVERRUN_R = 2.0   # stopped out by a bar that ran >= 2R past entry: a squeeze that would run any sane stop


def day_of(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, UTC).strftime("%Y-%m-%d")


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


def psr(sr: float, t: float, skew: float, kurt: float, sr0: float) -> float | None:
    den = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if t < 3 or den <= 0:
        return None
    return ND.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(den))


def expected_max_sr(n_trials: int, var_sr: float) -> float:
    if n_trials < 2 or var_sr <= 0:
        return 0.0
    return math.sqrt(var_sr) * ((1 - EULER) * ND.inv_cdf(1 - 1 / n_trials)
                                + EULER * ND.inv_cdf(1 - 1 / (n_trials * math.e)))


def dsr(xs: list[float], n_trials: int, var_sr: float, t_eff: float | None = None) -> dict[str, Any]:
    """t_eff: the sample length T (default len(xs), the pre-#31 iid count; trade_dsr passes the clustered n_eff)."""
    sr = sharpe(xs)
    t = float(len(xs)) if t_eff is None else t_eff
    if sr is None:
        return {"sr": None, "sr0": None, "psr0": None, "dsr": None, "t": t}
    sk, ku = moments(xs)
    sr0 = expected_max_sr(n_trials, var_sr)
    return {"sr": round(sr, 3), "sr0": round(sr0, 3), "psr0": _r(psr(sr, t, sk, ku, 0.0), 3),
            "dsr": _r(psr(sr, t, sk, ku, sr0), 3), "t": t}


def n_eff(trades: list[dict]) -> float:
    """Effective number of independent trades: s^2 / V_2way (day x coin), <= n (cluster_se.mean_inference)."""
    inf = C.mean_inference([t["r"] for t in trades], [t["day"] for t in trades],
                           [t.get("symbol") for t in trades])
    return round(inf["n_eff"], 1) if inf["n_eff"] is not None else 0.0


def trade_dsr(trades: list[dict], n_trials: int, var_sr: float) -> dict[str, Any]:
    """DSR of a book of trades with T = its effective number of independent trades."""
    return dsr([t["r"] for t in trades], n_trials, var_sr, t_eff=n_eff(trades))


def _r(x: float | None, n: int = 3) -> float | None:
    return None if x is None else round(x, n)


def cluster_t(trades: list[dict], key: str) -> tuple[float | None, int]:
    """(t of the pooled mean R clustered by `key` (day or coin), number of clusters): CV3 floored at iid, df G - 1
    (cluster_se.one_way_t). Fewer than 3 clusters: None."""
    t, g = C.one_way_t([x["r"] for x in trades], [x[key] for x in trades])
    return (_r(t, 2) if g >= 3 else None), g


def clustered(trades: list[dict], key: str = "r") -> dict[str, Any]:
    """t_day, t_coin, t_2way (day x coin), se_2way, n_eff of the pooled mean of `key` (cluster_se.mean_inference)."""
    inf = C.mean_inference([t[key] for t in trades], [t["day"] for t in trades], [t.get("symbol") for t in trades])
    ok = lambda k, g: _r(inf[k], 2) if inf[g] >= 3 else None  # noqa: E731
    return {"t_day": ok("t_day", "days"), "t_coin": ok("t_coin", "coins"),
            "t_2way": ok("t_2way", "days") if inf["coins"] >= 3 else None, "se_2way": _r(inf["se_2way"], 4),
            "n_eff": _r(inf["n_eff"], 1), "df_2way": inf["df_2way"]}


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
    cl = clustered(trades)
    return {
        "n": len(rs), "coins": len({t["symbol"] for t in trades}), "days": len({t["day"] for t in trades}),
        "win_pct": round(sum(r > 0 for r in rs) / len(rs) * 100, 1),
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
        **cl,
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
                          "entry_ts": t["entry_ts"], "symbol": t.get("symbol")})
    if len(diffs) < 3:
        return {"n": len(diffs)}
    t_day, n_days = cluster_t(diffs, "day")
    t_g, _ = cluster_t([{"r": x["g"], "day": x["day"]} for x in diffs], "day")
    t_2way = clustered(diffs)["t_2way"]
    half = lambda a, k="r": [x[k] for x in diffs if (x["entry_ts"] < split_ts) == a]  # noqa: E731
    mean = lambda xs: _r(statistics.fmean(xs), 3) if xs else None  # noqa: E731
    return {"n": len(diffs), "edge": round(statistics.fmean(x["r"] for x in diffs), 3), "t_day": t_day,
            "t_2way": t_2way,
            "days": n_days, "is": mean(half(True)), "oos": mean(half(False)),
            "edge_gross": round(statistics.fmean(x["g"] for x in diffs), 3), "t_day_gross": t_g,
            "is_gross": mean(half(True, "g")), "oos_gross": mean(half(False, "g"))}
