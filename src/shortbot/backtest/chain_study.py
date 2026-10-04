"""Do chained multi-timeframe rules beat their single-timeframe parts (task #41)? Research only.

Fixed before the first run (every chain x horizon is a trial in the register, study chain_study):
- Data: the replay cache's 1m perp bars (~93 days), aggregated exactly to 5m / 15m / 1h / 4h. Universe: the top
  UNIVERSE coins by mean quote volume with >= 60 days of bars (as cadence_study).
- States: mtf.series on each timeframe. At a 15m close T, each timeframe's last bar that closed by T.
- Events: a chain fires at the first 15m close where all of its conditions hold, then waits COOLDOWN (24 h) on that
  coin. Entry is the close at T.
- Outcome: the side-signed return at +1h / +4h / +24h minus the same side-signed equal-weight return of the universe
  over the same window (market-adjusted excess), less 10 bp. t is two-way clustered by day and coin.

Chains (from the desk's playbooks: the pump-fade skill's "lower high on 1h (and 15m)", learning #17's "don't short
within ~1% of the 24h high" (here 3%), the taker-flow veto; and their long mirrors):
- S0 pumped: the 24h return >= MIN_CHANGE (the population, short);
- S1 pumped & 15m lower high;
- S2 pumped & 1h lower high;
- S3 pumped & 1h lower high & 15m lower high (the skill's chain);
- S4 S3 & 5m taker imbalance < 0 (selling over the last 20 min);
- S5 pumped & 4h RSI >= 70 & 1h below its EMA20 & 15m lower high;
- S6 pumped & 15m >= 3% off its 24-bar high & 1h lower high;
- L1 dumped (24h <= -MIN_CHANGE) & 1h higher low & 15m higher low (long, the mirror of S3);
- L2 4h EMA20 rising and price above it & 1h higher low & 15m move z >= 2 (long, trend plus momentum).
- Decision (for wiring a chain as a tier-1 trigger): excess at +24h > 0 with t >= 2, and above its single-timeframe
  parent (S3, S4 and S6 vs S1 and S2; S5 vs S1) by >= 0.5 pp at +24h.

    .venv/bin/python -m shortbot.backtest.chain_study --cache <replay cache.db> [--no-save]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .. import cluster_se as C
from .. import mtf
from ..scan_job import MIN_CHANGE
from .cadence_study import universe

M15 = 900_000
STEP = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
HORIZONS = {"1h": 4, "4h": 16, "24h": 96}           # in 15m steps
COOLDOWN, COST = 96, 0.001
PUMP = MIN_CHANGE / 100
CHAINS = {
    "S0_pumped": (-1, lambda s: s["1h"]["ret"] >= PUMP),
    "S1_pumped_lh15": (-1, lambda s: (s["1h"]["ret"] >= PUMP) & (s["15m"]["lower_high"] > 0)),
    "S2_pumped_lh1h": (-1, lambda s: (s["1h"]["ret"] >= PUMP) & (s["1h"]["lower_high"] > 0)),
    "S3_pumped_lh1h_lh15": (-1, lambda s: (s["1h"]["ret"] >= PUMP) & (s["1h"]["lower_high"] > 0)
                            & (s["15m"]["lower_high"] > 0)),
    "S4_S3_taker_selling": (-1, lambda s: (s["1h"]["ret"] >= PUMP) & (s["1h"]["lower_high"] > 0)
                            & (s["15m"]["lower_high"] > 0) & (s["5m"]["ti"] < 0)),
    "S5_rsi4h_lost_ema1h_lh15": (-1, lambda s: (s["1h"]["ret"] >= PUMP) & (s["4h"]["rsi"] >= 70)
                                 & (s["1h"]["ema_gap"] < 0) & (s["15m"]["lower_high"] > 0)),
    "S6_pumped_off_high_lh1h": (-1, lambda s: (s["1h"]["ret"] >= PUMP) & (s["15m"]["off_high"] <= -0.03)
                                & (s["1h"]["lower_high"] > 0)),
    "L1_dumped_hl1h_hl15": (1, lambda s: (s["1h"]["ret"] <= -PUMP) & (s["1h"]["higher_low"] > 0)
                            & (s["15m"]["higher_low"] > 0)),
    "L2_trend4h_hl1h_momo15": (1, lambda s: (s["4h"]["ema_slope"] > 0) & (s["4h"]["ema_gap"] > 0)
                               & (s["1h"]["higher_low"] > 0) & (s["15m"]["move_z"] >= 2)),
}
PARENTS = {"S3_pumped_lh1h_lh15": ("S1_pumped_lh15", "S2_pumped_lh1h"),
           "S4_S3_taker_selling": ("S1_pumped_lh15", "S2_pumped_lh1h"),
           "S6_pumped_off_high_lh1h": ("S1_pumped_lh15", "S2_pumped_lh1h"),
           "S5_rsi4h_lost_ema1h_lh15": ("S1_pumped_lh15",)}


def minute_bars(db: sqlite3.Connection, sym: str) -> pd.DataFrame:
    df = pd.DataFrame(db.execute("SELECT ts, o, h, l, c, v, tbv FROM bars WHERE kind='perp' AND symbol=? ORDER BY ts",
                                 (sym,)).fetchall(), columns=["ts", "o", "h", "l", "c", "v", "tbv"])
    return df.astype({"ts": "int64"})


def aggregate(df: pd.DataFrame, step: int) -> np.ndarray:
    """1m rows -> rows [ts, o, h, l, c, v, tbv] of `step` (exact: first open, max high, min low, last close, sums).
    Only complete buckets are kept."""
    g = df.groupby(df["ts"] // step)
    out = pd.DataFrame({"ts": g["ts"].first() // step * step, "o": g["o"].first(), "h": g["h"].max(),
                        "l": g["l"].min(), "c": g["c"].last(), "v": g["v"].sum(), "tbv": g["tbv"].sum(),
                        "n": g["ts"].count()})
    return out[out["n"] == step // 60_000][["ts", "o", "h", "l", "c", "v", "tbv"]].to_numpy(dtype=float)


def states_at(df: pd.DataFrame, grid: np.ndarray) -> dict[str, dict[str, np.ndarray]]:
    """For each timeframe, each mtf column at every 15m close in `grid`, from the last bar closed by then."""
    out = {}
    for tf, step in STEP.items():
        bars = aggregate(df, step)
        if not len(bars):
            out[tf] = {k: np.full(len(grid), np.nan) for k in mtf.COLUMNS}
            continue
        s = mtf.series(bars, tf)
        j = np.searchsorted(bars[:, 0] + step, grid, side="right") - 1   # last bar with close <= T
        ok = j >= 0
        out[tf] = {k: np.where(ok, s[k][np.clip(j, 0, None)], np.nan) for k in mtf.COLUMNS}
    return out


def events(flags: np.ndarray) -> np.ndarray:
    """Indices where flags turn on, then COOLDOWN 15m steps of silence."""
    out, nxt = [], -1
    for i in np.flatnonzero(flags):
        if i >= nxt:
            out.append(i)
            nxt = int(i) + COOLDOWN
    return np.array(out, dtype=int)


def run(cache: str, n: int = 180) -> dict[str, Any]:
    db = sqlite3.connect(f"file:{cache}?mode=ro", uri=True)
    syms = universe(db, n)
    span = [db.execute("SELECT MIN(ts), MAX(ts) FROM bars WHERE kind='perp' AND symbol=?", (s,)).fetchone()
            for s in syms]
    t0 = min(a for a, _ in span) // M15 * M15 + M15
    t1 = max(b for _, b in span) // M15 * M15
    grid = np.arange(t0, t1 + 1, M15)
    closes = np.full((len(grid), len(syms)), np.nan)
    hits: dict[str, list[tuple[int, int]]] = {k: [] for k in CHAINS}
    for ci, s in enumerate(syms):
        df = minute_bars(db, s)                          # one coin in memory at a time
        c_at = pd.Series(df["c"].to_numpy(), index=df["ts"].to_numpy() + 60_000)    # close at the minute's end
        closes[:, ci] = c_at.reindex(grid, method="ffill").to_numpy()
        st = states_at(df, grid)
        for name, (_, rule) in CHAINS.items():
            with np.errstate(invalid="ignore"):
                f = np.nan_to_num(rule(st), nan=0).astype(bool)
            hits[name].extend((int(i), ci) for i in events(f))
    res: dict[str, Any] = {"coins": len(syms), "days": round(len(grid) / 96, 1), "chains": {}}
    fwd = {h: np.full_like(closes, np.nan) for h in HORIZONS}
    for h, k in HORIZONS.items():
        fwd[h][:-k] = closes[k:] / closes[:-k] - 1
    mkt = {h: np.nanmean(fwd[h], 1) for h in HORIZONS}
    for name, (side, _) in CHAINS.items():
        row: dict[str, Any] = {"side": "long" if side > 0 else "short"}
        for h in HORIZONS:
            ev = [(i, ci) for i, ci in hits[name] if not np.isnan(fwd[h][i, ci])]
            xs = [side * (fwd[h][i, ci] - mkt[h][i]) - COST for i, ci in ev]
            if len(xs) < 10:
                row[h] = {"n": len(xs)}
                continue
            inf = C.mean_inference(xs, [int(grid[i] // 86_400_000) for i, _ in ev], [ci for _, ci in ev])
            row[h] = {"n": len(xs), "coins": inf["coins"], "excess": round(inf["mean"], 4),
                      "t_2way": None if inf["t_2way"] is None else round(inf["t_2way"], 2),
                      "win": round(float(np.mean(np.array(xs) > 0)), 2), "median": round(float(np.median(xs)), 4)}
        res["chains"][name] = row
    res["decision"] = decide(res)
    return res


def decide(res: dict[str, Any]) -> dict[str, Any]:
    out = {}
    ch = res["chains"]
    for name in ch:
        r = ch[name].get("24h", {})
        ok = r.get("excess") is not None and r["excess"] > 0 and (r.get("t_2way") or 0) >= 2
        beats = all(r.get("excess", -9) >= ch[p].get("24h", {}).get("excess", 9) + 0.005 for p in PARENTS.get(name, ()))
        out[name] = {"positive_t2": ok, "beats_parents": beats if name in PARENTS else None,
                     "wire": ok and (beats if name in PARENTS else False)}
    return out


def rows(res: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for name, r in res["chains"].items():
        for h in HORIZONS:
            x = r.get(h, {})
            out.append({"name": f"{name}|{h}", "family": "mtf_chain", "params": {"chain": name, "horizon": h},
                        "n": x.get("n"), "coins": x.get("coins"), "mean": x.get("excess"), "t": x.get("t_2way"),
                        "t_kind": "2way_day_coin", "metric": "excess_ret"})
    return out


def main(argv: list[str] | None = None) -> None:
    from .. import provenance as P
    from .. import trial_register as TR
    ap = argparse.ArgumentParser(prog="shortbot.backtest.chain_study")
    ap.add_argument("--cache", required=True)
    ap.add_argument("--coins", type=int, default=180)
    ap.add_argument("--no-save", action="store_true")
    given = list(sys.argv[1:] if argv is None else argv)
    a = ap.parse_args(given)
    res = run(a.cache, a.coins)
    if not a.no_save:
        prov = P.stamp(P.config("shortbot.backtest.chain_study", given), {"replay_cache": P.fingerprint(Path(a.cache), ["bars"], file_hash=False)})
        print("saved to", out := P.write_json("chain_study", prov, res))
        print("trial register:", TR.record("chain_study", rows(res), prov, out), "rows")
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
