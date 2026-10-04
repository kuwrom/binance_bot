"""Carry regime study (task #12; library card lib.carry_crash_regime; BIS WP 1087 "Crypto carry").

Question: does high aggregate BTC/ETH carry mark a crash-prone regime in which shorting alts pays more, and does low or
negative carry mark one where new shorts should be cut? Daily data from 2020 to the last archived month
(histdaily.py, data/daily.db). Everything below was fixed BEFORE the first run; every cell is a trial.

- carry C_d: mean over BTC and ETH of the 7-day mean of daily funding (sum of the day's prints) x 365.
- percentile: rank of C_d among the previous 365 days (>= 180 days of history required). No look-ahead.
- regimes: top decile (p >= 0.9), top quintile (p >= 0.8), bottom quintile (p <= 0.2), C >= 20%, C <= 0.
- outcomes (entry at day d's close, short side, funding the short receives included, gross of fees):
  - alt basket short return over 7d and 30d: equal weight, >= 30 prior bars, 24h volume >= $5M, ex BTC/ETH/stables;
  - BTC short return over 7d and 30d;
  - top-gainer fade: coins up >= +20% on day d, short at the close, held 1d and 3d;
  - crash: the alt basket's worst close within 30d is <= -25%.
  A coin that stops trading inside the window is closed at its last close (delivery).
- stats: regime minus rest, OLS on a regime dummy with a Newey-West (Bartlett, lag = horizon) t, which accounts for
  overlapping windows. Plus a per-year split.
- verdict: |t| >= 3 and the same sign in >= 2/3 of the years with >= 20 regime days -> GATE; |t| >= 2 -> supported;
  otherwise no evidence. Sign convention: positive = shorts do better in the regime.

    .venv/bin/python -m shortbot.backtest.carry_regime [--record]   # --record writes the trials to the registry
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from .. import provenance as P

DAY = 86_400_000
STABLES = {"USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "USDPUSDT", "DAIUSDT", "USDEUSDT", "EURUSDT"}
MAJORS = ("BTCUSDT", "ETHUSDT")
CARRY_DAYS, PCT_LOOKBACK, PCT_MIN = 7, 365, 180
MIN_AGE, MIN_QV, GAINER, CRASH = 30, 5e6, 0.20, -0.25
REGIMES = {"top_decile": lambda p, c: p >= 0.9, "top_quintile": lambda p, c: p >= 0.8,
           "bottom_quintile": lambda p, c: p <= 0.2, "carry_ge_20pct": lambda p, c: c >= 0.20,
           "carry_le_0": lambda p, c: c <= 0.0}
OUTCOMES = {"alt_short_7d": 7, "alt_short_30d": 30, "btc_short_7d": 7, "btc_short_30d": 30,
            "fade_short_1d": 1, "fade_short_3d": 3, "alt_crash_30d": 30}


def daily_funding(rows: list[tuple]) -> dict[int, float]:
    """[(ts, rate)] -> {day_ms: sum of the day's funding prints} (works for 8h, 4h and 1h intervals alike)."""
    out: dict[int, float] = {}
    for ts, rate in rows:
        d = ts // DAY * DAY
        out[d] = out.get(d, 0.0) + rate
    return out


def carry_series(funding: dict[str, list[tuple]]) -> dict[int, float]:
    """{day: annualised carry} = mean over BTC/ETH of the trailing CARRY_DAYS mean of daily funding x 365. A day is
    only scored once every major has a full window ending on it."""
    per = [daily_funding(funding.get(m, [])) for m in MAJORS]
    days = sorted(set.intersection(*(set(p) for p in per))) if all(per) else []
    out = {}
    for d in days:
        win = [d - k * DAY for k in range(CARRY_DAYS)]
        if all(all(w in p for w in win) for p in per):
            out[d] = sum(sum(p[w] for w in win) / CARRY_DAYS for p in per) / len(per) * 365
    return out


def percentiles(series: dict[int, float]) -> dict[int, float]:
    """Rank of each day's value among the PREVIOUS PCT_LOOKBACK days (strictly earlier: no look-ahead)."""
    days = sorted(series)
    out = {}
    for i, d in enumerate(days):
        past = [series[x] for x in days[max(0, i - PCT_LOOKBACK):i]]
        if len(past) >= PCT_MIN:
            out[d] = sum(v < series[d] for v in past) / len(past)
    return out


def _closes(bars: list[tuple]) -> dict[int, tuple[float, float]]:
    return {b[0]: (b[4], b[5]) for b in bars}       # day -> (close, quote volume)


def _exit_close(cl: dict[int, tuple], d: int, h: int, last: int, end: int | None = None) -> float | None:
    """Close h days after d; a coin that stopped trading before then exits at its last close (delivery). A window
    that runs past the end of the DATA (`end`) has no exit: the coin may still be trading."""
    t = d + h * DAY
    if end is not None and t > end:
        return None
    if t in cl:
        return cl[t][0]
    return cl[last][0] if last < t and last > d else None


def _short_ret(entry: float, exit_: float | None, fund: dict[int, float], d: int, h: int) -> float | None:
    if exit_ is None or entry <= 0:
        return None
    received = sum(fund.get(d + k * DAY, 0.0) for k in range(1, h + 1))    # funding paid TO the short
    return -(exit_ / entry - 1) + received


def outcomes(data: dict[str, Any], days: list[int]) -> dict[str, dict[int, float]]:
    """Per-day outcome series (see module doc). Missing days mean no eligible coins / no data."""
    bars, funding = data["bars"], data["funding"]
    cl = {s: _closes(b) for s, b in bars.items()}
    first = {s: b[0][0] for s, b in bars.items() if b}
    last = {s: b[-1][0] for s, b in bars.items() if b}
    fund = {s: daily_funding(r) for s, r in funding.items()}
    alts = [s for s in bars if s not in MAJORS and s not in STABLES]
    end = max(last.values())
    out: dict[str, dict[int, float]] = {k: {} for k in OUTCOMES}
    for d in days:
        elig = [s for s in alts if d in cl[s] and d - MIN_AGE * DAY >= first[s] and cl[s][d][1] >= MIN_QV]
        for h, key in ((7, "alt_short_7d"), (30, "alt_short_30d")):
            rs = [r for s in elig if (r := _short_ret(cl[s][d][0], _exit_close(cl[s], d, h, last[s], end),
                                                      fund.get(s, {}), d, h)) is not None]
            if len(rs) >= 10:
                out[key][d] = float(np.mean(rs))
        b = cl.get("BTCUSDT", {})
        if d in b:
            for h, key in ((7, "btc_short_7d"), (30, "btc_short_30d")):
                r = _short_ret(b[d][0], _exit_close(b, d, h, last["BTCUSDT"], end), fund.get("BTCUSDT", {}), d, h)
                if r is not None:
                    out[key][d] = r
        gainers = [s for s in elig if d - DAY in cl[s] and cl[s][d][0] / cl[s][d - DAY][0] - 1 >= GAINER]
        for h, key in ((1, "fade_short_1d"), (3, "fade_short_3d")):
            rs = [r for s in gainers if (r := _short_ret(cl[s][d][0], _exit_close(cl[s], d, h, last[s], end),
                                                         fund.get(s, {}), d, h)) is not None]
            if rs:
                out[key][d] = float(np.mean(rs))
        if len(elig) >= 10 and d + 30 * DAY <= end:
            path = []
            for k in range(1, 31):
                rs = [(_exit_close(cl[s], d, k, last[s], end) or cl[s][d][0]) / cl[s][d][0] - 1 for s in elig]
                path.append(float(np.mean(rs)))
            out["alt_crash_30d"][d] = 1.0 if min(path) <= CRASH else 0.0
    return out


def nw_diff(y: np.ndarray, dummy: np.ndarray, lag: int) -> tuple[float, float | None]:
    """OLS y = a + b*dummy; returns (b, Newey-West t of b) with Bartlett weights up to `lag`."""
    x = np.column_stack([np.ones_like(y), dummy.astype(float)])
    if dummy.sum() < 2 or (1 - dummy).sum() < 2:
        return float("nan"), None
    xtx_inv = np.linalg.inv(x.T @ x)
    beta = xtx_inv @ x.T @ y
    u = y - x @ beta
    xu = x * u[:, None]
    s = xu.T @ xu
    for k in range(1, min(lag, len(y) - 1) + 1):
        w = 1 - k / (lag + 1)
        g = xu[k:].T @ xu[:-k]
        s += w * (g + g.T)
    var = xtx_inv @ s @ xtx_inv
    se = math.sqrt(var[1, 1]) if var[1, 1] > 0 else None
    return float(beta[1]), (float(beta[1] / se) if se else None)


def year_of(d: int) -> int:
    return datetime.fromtimestamp(d / 1000, UTC).year


def evaluate(pct: dict[int, float], carry: dict[int, float], outs: dict[str, dict[int, float]]) -> list[dict]:
    rows = []
    for rname, rule in REGIMES.items():
        for oname, h in OUTCOMES.items():
            ser = outs[oname]
            days = [d for d in sorted(ser) if d in pct]
            if len(days) < 60:
                continue
            y = np.array([ser[d] for d in days])
            dmy = np.array([rule(pct[d], carry[d]) for d in days])
            diff, t = nw_diff(y, dmy, h)
            years: dict[int, list] = {}
            for d, v, flag in zip(days, y, dmy, strict=True):
                years.setdefault(year_of(d), [[], []])[0 if flag else 1].append(v)
            per_year = {yr: round(float(np.mean(a) - np.mean(b)), 4)
                        for yr, (a, b) in years.items() if len(a) >= 20 and b}
            same = sum(np.sign(v) == np.sign(diff) for v in per_year.values())
            verdict = ("GATE" if t is not None and abs(t) >= 3 and per_year and same >= 2 / 3 * len(per_year)
                       else "supported" if t is not None and abs(t) >= 2 else "no evidence")
            rows.append({"regime": rname, "outcome": oname, "n_days": len(days), "regime_days": int(dmy.sum()),
                         "mean_regime": round(float(y[dmy].mean()), 4) if dmy.any() else None,
                         "mean_rest": round(float(y[~dmy].mean()), 4), "diff": round(diff, 4),
                         "t_nw": None if t is None else round(t, 2), "per_year": per_year,
                         "years_same_sign": f"{same}/{len(per_year)}", "verdict": verdict})
    return rows


def run(db_path: str = "data/daily.db") -> dict[str, Any]:
    from ..histdaily import load
    data = load(db_path)
    carry = carry_series(data["funding"])
    pct = percentiles(carry)
    days = sorted(pct)
    outs = outcomes(data, days)
    return {"window": [datetime.fromtimestamp(days[0] / 1000, UTC).date().isoformat(),
                       datetime.fromtimestamp(days[-1] / 1000, UTC).date().isoformat()] if days else None,
            "coins": len(data["bars"]), "carry_days": len(carry),
            "carry_quantiles": [round(float(q), 3) for q in np.quantile(list(carry.values()), [.1, .5, .9])],
            "rows": evaluate(pct, carry, outs)}


def record(res: dict[str, Any], prov: dict | None = None, out: Any = None) -> int:
    """Every cell is a trial of lib.carry_crash_regime in the trial register (#31; the old ledger's shape)."""
    from .. import trial_register as TR
    rows = [{"name": f"{r['regime']}|{r['outcome']}", "family": "lib.carry_crash_regime",
             "params": {"regime": r["regime"], "outcome": r["outcome"]}, "n": r["regime_days"], "mean": r["diff"],
             "t": r["t_nw"], "t_kind": "newey_west", "metric": "regime_minus_rest"} for r in res["rows"]]
    return TR.record("carry_regime", rows, prov, out)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="shortbot.backtest.carry_regime")
    ap.add_argument("--db", type=Path, help="default: <data dir>/daily.db")
    ap.add_argument("--out", type=Path, help="JSON file (default: <data dir>/research/<date>-<sha7>/carry_regime/)")
    ap.add_argument("--no-save", action="store_true", help="print only")
    ap.add_argument("--record", action="store_true", help="no-op: every run records its cells (trial register, #31)")
    given = list(sys.argv[1:] if argv is None else argv)
    args = ap.parse_args(given)
    db = args.db or P.data_dir() / "daily.db"
    fp = P.fingerprint(db)
    res = run(str(db))
    prov = out = None
    if not args.no_save:
        argv_ = P.ensure_arg([x for x in given if x != "--record"], "--db", db)
        prov = P.stamp(P.config("shortbot.backtest.carry_regime", argv_), {"daily_db": fp})
        print("saved to", out := P.write_json("carry_regime", prov, res, args.out))
    res["recorded_trials"] = record(res, prov, out)
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}, indent=1))
    for r in res["rows"]:
        print(f"{r['regime']:16} {r['outcome']:14} n={r['n_days']:5} reg={r['regime_days']:4} "
              f"reg={r['mean_regime']} rest={r['mean_rest']} diff={r['diff']:+.4f} t={r['t_nw']} "
              f"yrs={r['years_same_sign']} {r['verdict']}")


if __name__ == "__main__":
    main()
