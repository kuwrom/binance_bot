"""Score the shadow ladder variants (docs/LADDER.md): each variant's virtual exposure path, the real path and plain
hold-to-plan, all in R on the SAME trades (paired), net of resizing costs. A variant earns adoption only if its
paired edge over hold survives shrinkage and the trial ledger's multiple-testing penalty; 'placebo' (price-only)
must NOT beat 'base', or the score carries no information."""

from __future__ import annotations

import math
import statistics
from typing import Any

SIDE_COST = 0.001        # per side (fees + slippage), fraction of notional


def path_r(prices: list[float], xs: list[float], init_r: float, exit_price: float | None = None) -> float:
    """Short P&L in R of holding exposure xs[k] (fraction of the original size) from tick k to k+1, minus costs
    for every change in exposure. The last segment runs to exit_price when the real trade has closed."""
    if init_r <= 0 or len(prices) < 1:
        return 0.0
    pts = prices + ([exit_price] if exit_price else [])
    r = 0.0
    for k in range(len(pts) - 1):
        x = xs[min(k, len(xs) - 1)]
        r += x * (pts[k] - pts[k + 1]) / init_r
        if k > 0 and k < len(xs):
            r -= abs(xs[k] - xs[k - 1]) * SIDE_COST * pts[k] / init_r
    return r


def evaluate(journal) -> dict[str, Any]:
    from .ladder import seed_trials
    seed_trials(journal)
    rows = journal.db.execute("SELECT trade_id, variant, ts, price, x_actual, x_virtual, init_r FROM ladder_shadow"
                              " WHERE trade_id IS NOT NULL ORDER BY trade_id, id").fetchall()
    by: dict[int, dict[str, list]] = {}
    for tid, var, ts, price, xa, xv, ir in rows:
        d = by.setdefault(tid, {})
        d.setdefault(var or "base", []).append((ts, price, xa, xv, ir))
    per_trade: dict[int, dict[str, float]] = {}
    for tid, vs in by.items():
        t = journal.get_trade(tid) or {}
        exit_px = t.get("exit_price") if t.get("status") == "closed" else None
        base = vs.get("base") or next(iter(vs.values()))
        init_r = next((x[4] for x in base if x[4]), None) or ((t.get("initial_stop") or 0) - (t.get("entry_price") or 0))
        prices = [x[1] for x in base]
        res = {"real": path_r(prices, [x[2] for x in base], init_r, exit_px),
               "hold": path_r(prices, [base[0][2]] * len(base), init_r, exit_px)}
        for name, seq in vs.items():
            res[name] = path_r([x[1] for x in seq], [x[3] for x in seq], init_r, exit_px)
        per_trade[tid] = {k: round(v, 3) for k, v in res.items()}
    variants = sorted({k for r in per_trade.values() for k in r} - {"hold"})
    summary = {}
    for v in variants:
        d = [r[v] - r["hold"] for r in per_trade.values() if v in r]
        if not d:
            continue
        m = statistics.fmean(d)
        se = statistics.stdev(d) / math.sqrt(len(d)) if len(d) > 1 else None
        summary[v] = {"n_trades": len(d), "mean_delta_R_vs_hold": round(m, 3),
                      "se": round(se, 3) if se else None, "t": round(m / se, 2) if se else None}
    # ladder_live_v1: trades whose exposure was ACTUALLY managed by `ladder --apply`; 'hold' is the ghost of the
    # original plan (initial exposure held to the real exit), 'real' is what the live ladder did
    live_ids = [tid for tid in per_trade if ((journal.get_trade(tid) or {}).get("plan") or {}).get("ladder_live")]
    ld = [per_trade[t]["real"] - per_trade[t]["hold"] for t in live_ids]
    live = {"n_trades": len(ld), "mean_delta_R_live_vs_ghost": round(statistics.fmean(ld), 3) if ld else None,
            "trades": live_ids}
    from . import trial_register as TR       # the register (#31) counts every ladder variant ever evaluated
    ledger = max(TR.count("ladder"), journal.db.execute("SELECT COUNT(*) FROM trial_ledger").fetchone()[0])
    return {"per_trade_R": per_trade, "vs_hold": summary, "ladder_live_v1": live, "trials_in_ledger": ledger,
            "rule": "adopt only with ~200 paired trades, shrunk delta > cost, deflated Sharpe vs hold > 0.95 counting "
                    "all ledger trials, AND placebo not beating base"}
