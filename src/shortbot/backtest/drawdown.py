"""Drawdown quantiles for a setup that really has the edge we require (proposal P2, docs/PROPOSALS.md; task #23).

A stop-out rule is a statistical test: "would a setup with true mean mu R per trade plausibly have drawn down this
far?" Following Bailey & López de Prado (Journal of Risk 2015, "Stop-outs under serial correlation and the triple
penance rule"):
- Their closed form, the peak of the alpha-quantile PATH for iid Gaussian trades: (z_alpha * sigma)^2 / (4 * mu),
  reached after t* = (z_alpha * sigma / (2 * mu))^2 trades.
- The exact alpha-quantile of the deepest loss from one peak, for Brownian motion with drift mu > 0:
  P(loss ever reaches x) = exp(-2 mu x / sigma^2), so x_alpha = sigma^2 ln(1 / (1 - alpha)) / (2 mu). This is
  more than double the path shortcut at alpha = 0.95, so the shortcut understates single-episode risk.
- A live rule watches EVERY episode over a setup's life, so its false-stop rate is set by the max drawdown over a
  horizon of N trades. We block-bootstrap the actual per-trade R residuals (r - mean) + mu, which keeps the fat
  tails and serial correlation.
A threshold at a quantile q of that distribution stops a setup that truly has edge mu with probability 1 - q.

    .venv/bin/python -m shortbot.backtest.drawdown TRADES.json [TRADES2.json ...]
"""

from __future__ import annotations

import json
import math
import random
import statistics
import sys
from statistics import NormalDist
from typing import Any

MUS = (0.05, 0.10, 0.20)
HORIZONS = (50, 100, 250)
QUANTILES = (0.80, 0.90, 0.95, 0.99)


def maxdd_gaussian(mu: float, sigma: float, alpha: float) -> dict[str, float]:
    """Bailey & López de Prado's quantile-path shortcut (see the module doc)."""
    z = NormalDist().inv_cdf(alpha)
    return {"maxdd_r": round((z * sigma) ** 2 / (4 * mu), 2), "at_trade": round((z * sigma / (2 * mu)) ** 2, 1)}


def first_passage_quantile(mu: float, sigma: float, alpha: float) -> float:
    """Exact alpha-quantile of the deepest loss from one peak for Brownian motion with drift mu > 0."""
    return round(sigma ** 2 * math.log(1 / (1 - alpha)) / (2 * mu), 2)


def max_drawdown(xs: list[float]) -> float:
    peak = cum = dd = 0.0
    for x in xs:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def bootstrap_maxdd(resid: list[float], mu: float, n: int, paths: int = 4000, block: int = 5,
                    seed: int = 7) -> dict[float, float]:
    """MaxDD quantiles (R) over n trades: blocks of `block` consecutive residuals (keeps serial correlation)
    shifted to mean mu."""
    rng, k = random.Random(seed), len(resid)
    out = []
    for _ in range(paths):
        xs: list[float] = []
        while len(xs) < n:
            s = rng.randrange(0, max(1, k - block))
            xs.extend(r + mu for r in resid[s:s + block])
        out.append(max_drawdown(xs[:n]))
    out.sort()
    return {q: round(out[min(len(out) - 1, int(q * len(out)))], 2) for q in QUANTILES}


def lag1(xs: list[float]) -> float | None:
    if len(xs) < 10:
        return None
    m = statistics.fmean(xs)
    num = sum((a - m) * (b - m) for a, b in zip(xs, xs[1:]))
    den = sum((a - m) ** 2 for a in xs)
    return round(num / den, 3) if den else None


def profile(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-trade R stats in entry order, and the drawdown quantile tables for the required edges."""
    rs = [t["r"] for t in sorted(trades, key=lambda t: (t.get("entry_ts") or 0, t.get("symbol", "")))]
    if len(rs) < 30:
        return {"n": len(rs)}
    mean, sd = statistics.fmean(rs), statistics.stdev(rs)
    resid = [r - mean for r in rs]
    out: dict[str, Any] = {"n": len(rs), "mean_r": round(mean, 3), "sd_r": round(sd, 3), "lag1": lag1(rs),
                           "p99_loss_r": round(sorted(rs)[int(0.01 * len(rs))], 2)}
    for mu in MUS:
        out[f"mu={mu}"] = {"bldp_path": {q: maxdd_gaussian(mu, sd, q)["maxdd_r"] for q in QUANTILES},
                           "exact_one_peak": {q: first_passage_quantile(mu, sd, q) for q in QUANTILES},
                           **{f"boot_n{n}": bootstrap_maxdd(resid, mu, n) for n in HORIZONS}}
    return out


def main(argv: list[str] | None = None) -> None:
    paths = list(sys.argv[1:] if argv is None else argv)
    trades = [t for p in paths for t in json.load(open(p))]
    by: dict[str, list[dict]] = {}
    for t in trades:
        by.setdefault(t.get("strategy", "?"), []).append(t)
    res = {"pooled": profile(trades), **{s: profile(v) for s, v in sorted(by.items())}}
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
