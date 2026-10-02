"""High-funding short events by coin class (task #13; library cards lib.high_funding_trend_confirm and
lib.high_funding_squeeze_veto). Question: does shorting on high funding lose on MAJORS, so that funding_hi should
stay with post-pump alts plus a price trigger? Daily data (histdaily.py). Fixed before the first run:

- event: a coin's average funding per 8h over day d (daily sum / 3, so 1h/4h intervals compare) >= thr,
  thr in {0.05%, 0.10%}. The first qualifying day of a run counts; a new event needs 7 quiet days.
- class at d:
  - major: top 10 by 30-day mean quote volume among coins trading that day (dynamic, no look-ahead);
  - pumped_alt: a non-major up >= 30% over 7 days;
  - other_alt: the remaining non-majors.
  Eligibility: >= 30 prior bars and 24h volume >= $5M.
- entry: 'raw' = short at day d's close. 'breakdown' = short at the close of the first day within the next 3 whose
  close is below the prior day's low (no entry otherwise).
- outcome: short return over 1, 3 and 7 days, including the funding received, minus 0.10% round-trip fees.
- control: edge = event return minus the mean short return of every eligible coin of the same class over the SAME
  window (from the entry day: a breakdown entry starts 1-3 days after the event). This cancels the market regime.
- stats: the mean of day-means and its t (events on one day are one bet) for the return and the edge; the pooled
  mean is reported alongside it; per-year mean edge.
- verdict per cell:
  - 'avoid' if edge < 0 with t <= -2;
  - 'works' if edge > 0, t >= 2 and the mean return > 0;
  - otherwise 'no evidence'.

    .venv/bin/python -m shortbot.backtest.funding_events [--record]
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from typing import Any

from .carry_regime import DAY, MIN_AGE, MIN_QV, STABLES, _exit_close, _short_ret, daily_funding, year_of

THRESHOLDS = (0.0005, 0.0010)
HORIZONS = (1, 3, 7)
ENTRIES = ("raw", "breakdown")
CLASSES = ("major", "pumped_alt", "other_alt")
TOP_N, PUMP_7D, QUIET_DAYS, CONFIRM_DAYS, FEES = 10, 0.30, 7, 3, 0.001


def classify(day: int, vol30: dict[str, float], ret7: dict[str, float]) -> dict[str, str]:
    """{symbol: class} for the coins eligible on `day` (vol30/ret7 computed from data up to `day` only)."""
    majors = set(sorted(vol30, key=vol30.get, reverse=True)[:TOP_N])
    return {s: "major" if s in majors else "pumped_alt" if ret7.get(s, 0.0) >= PUMP_7D else "other_alt"
            for s in vol30}


def breakdown_day(bars: dict[int, tuple], d: int) -> int | None:
    """First day in (d, d+CONFIRM_DAYS] whose close is below the prior day's low."""
    for k in range(1, CONFIRM_DAYS + 1):
        t, p = d + k * DAY, d + (k - 1) * DAY
        if t in bars and p in bars and bars[t][3] < bars[p][2]:
            return t
    return None


def _t_clustered(rows: list[dict], key: str) -> tuple[float | None, float | None, int]:
    """(mean of day-means, its t, n days): events on one day are one bet. Mean and t come from the SAME estimator,
    so a few huge outliers (a coin 5x-ing) cannot give the pooled mean one sign and the t the other."""
    by: dict[int, list[float]] = {}
    for r in rows:
        by.setdefault(r["day"], []).append(r[key])
    means = [statistics.fmean(v) for v in by.values()]
    if len(means) < 3:
        return None, None, len(means)
    mu, sd = statistics.fmean(means), statistics.stdev(means)
    return mu, (mu / (sd / math.sqrt(len(means))) if sd else None), len(means)


def events(data: dict[str, Any]) -> list[dict]:
    """Every (event, entry, horizon) trade with its class, return and same-window class control."""
    bars = {s: {b[0]: b[1:] for b in rows} for s, rows in data["bars"].items() if s not in STABLES}
    fund = {s: daily_funding(r) for s, r in data["funding"].items()}
    closes = {s: {k: (v[3],) for k, v in b.items()} for s, b in bars.items()}    # the shape _exit_close reads
    first = {s: min(b) for s, b in bars.items() if b}
    last = {s: max(b) for s, b in bars.items() if b}
    end = max(last.values())
    days = sorted({d for b in bars.values() for d in b})
    cls_at: dict[int, dict[str, str]] = {}
    control: dict[tuple[int, str, int], float] = {}      # (entry day, class, h) -> class mean short return that day
    for d in days:                                        # pass 1: classes and controls for every day
        elig = [s for s, b in bars.items() if d in b and d - MIN_AGE * DAY >= first[s] and b[d][4] >= MIN_QV]
        if len(elig) < 20:
            continue
        vol30 = {s: statistics.fmean(bars[s][x][4] for x in range(d - 29 * DAY, d + 1, DAY) if x in bars[s])
                 for s in elig}
        ret7 = {s: bars[s][d][3] / bars[s][d - 7 * DAY][3] - 1 for s in elig if d - 7 * DAY in bars[s]}
        cls_at[d] = cls = classify(d, vol30, ret7)
        for c in CLASSES:
            for h in HORIZONS:
                rs = [r for s in elig if cls[s] == c and (r := _short_ret(
                    bars[s][d][3], _exit_close(closes[s], d, h, last[s], end), fund.get(s, {}), d, h)) is not None]
                if rs:
                    control[(d, c, h)] = statistics.fmean(rs) - FEES
    out: list[dict] = []
    quiet: dict[tuple[str, float], int] = {}
    for d in days:                                        # pass 2: events; the control starts on the ENTRY day
        cls = cls_at.get(d)
        if cls is None:
            continue
        for s in cls:
            rate8h = fund.get(s, {}).get(d, 0.0) / 3
            for thr in THRESHOLDS:
                if rate8h < thr:
                    continue
                if d - quiet.get((s, thr), -10 ** 15) < QUIET_DAYS * DAY:
                    quiet[(s, thr)] = d
                    continue
                quiet[(s, thr)] = d
                for entry in ENTRIES:
                    e = d if entry == "raw" else breakdown_day(bars[s], d)
                    if e is None:
                        continue
                    for h in HORIZONS:
                        r = _short_ret(bars[s][e][3], _exit_close(closes[s], e, h, last[s], end),
                                       fund.get(s, {}), e, h)
                        ctl = control.get((e, cls[s], h))
                        if r is None or ctl is None:
                            continue
                        out.append({"symbol": s, "day": d, "cls": cls[s], "thr": thr, "entry": entry, "h": h,
                                    "ret": r - FEES, "edge": r - FEES - ctl})
    return out


def evaluate(trades: list[dict]) -> list[dict]:
    rows = []
    for thr in THRESHOLDS:
        for entry in ENTRIES:
            for c in CLASSES:
                for h in HORIZONS:
                    xs = [t for t in trades if t["thr"] == thr and t["entry"] == entry and t["cls"] == c
                          and t["h"] == h]
                    if len(xs) < 10:
                        continue
                    mean_ret, t_ret, n_days = _t_clustered(xs, "ret")
                    mean_edge, t_edge, _ = _t_clustered(xs, "edge")
                    if mean_ret is None or mean_edge is None:
                        continue
                    yrs: dict[int, list[float]] = {}
                    for x in xs:
                        yrs.setdefault(year_of(x["day"]), []).append(x["edge"])
                    verdict = ("avoid" if t_edge is not None and mean_edge < 0 and t_edge <= -2 else
                               "works" if t_edge is not None and mean_edge > 0 and t_edge >= 2 and mean_ret > 0
                               else "no evidence")
                    pooled = statistics.fmean(x["edge"] for x in xs)
                    rows.append({"thr_pct_8h": thr * 100, "entry": entry, "cls": c, "h": h, "n": len(xs),
                                 "days": n_days, "coins": len({x["symbol"] for x in xs}),
                                 "mean_ret": round(mean_ret, 4), "t_ret": None if t_ret is None else round(t_ret, 2),
                                 "mean_edge": round(mean_edge, 4),
                                 "t_edge": None if t_edge is None else round(t_edge, 2),
                                 "pooled_edge": round(pooled, 4),
                                 "win": round(sum(x["ret"] > 0 for x in xs) / len(xs), 3),
                                 "edge_by_year": {y: round(statistics.fmean(v), 4) for y, v in sorted(yrs.items())},
                                 "verdict": verdict})
    return rows


def run(db_path: str = "data/daily.db") -> dict[str, Any]:
    from ..histdaily import load
    trades = events(load(db_path))
    return {"trades": len(trades), "rows": evaluate(trades)}


def record(res: dict[str, Any], db_path: str = "data/shortbot.db") -> int:
    from .. import registry as R
    db = sqlite3.connect(db_path, timeout=30)
    for r in res["rows"]:
        pid = "lib.high_funding_trend_confirm" if r["cls"] == "major" else "lib.high_funding_squeeze_veto"
        R.add_trial(db, pid, {k: r[k] for k in ("thr_pct_8h", "entry", "cls", "h")}, "funding_events_daily",
                    {"n": r["n"], "n_coins": r["coins"], "mean": r["mean_edge"], "t": r["t_edge"]})
    db.close()
    return len(res["rows"])


def main() -> None:
    ap = argparse.ArgumentParser(prog="shortbot.backtest.funding_events")
    ap.add_argument("--db", default="data/daily.db")
    ap.add_argument("--out", default=None)
    ap.add_argument("--record", action="store_true")
    args = ap.parse_args()
    res = run(args.db)
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(res, fh, indent=1)
    if args.record:
        res["recorded_trials"] = record(res)
    print(f"trades {res['trades']}")
    for r in res["rows"]:
        print(f"{r['thr_pct_8h']:.2f}% {r['entry']:9} {r['cls']:10} {r['h']}d n={r['n']:5} coins={r['coins']:4} "
              f"ret={r['mean_ret']:+.4f} (t {r['t_ret']}) edge={r['mean_edge']:+.4f} (t {r['t_edge']}) "
              f"win={r['win']} {r['verdict']}")


if __name__ == "__main__":
    main()
