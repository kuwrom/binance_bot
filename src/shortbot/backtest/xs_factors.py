"""Cross-sectional factor portfolios on six years of daily perps (task #19; library cards
lib.ctrend_technical_aggregate and lib.carry_momo_breakout_blend). Research only: it never trades.

Fixed before the first run (every reported leg counts as a trial):

- CTREND (Fieberg et al., JFQA 2025), weekly:
  - rebalance at each Sunday close;
  - universe: the top 100 perps by 30-day mean quote volume, >= 60 prior bars, >= $5M/day, ex BTC-pegged stables;
  - the 21 xs_features indicators, rank-normalised within the week;
  - combination: ridge regression of next-week cross-sectionally demeaned returns on the features, pooled over the
    previous 52 completed weeks (>= 26 needed). Shrinkage lambda = 10% of trace(X'X)/k. This stands in for the
    paper's elastic net. Every prediction is out of sample;
  - long the top quintile of predicted return, short the bottom quintile, equal weight within each leg.
- Blend (Robot Wealth), daily:
  - universe: the top 30 by 30-day volume (same filters);
  - score = 0.5 carry + 0.2 momentum + 0.3 breakout, each a cross-sectional z-score:
    - carry = -(the day's funding), so high funding scores short;
    - momentum = 10-day return;
    - breakout = 9.5 - days since the 20-day high;
  - weights = score - mean, scaled to gross 1 (dollar-neutral);
  - no-trade buffer: a name only trades when its target moves > 0.02 from the held weight (so a target within
    0.02 of zero is never opened, and the book's gross can sit a little under 1).
- Both:
  - P&L includes funding (a long pays positive funding, a short receives it);
  - costs are 15 bp per unit of one-way turnover;
  - a coin that stops trading exits at its last close.
  - Reported: long leg, short leg (as a short) and the dollar-neutral book (0.5 long + 0.5 short for CTREND), gross
    and net, each with a simple t and annualised Sharpe, per year.

    .venv/bin/python -m shortbot.backtest.xs_factors [--record]
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from .. import provenance as P
from .carry_regime import DAY, STABLES, _exit_close, daily_funding
from .xs_features import FEATURES, by_day, rank_normalise

COST = 0.0015
MIN_AGE, MIN_QV = 60, 5e6


def _wd(d: int) -> int:
    return datetime.fromtimestamp(d / 1000, UTC).weekday()


class Panel:
    """Daily closes, volumes, funding and features for every coin, indexed by day."""

    def __init__(self, data: dict[str, Any]):
        self.bars = {s: {b[0]: b for b in rows} for s, rows in data["bars"].items() if s not in STABLES}
        self.first = {s: min(b) for s, b in self.bars.items()}
        self.last = {s: max(b) for s, b in self.bars.items()}
        self.end = max(self.last.values())
        self.fund = {s: daily_funding(r) for s, r in data["funding"].items()}
        self.feat = by_day({"bars": {s: data["bars"][s] for s in self.bars}})
        self.closes = {s: {d: (b[4],) for d, b in bb.items()} for s, bb in self.bars.items()}
        self.days = sorted({d for bb in self.bars.values() for d in bb})

    def universe(self, d: int, top: int) -> list[str]:
        vol = {}
        for s, bb in self.bars.items():
            if d not in bb or d - MIN_AGE * DAY < self.first[s]:
                continue
            v = [bb[x][5] for x in range(d - 29 * DAY, d + 1, DAY) if x in bb]
            m = sum(v) / len(v)
            if m >= MIN_QV:
                vol[s] = m
        return sorted(vol, key=vol.get, reverse=True)[:top]

    def ret(self, s: str, d: int, h: int) -> float | None:
        """Long price return close d -> close d+h (delivery-aware)."""
        x = _exit_close(self.closes[s], d, h, self.last[s], self.end)
        return None if x is None else x / self.bars[s][d][4] - 1

    def funding(self, s: str, d: int, h: int) -> float:
        """Funding paid by a LONG over (d, d+h] (a short receives it)."""
        f = self.fund.get(s, {})
        return sum(f.get(d + k * DAY, 0.0) for k in range(1, h + 1))


def ridge(x: np.ndarray, y: np.ndarray, shrink: float = 0.10) -> np.ndarray:
    xtx = x.T @ x
    lam = shrink * np.trace(xtx) / x.shape[1]
    return np.linalg.solve(xtx + lam * np.eye(x.shape[1]), x.T @ y)


def _leg(p: Panel, names: list[str], d: int, h: int, side: int) -> tuple[float | None, int]:
    """Equal-weight leg return (side +1 long, -1 short) with funding, gross of costs."""
    rs = []
    for s in names:
        r = p.ret(s, d, h)
        if r is not None:
            rs.append(side * r - side * p.funding(s, d, h))
    return (statistics.fmean(rs), len(rs)) if rs else (None, 0)


def _turnover(old: dict[str, float], new: dict[str, float]) -> float:
    return sum(abs(new.get(s, 0.0) - old.get(s, 0.0)) for s in set(old) | set(new))


def ctrend(p: Panel, top: int = 100, window: int = 52, min_weeks: int = 26) -> list[dict]:
    weeks = [d for d in p.days if _wd(d) == 6 and d + 7 * DAY <= p.end]
    hist: list[tuple[int, np.ndarray, np.ndarray]] = []       # (week, X, demeaned next-week return)
    out, w_long, w_short = [], {}, {}
    for d in weeks:
        uni = [s for s in p.universe(d, top) if d in p.feat[s]]
        if len(uni) < 25:
            continue
        ranks = {f: rank_normalise({s: p.feat[s][d][f] for s in uni}) for f in FEATURES}
        x = np.array([[ranks[f][s] for f in FEATURES] for s in uni])
        known = [(wk, xx, yy) for wk, xx, yy in hist if wk + 7 * DAY <= d][-window:]
        if len(known) >= min_weeks:
            beta = ridge(np.vstack([k[1] for k in known]), np.concatenate([k[2] for k in known]))
            pred = dict(zip(uni, x @ beta, strict=True))
            order = sorted(uni, key=pred.get)
            q = len(order) // 5
            shorts, longs = order[:q], order[-q:]
            lr, nl = _leg(p, longs, d, 7, +1)
            sr, ns = _leg(p, shorts, d, 7, -1)
            ur, _ = _leg(p, uni, d, 7, +1)
            new_l, new_s = {s: 1 / len(longs) for s in longs}, {s: 1 / len(shorts) for s in shorts}
            cl, cs = COST * _turnover(w_long, new_l), COST * _turnover(w_short, new_s)
            w_long, w_short = new_l, new_s
            if lr is not None and sr is not None:
                out.append({"day": d, "long": lr, "short": sr, "universe": ur, "long_net": lr - cl,
                            "short_net": sr - cs, "ls": 0.5 * (lr + sr), "ls_net": 0.5 * (lr - cl + sr - cs),
                            "n": len(uni)})
        rets = [p.ret(s, d, 7) for s in uni]
        ok = [i for i, r in enumerate(rets) if r is not None]
        if len(ok) >= 10:
            y = np.array([rets[i] for i in ok])
            hist.append((d, x[ok], y - y.mean()))
    return out


def blend(p: Panel, top: int = 30, buffer: float = 0.02) -> list[dict]:
    out, held = [], {}
    for d in p.days:
        if d + DAY > p.end:
            break
        uni = [s for s in p.universe(d, top) if d - 20 * DAY in p.bars[s]]
        if len(uni) < 10:
            continue
        carry = {s: -p.fund.get(s, {}).get(d, 0.0) for s in uni}
        mom = {s: p.bars[s][d][4] / p.bars[s][d - 10 * DAY][4] - 1 for s in uni if d - 10 * DAY in p.bars[s]}
        brk = {}
        for s in uni:
            highs = [(p.bars[s][d - k * DAY][2], k) for k in range(20) if d - k * DAY in p.bars[s]]
            brk[s] = 9.5 - max(highs)[1] if highs else 0.0
        z = {name: _z(sig, uni) for name, sig in (("carry", carry), ("mom", mom), ("brk", brk))}
        score = {s: 0.5 * z["carry"][s] + 0.2 * z["mom"][s] + 0.3 * z["brk"][s] for s in uni}
        mu = statistics.fmean(score.values())
        raw = {s: v - mu for s, v in score.items()}
        gross = sum(abs(v) for v in raw.values()) or 1.0
        target = {s: v / gross for s, v in raw.items()}
        new = dict(held)
        for s in set(held) | set(target):
            t = target.get(s, 0.0)
            if abs(t - held.get(s, 0.0)) > buffer or s not in target:
                new[s] = t
        new = {s: w for s, w in new.items() if abs(w) > 1e-12}
        cost = COST * _turnover(held, new)
        held = new
        pnl_l = pnl_s = 0.0
        for s, w in held.items():
            r = p.ret(s, d, 1)
            if r is None:
                continue
            leg = w * r - w * p.funding(s, d, 1)
            if w > 0:
                pnl_l += leg
            else:
                pnl_s += leg
        out.append({"day": d, "ls": pnl_l + pnl_s, "ls_net": pnl_l + pnl_s - cost, "long": pnl_l, "short": pnl_s,
                    "cost": cost})
    return out


def _z(sig: dict[str, float], uni: list[str]) -> dict[str, float]:
    vals = [sig[s] for s in uni if s in sig]
    if len(vals) < 3:
        return {s: 0.0 for s in uni}
    mu, sd = statistics.fmean(vals), statistics.pstdev(vals)
    return {s: ((sig[s] - mu) / sd if sd and s in sig else 0.0) for s in uni}


def summarise(rows: list[dict], key: str, per_year: int) -> dict[str, Any]:
    xs = [r[key] for r in rows if r.get(key) is not None]
    if len(xs) < 10:
        return {"n": len(xs)}
    mu, sd = statistics.fmean(xs), statistics.stdev(xs)
    yrs: dict[int, list[float]] = {}
    for r in rows:
        if r.get(key) is not None:
            yrs.setdefault(datetime.fromtimestamp(r["day"] / 1000, UTC).year, []).append(r[key])
    return {"n": len(xs), "mean": round(mu, 5), "t": round(mu / (sd / math.sqrt(len(xs))), 2) if sd else None,
            "sharpe_ann": round(mu / sd * math.sqrt(per_year), 2) if sd else None,
            "total": round(float(np.prod([1 + x for x in xs]) - 1), 3),
            "by_year": {y: round(statistics.fmean(v) * per_year, 3) for y, v in sorted(yrs.items())}}


def run(db_path: str = "data/daily.db") -> dict[str, Any]:
    from ..histdaily import load
    p = Panel(load(db_path))
    ct, bl = ctrend(p), blend(p)
    keys = ("long", "long_net", "short", "short_net", "ls", "ls_net")
    return {"ctrend": {k: summarise(ct, k, 52) for k in (*keys, "universe")},
            "blend": {k: summarise(bl, k, 365) for k in ("long", "short", "ls", "ls_net")},
            "ctrend_weeks": len(ct), "blend_days": len(bl)}


def record(res: dict[str, Any], prov: dict | None = None, out: Any = None) -> int:
    """Every reported leg is a trial in the trial register (#31; the old journal ledger's shape, source xs_daily)."""
    from .. import trial_register as TR
    rows = []
    for pid, block, legs in (("lib.ctrend_technical_aggregate", "ctrend", ("long_net", "short_net", "ls_net")),
                             ("lib.carry_momo_breakout_blend", "blend", ("long", "short", "ls_net"))):
        for leg in legs:
            s = res[block][leg]
            rows.append({"name": f"{block}|{leg}", "family": pid, "params": {"leg": leg}, "n": s.get("n"),
                         "mean": s.get("mean"), "t": s.get("t"), "t_kind": "iid_daily", "metric": "daily_ret"})
    return TR.record("xs_factors", rows, prov, out)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="shortbot.backtest.xs_factors")
    ap.add_argument("--db", type=Path, help="default: <data dir>/daily.db")
    ap.add_argument("--out", type=Path, help="JSON file (default: <data dir>/research/<date>-<sha7>/xs_factors/)")
    ap.add_argument("--no-save", action="store_true", help="print only")
    ap.add_argument("--record", action="store_true", help="no-op: every run records its legs (trial register, #31)")
    given = list(sys.argv[1:] if argv is None else argv)
    a = ap.parse_args(given)
    db = a.db or P.data_dir() / "daily.db"
    fp = P.fingerprint(db)
    res = run(str(db))
    prov = out = None
    if not a.no_save:
        argv_ = P.ensure_arg([x for x in given if x != "--record"], "--db", db)
        prov = P.stamp(P.config("shortbot.backtest.xs_factors", argv_), {"daily_db": fp})
        print("saved to", out := P.write_json("xs_factors", prov, res, a.out))
    res["recorded_trials"] = record(res, prov, out)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
