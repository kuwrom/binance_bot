"""How often should tier 1 run (task #40)? The scan_triggers bar rules evaluated every 1, 5 or 15 minutes, on 15m
windows aligned to the evaluation minute, over the replay's 1m perp bars.

Fixed before the first run:
- Data: the replay cache's 1m perp bars (replay_window.py; `--cache`), about 93 days. Universe: the top UNIVERSE
  coins by mean daily quote volume with at least 60 days of bars.
- Rules, the same definitions as scan_triggers, on windows ending at minute t:
  - move_1h / move_4h: the log return over 60 / 240 minutes, over the coin's 15m volatility (the up to 96 15m
    returns before the move, aligned to t); |z| >= 2;
  - volume_surge: the last 60 minutes' quote volume against the 29 prior aligned hours, robust z (median / MAD) >= 2;
  - lower_high_after_pump: the 24h return >= MIN_CHANGE and the last 60-minute high below the 60 minutes before
    (= four aligned 15m bars), newly true since the previous evaluation.
- An episode is a (coin, rule) trigger with no trigger of that rule on that coin in the previous 60 minutes at that
  cadence (scan_triggers.RESCORE_S). Each 15-minute episode is matched to the 1- and 5-minute episodes that started
  within the 60 minutes before it.
- Measured per rule and cadence:
  - episodes per day;
  - new episodes per 15-minute window, mean and p95: the tier-2 load;
  - the lead over the 15-minute cadence;
  - transients: 1/5-minute episodes with no 15-minute episode within 60 minutes after.
- Decision rule:
  - **1 min** if its median lead over 15 min is >= 5 min, its transients are <= 25%, and its p95 new-episode load
    is <= 30 coins per 15 min (the tier-2 cap at ~20 weight per coin);
  - else **5 min** under the same conditions with a lead of >= 3 min;
  - else keep **15 min**.
  - Why: the live-pipeline replay measured 0.022 R lost per 5 minutes of entry delay on pump fades (BACKTEST.md).
Descriptive (an engineering choice, no edge claim): exempt from the trial register like trigger_rates.

    .venv/bin/python -m shortbot.backtest.cadence_study --cache <replay cache.db> [--out file.json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as sw

from ..scan_job import MIN_CHANGE

UNIVERSE, MIN_DAYS = 180, 60
CADENCES = (1, 5, 15)
EPISODE_GAP = 60          # minutes (scan_triggers.RESCORE_S)
Z = 2.0
RULES = ("move_1h", "move_4h", "volume_surge", "lower_high_after_pump")


def universe(db: sqlite3.Connection, n: int = UNIVERSE) -> list[str]:
    rows = db.execute("SELECT symbol, COUNT(*), SUM(qv) FROM bars WHERE kind='perp' GROUP BY symbol").fetchall()
    ok = [(s, q / max(c, 1)) for s, c, q in rows if c >= MIN_DAYS * 1440 * 0.95]
    return [s for s, _ in sorted(ok, key=lambda x: -x[1])[:n]]


def load(db: sqlite3.Connection, sym: str) -> tuple[np.ndarray, ...]:
    """Minute grid from the first to the last bar; gaps forward-filled (close), zero volume, high = low = close."""
    a = np.array(db.execute("SELECT ts, h, l, c, qv FROM bars WHERE kind='perp' AND symbol=? ORDER BY ts",
                            (sym,)).fetchall(), dtype=float)
    t0 = int(a[0, 0]) // 60_000
    idx = (a[:, 0] // 60_000).astype(int) - t0
    n = idx[-1] + 1
    have = np.zeros(n, dtype=int)
    have[idx] = idx
    c = np.zeros(n)
    c[idx] = a[:, 3]
    c = c[np.maximum.accumulate(have)]          # forward-fill gaps with the last close
    h, lo, qv = c.copy(), c.copy(), np.zeros(n)
    h[idx], lo[idx], qv[idx] = a[:, 1], a[:, 2], a[:, 4]
    return t0 + np.arange(n), c, h, lo, qv


def move_z(c: np.ndarray, k: int) -> np.ndarray:
    """z at every minute of the k-bar 15m move (k = 4: 1h, 16: 4h), volatility from the aligned 15m returns before
    it (up to 96, at least 32)."""
    n = len(c)
    out = np.full(n, np.nan)
    lc = np.log(c)
    for o in range(15):                         # each alignment of the 15m grid
        s = lc[o::15]
        r = np.diff(s)                          # r[j] = return of the 15m bar ending at grid point j + 1
        if len(r) < k + 32:
            continue
        mv = sw(r, k).sum(1)                    # move ending at grid point j + k
        sd = np.full(len(r), np.nan)
        win = sw(r, 96).std(1) if len(r) >= 96 else None
        if win is not None:
            sd[95:] = win                       # std of r[j-95 .. j] at index j
        z = np.full(len(s), np.nan)
        j = np.arange(k + 96, len(s))           # grid point j: move = r[j-k .. j-1], vol = r[j-k-96 .. j-k-1]
        if len(j):
            v = sd[j - k - 1]
            with np.errstate(divide="ignore", invalid="ignore"):
                z[j] = np.where(v > 0, mv[j - k] / (v * np.sqrt(k)), np.nan)
        out[o::15] = z
    return out


def volume_z(qv: np.ndarray) -> np.ndarray:
    """Robust z of the last 60 minutes' volume vs the 29 prior aligned hours, at every minute."""
    n = len(qv)
    out = np.full(n, np.nan)
    cs = np.concatenate([[0.0], np.cumsum(qv)])
    hour = cs[60:] - cs[:-60]                   # hour[i] = volume of minutes i .. i + 59
    t = np.arange(60 * 30 - 1, n)               # minute t: last hour ends at t, i.e. starts at t - 59
    last = hour[t - 59]
    prior = np.stack([hour[t - 59 - 60 * j] for j in range(1, 30)], 1)
    med = np.median(prior, 1)
    mad = np.median(np.abs(prior - med[:, None]), 1) * 1.4826
    mad = np.where(mad > 0, mad, np.where(prior.std(1) > 0, prior.std(1), 1.0))
    out[t] = (last - med) / mad
    return out


def lower_high(c: np.ndarray, h: np.ndarray) -> np.ndarray:
    """24h return >= MIN_CHANGE and the last 60-minute high below the previous 60 minutes' high, at every minute."""
    n = len(c)
    out = np.zeros(n, dtype=bool)
    if n < 1440 + 120:
        return out
    mx = np.full(n, np.nan)
    mx[59:] = sw(h, 60).max(1)                  # high of minutes t-59 .. t
    t = np.arange(1440 + 119, n)
    out[t] = (mx[t] < mx[t - 60]) & ((c[t] / c[t - 1440] - 1) * 100 >= MIN_CHANGE)
    return out


def episodes(flags: np.ndarray, minutes: np.ndarray, cadence: int, new_only: bool = False) -> np.ndarray:
    """Episode start minutes at this cadence: evaluated where minute % cadence == 0."""
    sel = minutes % cadence == 0
    m, f = minutes[sel], flags[sel]
    if new_only:                                # "newly true since the previous evaluation"
        f = f & ~np.concatenate([[False], f[:-1]])
    hits = m[f]
    if not len(hits):
        return hits
    keep = np.concatenate([[True], np.diff(hits) > EPISODE_GAP])
    return hits[keep]


def coin(c, h, lo, qv, minutes) -> dict[str, dict[int, np.ndarray]]:
    z1, z4, vz, lh = move_z(c, 4), move_z(c, 16), volume_z(qv), lower_high(c, h)
    flags = {"move_1h": np.abs(np.nan_to_num(z1)) >= Z, "move_4h": np.abs(np.nan_to_num(z4)) >= Z,
             "volume_surge": np.nan_to_num(vz) >= Z, "lower_high_after_pump": lh}
    return {r: {cd: episodes(f, minutes, cd, new_only=(r == "lower_high_after_pump")) for cd in CADENCES}
            for r, f in flags.items()}


def compare(eps: dict[int, np.ndarray]) -> dict[int, dict[str, Any]]:
    """Leads of the 1- and 5-minute cadences over 15, and their transients (by cadence)."""
    e15 = eps[15]
    out: dict[int, dict[str, Any]] = {}
    for cd in (1, 5):
        e = eps[cd]
        leads = []
        for t in e15:
            j = np.searchsorted(e, t, side="right") - 1
            if j >= 0 and t - e[j] <= EPISODE_GAP:
                leads.append(t - e[j])
        nxt = np.searchsorted(e15, e, side="left")           # the first 15-minute episode at or after t
        later = np.where(nxt < len(e15), e15[np.minimum(nxt, len(e15) - 1)], np.inf)
        trans = int(np.sum(later > e + EPISODE_GAP))
        out[cd] = {"leads": leads, "transients": trans, "n": len(e)}
    return out


def run(cache: str, n: int = UNIVERSE) -> dict[str, Any]:
    db = sqlite3.connect(f"file:{cache}?mode=ro", uri=True)
    syms = universe(db, n)
    starts: dict[str, dict[int, list[int]]] = {r: {cd: [] for cd in CADENCES} for r in RULES}
    leads: dict[str, dict[int, list[int]]] = {r: {1: [], 5: []} for r in RULES}
    trans: dict[str, dict[int, list[int]]] = {r: {1: [0, 0], 5: [0, 0]} for r in RULES}
    span = [10 ** 12, 0]
    for s in syms:
        minutes, c, h, lo, qv = load(db, s)
        span = [min(span[0], minutes[0]), max(span[1], minutes[-1])]
        for r, eps in coin(c, h, lo, qv, minutes).items():
            for cd in CADENCES:
                starts[r][cd].extend(eps[cd].tolist())
            for cd, v in compare(eps).items():
                leads[r][cd].extend(v["leads"])
                trans[r][cd][0] += v["transients"]
                trans[r][cd][1] += v["n"]
    days = (span[1] - span[0]) / 1440
    windows = int((span[1] - span[0]) // 15) + 1
    res: dict[str, Any] = {"coins": len(syms), "days": round(days, 1), "rules": {}}
    all_starts: dict[int, list[int]] = {cd: [] for cd in CADENCES}
    for r in RULES:
        row: dict[str, Any] = {}
        for cd in CADENCES:
            st = np.array(starts[r][cd])
            all_starts[cd].extend(starts[r][cd])
            row[f"{cd}m"] = {"episodes_per_day": round(len(st) / days, 1)}
            if cd in (1, 5):
                lv = np.array(leads[r][cd]) if leads[r][cd] else np.array([0])
                row[f"{cd}m"].update(lead_median=float(np.median(lv)), lead_mean=round(float(lv.mean()), 1),
                                     matched=len(leads[r][cd]),
                                     transients=round(trans[r][cd][0] / max(trans[r][cd][1], 1), 3))
        res["rules"][r] = row
    per15 = {}
    for cd in CADENCES:
        w = np.bincount(((np.array(all_starts[cd]) - span[0]) // 15).astype(int), minlength=windows)
        per15[f"{cd}m"] = {"mean": round(float(w.mean()), 2), "p95": float(np.percentile(w, 95)),
                          "p99": float(np.percentile(w, 99)), "max": int(w.max())}
    res["new_episodes_per_15m"] = per15
    res["decision"] = decide(res)
    return res


def decide(res: dict[str, Any]) -> dict[str, Any]:
    """The pre-registered rule, on the pooled leads (all rules) and the p95 load."""
    out: dict[str, Any] = {}
    for cd, need in ((1, 5), (5, 3)):
        meds = [res["rules"][r][f"{cd}m"]["lead_median"] for r in RULES]
        tr = [res["rules"][r][f"{cd}m"]["transients"] for r in RULES]
        ok = (float(np.median(meds)) >= need and max(tr) <= 0.25
              and res["new_episodes_per_15m"][f"{cd}m"]["p95"] <= 30)
        out[f"{cd}m"] = {"median_lead": float(np.median(meds)), "max_transients": max(tr),
                         "p95_load": res["new_episodes_per_15m"][f"{cd}m"]["p95"], "passes": ok}
    out["choice"] = "1m" if out["1m"]["passes"] else "5m" if out["5m"]["passes"] else "15m"
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="shortbot.backtest.cadence_study")
    ap.add_argument("--cache", required=True, help="the replay cache with 1m perp bars (replay_window.py)")
    ap.add_argument("--coins", type=int, default=UNIVERSE)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    res = run(a.cache, a.coins)
    if a.out:
        a.out.write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
