"""Run every pre-registered trial (strategies.trials) plus controls, then build the report.

    .venv/bin/python -m shortbot.backtest.run                 # 10-day cache (data/shortbot.db study_coins)
    .venv/bin/python -m shortbot.backtest.run --days60        # 60-day history (shortbot.histdata.load60)
    .venv/bin/python -m shortbot.backtest.run --days60 --round1   # + round-1 variants (strategies_v2)
    .venv/bin/python -m shortbot.backtest.run --days60 --round2   # + round-2 variants (strategies_v3, report_v3)
    .venv/bin/python -m shortbot.backtest.run --days60 --round3   # + round-3 veto tests (strategies_v4, report_v4)
    ... --round3 --round4      # + round-4 corrections table (report_v5); defaults --placebo=matched --dsr=clustered
    ... --placebo=legacy --dsr=legacy   # reproduces rounds 0-3 exactly
    ... --out DIR  (writes results.json + report.md; default: print the markdown only)

Read-only: loads cached market data, never touches the exchange or the journal's tables."""

from __future__ import annotations

import argparse
import json
import sqlite3
import types
import zlib
from pathlib import Path
from typing import Any

from .features import Market, features
from .sim import COSTS, FREE, Costs, simulate

STRESS = Costs(taker_bps=7.5, maker_bps=3.0, slip_k_bps=40.0, stop_overshoot=0.1)   # 1.5x fees, 2x slippage
from .stats import day_of
from .strategies import Trial, trials
from .placebo import matched_signals
from .strategies_more import RANDOM, placebo_signals

ROOT = Path(__file__).resolve().parents[3]


def load_coins(days60: bool = False, db: Path | None = None) -> list[dict[str, Any]]:
    if days60:
        from .. import histdata
        try:
            return histdata.load60()
        except TypeError:
            j = types.SimpleNamespace(db=sqlite3.connect(str(ROOT / "data" / "hist.db"), timeout=30))
            return histdata.load60(j)
    from ..patterns_data import load
    conn = sqlite3.connect(str(db or ROOT / "data" / "shortbot.db"), timeout=30)
    try:
        return load(types.SimpleNamespace(db=conn))
    finally:
        conn.close()


class Runner:
    def __init__(self, m: Market, costs: Costs = COSTS):
        self.m, self.costs = m, costs
        self._feat: dict[tuple[str, int], dict] = {}

    def feat(self, coin, i: int) -> dict:
        k = (coin.symbol, i)
        if k not in self._feat:
            self._feat[k] = features(self.m, coin, i)
        return self._feat[k]

    def signals(self, t: Trial) -> list:
        if t.cross:
            sig = list(t.fn(self.m, t.params))
        else:
            sig = [(c, i, pl) for c in self.m.coins.values() for i, pl in t.fn(self.m, c, t.params)]
        return sorted(sig, key=lambda x: (x[0].symbol, x[1]))

    def execute(self, name: str, sig: list, strategy: str, exclusive: bool = True) -> dict[str, Any]:
        """Simulate signals; one open trade per coin at a time (exclusive=False: every signal on its own, for the
        matched placebos, which are per-donor controls, not a book); drop entries whose exit window runs past the
        data."""
        trades, busy, unfilled, censored = [], {}, 0, 0
        for coin, i, pl in sig:
            if exclusive and i <= busy.get(coin.symbol, -1):
                continue
            if i + pl.time_stop + (pl.limit_ttl if pl.entry != "market" else 0) >= len(coin.rows):
                censored += 1
                continue
            res = simulate(coin.rows, i, pl, costs=self.costs, fidx=coin.fidx)
            if not res:
                continue
            if not res["filled"]:
                unfilled += 1
                continue
            gross = simulate(coin.rows, i, pl, costs=FREE, fidx=coin.fidx)
            busy[coin.symbol] = res["exit_i"]
            trades.append({**res, "symbol": coin.symbol, "trial": name, "strategy": strategy,
                           "r_gross": gross["r"] if gross and gross.get("filled") else res["r"],
                           "day": day_of(res["entry_ts"]), "exit_ts": coin.rows[res["exit_i"]]["ts"],
                           "time_stop": pl.time_stop, "be_r": pl.be_r, "trail_act_r": pl.trail_act_r,
                           "trail_dist_r": pl.trail_dist_r, "entry_kind": pl.entry, "meta": pl.meta,
                           "fund_budget_r": pl.fund_budget_r, "fund_follow_r": pl.fund_follow_r,
                           "f": self.feat(coin, i)})
        return {"trades": trades, "signals": len(sig), "unfilled": unfilled, "censored": censored}


def run_all(m: Market, costs: Costs = COSTS, placebo_per: int = 3, controls: bool = True,
            ledger: list[Trial] | None = None, placebo: str = "legacy") -> dict[str, Any]:
    """placebo "legacy": strategies_more.placebo_signals (rounds 0-3); "matched": placebo.matched_signals (round 4:
    volatility / liquidity-matched same-hour draw at the fill bar, budget-exit parity)."""
    run = Runner(m, costs)
    draw = matched_signals if placebo == "matched" else placebo_signals
    out: dict[str, Any] = {"trials": {}, "placebo": {}, "placebo_time": {}, "controls": {}, "placebo_mode": placebo}
    for t in ledger if ledger is not None else trials():
        res = run.execute(t.name, run.signals(t), t.strategy)
        res.update(strategy=t.strategy, params=t.params, grid_idx=t.grid_idx, notes=t.notes)
        if "placebo@fill" in t.notes:        # round 2 entry types: the same-hour placebo enters at the FILL bar
            for tr in res["trades"]:
                tr["arm_i"], tr["signal_i"] = tr["signal_i"], tr["entry_i"]
        out["trials"][t.name] = res
        if res["trades"] and controls:
            seed = zlib.crc32(t.name.encode()) % 10_000
            for key, mode in (("placebo", "coin"), ("placebo_time", "time")):
                ps = draw(m, res["trades"], per=placebo_per, seed=seed, mode=mode)
                out[key][t.name] = run.execute(f"{key}:{t.name}", sorted(ps, key=lambda x: (x[0].symbol, x[1])), key,
                                               exclusive=placebo != "matched")
    if controls:
        out["controls"]["random"] = run.execute("random", run.signals(RANDOM), "random")
    out["window"] = {"t0": day_of(m.t0), "t1": day_of(m.t1), "coins": len(m.coins),
                     "has_ls": any(c.extra.get("ls_top") for c in m.coins.values()),
                     "has_delist": any(c.delist_ms for c in m.coins.values())}
    return out


def main(argv: list[str] | None = None) -> None:
    from .report import build, markdown
    ap = argparse.ArgumentParser()
    ap.add_argument("--days60", action="store_true")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--round1", action="store_true", help="ledger = round 0 + strategies_v2 (docs/FAILURES.md)")
    ap.add_argument("--round2", action="store_true", help="ledger = rounds 0-1 + strategies_v3 (docs/FAILURES.md)")
    ap.add_argument("--round3", action="store_true", help="ledger = rounds 0-2 + strategies_v4 (docs/FAILURES.md)")
    ap.add_argument("--placebo", choices=("legacy", "matched"), default="matched",
                    help="same-hour placebo: legacy (rounds 0-3) or matched (round 4, docs/failures/ROUND4.md)")
    ap.add_argument("--dsr", choices=("legacy", "clustered"), default="clustered",
                    help="Deflated Sharpe: legacy (raw N, all-trial V[SR]) or clustered (round 4 effective N)")
    ap.add_argument("--round4", action="store_true", help="add the round-4 corrections section (report_v5)")
    ap.add_argument("--compare", type=Path, help="--round4: an earlier results.json to diff every verdict against")
    a = ap.parse_args(argv)
    raw = load_coins(a.days60)
    m = Market(raw)
    led = None
    if a.round1:
        from .strategies_v2 import ledger
        led = ledger()
    if a.round2:
        from .strategies_v3 import ledger as ledger3
        led = ledger3()
    if a.round3:
        from .strategies_v4 import attach_spot
        from .strategies_v4 import ledger as ledger4
        attach_spot(m, raw)
        led = ledger4()
    res = run_all(m, ledger=led, placebo=a.placebo)
    rep = build(res, m, stress=run_all(m, STRESS, controls=False, ledger=led), dsr_mode=a.dsr)
    md = markdown(rep)
    if a.round2:
        from .report_v3 import round2_markdown
        md = round2_markdown(rep, res, m) + "\n\n" + md
    if a.round3:
        from .report_v4 import round3_markdown
        md = round3_markdown(rep, res, m) + "\n\n" + md
    if a.round4:
        from .report_v5 import round4_markdown
        md = round4_markdown(rep, res, m, a.compare) + "\n\n" + md
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "report.md").write_text(md)
        slim = {k: v for k, v in rep.items()}
        (a.out / "results.json").write_text(json.dumps(slim, indent=1, default=str))
        trades = [{k: v for k, v in t.items() if k != "meta"} for r in res["trials"].values() for t in r["trades"]]
        (a.out / "trades.json").write_text(json.dumps(trades, default=str))
    print(md)


if __name__ == "__main__":
    main()
