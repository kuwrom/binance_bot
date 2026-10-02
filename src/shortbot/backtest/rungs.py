"""Resting reduce-only take-profit rungs below a short (proposal P1a, docs/PROPOSALS.md; task #24). Research only.

Question: does resting extra buy limits deep below entry (they fill on last-price wicks, which the guardian's
60 s mark-price check can miss) improve R, and at which depths and sizes? Fixed before the first run:

- signals: every unique (coin, bar) signal of the 60-day ledger (strategies.trials(), first plan seen), plus the
  random-entry control (strategies_more.RANDOM), so a generic effect can be told apart from one tied to the signals.
  A second, long sample: 2020-2026 daily top-gainer fades (histdaily): short at the close of a >= +20% day, stop
  +25%, 7-day time stop.
- baselines:
  - "targets": the plan as backtested (resting targets at 1R / 2R ...);
  - "runner": the same plan with no targets (live plans whose targets only alert): breakeven, trail and time
    stop only.
- variants: rungs at entry x (1 - d) for depth sets (4, 8, 12), (6, 12, 20), (10, 20, 30) %, with total sizes
  30 / 50 / 70% of the position split 30/30/40 across the three rungs.
  - On "targets" the rung share is carved out of the plan's targets (scaled by 1 - total), so it is the part
    left to run after them.
  - On "runner" the rungs are simply added.
- metric: R per signal (fees, slippage, funding as sim.simulate). The uplift is paired against the same signal's
  baseline, with a day-clustered t. Also the share of trades where any rung filled, and the uplift on those trades.
  In one bar a stop is assumed to come before a rung, which is conservative.

    .venv/bin/python -m shortbot.backtest.rungs [--daily]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .. import provenance as P
from ..cluster_se import one_way_t
from .sim import COSTS, Plan, simulate
from .stats import day_of

DEPTHS = ((0.04, 0.08, 0.12), (0.06, 0.12, 0.20), (0.10, 0.20, 0.30))
SIZES = (0.30, 0.50, 0.70)
SPLIT = (0.3, 0.3, 0.4)


def with_rungs(plan: Plan, entry: float, depths: tuple[float, ...], total: float, carve: bool) -> Plan:
    rungs = tuple((entry * (1 - d), total * s) for d, s in zip(depths, SPLIT))
    base = tuple((px, f * (1 - total)) for px, f in plan.targets) if carve else ()
    return replace(plan, targets=base + rungs)


def variants(plan: Plan, entry: float) -> dict[str, Plan]:
    out = {"targets": plan, "runner": replace(plan, targets=())}
    for d in DEPTHS:
        for t in SIZES:
            tag = f"d{int(d[0] * 100)}-{int(d[1] * 100)}-{int(d[2] * 100)}_s{int(t * 100)}"
            out[f"targets+{tag}"] = with_rungs(plan, entry, d, t, carve=True)
            out[f"runner+{tag}"] = with_rungs(plan, entry, d, t, carve=False)
    return out


def _t_day(diffs: list[tuple[str, float]]) -> float | None:
    """t of the mean uplift, clustered by entry day (#31: CV3 on the pooled mean, cluster_se; before #31 a t over
    equal-weighted day means). None below 3 days."""
    t, g = one_way_t([x for _, x in diffs], [d for d, _ in diffs])
    return round(t, 2) if t is not None and g >= 3 else None


def evaluate(results: list[dict[str, Any]]) -> dict[str, Any]:
    """results: one row per signal {"day", "group", "r": {variant: R}, "fill": {variant: bool}}."""
    out: dict[str, Any] = {}
    for group in sorted({r["group"] for r in results}):
        rows = [r for r in results if r["group"] == group]
        g: dict[str, Any] = {"n": len(rows)}
        for base in ("targets", "runner"):
            g[base] = round(statistics.fmean(r["r"][base] for r in rows), 4)
            for v in rows[0]["r"]:
                if not v.startswith(base + "+"):
                    continue
                diffs = [(r["day"], r["r"][v] - r["r"][base]) for r in rows]
                filled = [r for r in rows if r["fill"][v]]
                g[v] = {"avg_r": round(statistics.fmean(r["r"][v] for r in rows), 4),
                        "uplift": round(statistics.fmean(d for _, d in diffs), 4), "t_day": _t_day(diffs),
                        "fill_pct": round(len(filled) / len(rows) * 100, 1),
                        "uplift_when_filled": round(statistics.fmean(r["r"][v] - r["r"][base] for r in filled), 3)
                        if filled else None}
        out[group] = g
    return out


def _sim_all(rows: list[dict], i: int, plan: Plan, entry: float, fidx: dict | None, costs=COSTS) -> dict | None:
    r: dict[str, float] = {}
    fill: dict[str, bool] = {}
    for name, pl in variants(plan, entry).items():
        res = simulate(rows, i, pl, costs=costs, fidx=fidx)
        if not res or not res.get("filled"):
            return None
        r[name] = res["r"]
        rung_px = [px for px, _ in pl.targets[-len(SPLIT):]] if "+" in name else []   # with_rungs appends them
        lowest = res["entry_px"] * (1 - res["mfe_pct"] / 100)          # lowest low while the trade was open
        fill[name] = bool(rung_px) and lowest <= max(rung_px)
    return {"r": r, "fill": fill}


def run_hourly() -> dict[str, Any]:
    from .features import Market
    from .run import load_coins
    from .strategies import trials
    from .strategies_more import RANDOM
    m = Market(load_coins(True))
    seen: set[tuple[str, int]] = set()
    results = []
    for t in [*trials(), RANDOM]:
        sig = list(t.fn(m, t.params)) if t.cross else [(c, i, pl) for c in m.coins.values()
                                                       for i, pl in t.fn(m, c, t.params)]
        group = "random" if t.control else "strategies"
        for coin, i, pl in sig:
            key = (group, coin.symbol, i)
            if key in seen or pl.entry != "market" or i + pl.time_stop + 1 >= len(coin.rows):
                continue
            seen.add(key)
            res = _sim_all(coin.rows, i, pl, coin.rows[i]["c"], coin.fidx)
            if res:
                results.append({"day": day_of(coin.rows[i]["ts"]), "group": group, **res})
    return {"sample": "60d hourly ledger", "eval": evaluate(results)}


def run_daily(db: Path) -> dict[str, Any]:
    from .carry_regime import STABLES, daily_funding
    from ..histdaily import load
    data = load(str(db))
    results = []
    for s, bars in data["bars"].items():
        if s in STABLES or s in ("BTCUSDT", "ETHUSDT") or len(bars) < 40:
            continue
        rows = [{"ts": b[0], "o": b[1], "h": b[2], "l": b[3], "c": b[4], "qv": b[5] / 24} for b in bars]
        df = daily_funding(data["funding"].get(s, []))
        fidx = {k: df.get(r["ts"], 0.0) for k, r in enumerate(rows)}
        for i in range(30, len(rows) - 8):
            r0, r1 = rows[i - 1]["c"], rows[i]["c"]
            if r1 / r0 - 1 < 0.20 or bars[i][5] < 5e6:
                continue
            entry = r1
            plan = Plan(stop=entry * 1.25, targets=((entry * 0.90, 0.5), (entry * 0.80, 0.5)), time_stop=7,
                        be_r=None)
            res = _sim_all(rows, i, plan, entry, fidx)
            if res:
                results.append({"day": day_of(rows[i]["ts"]), "group": "daily_gainer_fade", **res})
    return {"sample": "2020-2026 daily top-gainer fades", "eval": evaluate(results)}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="shortbot.backtest.rungs")
    ap.add_argument("--daily", action="store_true", help="the 2020-2026 daily sample instead of the 60-day ledger")
    ap.add_argument("--db", type=Path, help="daily: default <data dir>/daily.db")
    ap.add_argument("--no-save", action="store_true")
    given = list(sys.argv[1:] if argv is None else argv)
    a = ap.parse_args(given)
    if a.daily:
        db = a.db or P.data_dir() / "daily.db"
        res, fp = run_daily(db), {"daily_db": P.fingerprint(db)}
    else:
        res, fp = run_hourly(), {}
    prov = out = None
    if not a.no_save:
        prov = P.stamp(P.config("shortbot.backtest.rungs", given), fp)
        print("saved to", out := P.write_json("rungs_daily" if a.daily else "rungs", prov, res, None))
    print(json.dumps(res, indent=1))
    from .register_rows import rungs_rows
    from .. import trial_register as TR
    study = "rungs_daily" if a.daily else "rungs"
    print(f"trial register: {TR.record(study, rungs_rows(res, study), prov, out)} rows")


if __name__ == "__main__":
    main()
