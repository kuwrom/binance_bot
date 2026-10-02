"""Run every pre-registered trial (strategies.trials) plus controls, then build the report.

    .venv/bin/python -m shortbot.backtest.run                 # 10-day cache (data/shortbot.db study_coins)
    .venv/bin/python -m shortbot.backtest.run --days60        # 60-day history (shortbot.histdata.load60)
    .venv/bin/python -m shortbot.backtest.run --days60 --round1   # + round-1 variants (strategies_v2)
    .venv/bin/python -m shortbot.backtest.run --days60 --long     # the long sleeve only (strategies_long, research)
    .venv/bin/python -m shortbot.backtest.run --days60 --round2   # + round-2 variants (strategies_v3, report_v3)
    .venv/bin/python -m shortbot.backtest.run --days60 --round3   # + round-3 veto tests (strategies_v4, report_v4)
    ... --round3 --round4      # + round-4 corrections table (report_v5); defaults --placebo=matched --dsr=clustered
    ... --placebo=legacy --dsr=legacy   # reproduces rounds 0-3 exactly
    ... --db PATH  (input; default <data dir>/hist.db with --days60, else the journal's study_coins)
    ... --t0 2026-07-27 --t1 2026-09-29T00:00   # pin the window (epoch ms or UTC), e.g. to rerun on a grown hist.db
    ... --out DIR  (default <data dir>/research/<UTC date>-<sha7>/<bt60_r3...>/; --no-save prints only)

Writes report.md, results.json, trades.json and provenance.json: git sha + dirty flag, code hash, the input's
fingerprint, window, seeds, every flag and the ledger (provenance.py; `python -m shortbot.provenance verify DIR`).
Read-only: loads cached market data, never touches the exchange or the journal's tables."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import types
import zlib
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import provenance as P
from .features import Market, features
from .sim import COSTS, FREE, Costs, simulate

STRESS = Costs(taker_bps=7.5, maker_bps=3.0, slip_k_bps=40.0, stop_overshoot=0.1)   # 1.5x fees, 2x slippage
from .stats import day_of
from .strategies import Trial, trials
from .placebo import matched_signals
from .strategies_more import RANDOM, placebo_signals

H = 3_600_000
OPEN_KEYED = ("premium",)                                          # keyed by the bar's open time, like the bars
END_KEYED = ("oi", "ls_top", "ls_top_acct", "ls_global", "taker_ratio")   # keyed by the hour's END


def source(days60: bool, db: Path | None = None) -> tuple[Path, list[str] | None]:
    """The input database and the tables read (the 10-day cache is one table of the live journal)."""
    if days60:
        return Path(db or P.data_dir() / "hist.db"), None
    return Path(db or P.desk_db()), ["study_coins"]


def load_coins(days60: bool = False, db: Path | None = None) -> list[dict[str, Any]]:
    path, _ = source(days60, db)
    if days60:
        from .. import histdata
        return histdata.load60(path)
    from ..patterns_data import load
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        return load(types.SimpleNamespace(db=conn))
    finally:
        conn.close()


def _ms(s: str) -> int:
    """Epoch ms, or a UTC date / date-time (2026-07-27, 2026-07-27T05:00)."""
    if s.isdigit():
        return int(s)
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def window(raw: list[dict[str, Any]], t0: int | None, t1: int | None) -> list[dict[str, Any]]:
    """Every series cut to [t0, t1]: bars by open time, hour-END keyed series and funding prints up to t1 + 1h. hist.db
    only grows (histdata.build never drops old rows), so this replays a recorded window on a later build; prints
    after the last bar never enter a trade (censored exits are dropped)."""
    if t0 is None and t1 is None:
        return raw
    lo, hi = t0 if t0 is not None else float("-inf"), t1 if t1 is not None else float("inf")
    out = []
    for c in raw:
        d = dict(c)
        for k in ("perp", "spot"):
            d[k] = [r for r in c.get(k) or [] if lo <= r["ts"] <= hi]
        d["funding"] = [f for f in c.get("funding") or [] if lo <= f[0] <= hi + H]
        for keys, top in ((OPEN_KEYED, hi), (END_KEYED, hi + H)):
            for k in keys:
                if isinstance(c.get(k), dict):
                    d[k] = {t: v for t, v in c[k].items() if lo <= int(t) <= top}
        out.append(d)
    return out


def placebo_seed(name: str) -> int:
    return zlib.crc32(name.encode()) % 10_000


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
            seed = placebo_seed(t.name)
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
    ap.add_argument("--db", type=Path, help="input DB (default: <data dir>/hist.db with --days60, else the journal)")
    ap.add_argument("--t0", type=_ms, help="window start: epoch ms or UTC date[Thh:mm] (pins a rerun)")
    ap.add_argument("--t1", type=_ms, help="window end (last bar's open time), as --t0")
    ap.add_argument("--out", type=Path, help="default: <data dir>/research/<UTC date>-<sha7>[-dirty]/<label>/")
    ap.add_argument("--no-save", action="store_true", help="print the markdown only")
    ap.add_argument("--round1", action="store_true", help="ledger = round 0 + strategies_v2 (docs/FAILURES.md)")
    ap.add_argument("--round2", action="store_true", help="ledger = rounds 0-1 + strategies_v3 (docs/FAILURES.md)")
    ap.add_argument("--round3", action="store_true", help="ledger = rounds 0-2 + strategies_v4 (docs/FAILURES.md)")
    ap.add_argument("--placebo", choices=("legacy", "matched"), default="matched",
                    help="same-hour placebo: legacy (rounds 0-3) or matched (round 4, docs/failures/ROUND4.md)")
    ap.add_argument("--dsr", choices=("legacy", "clustered"), default="clustered",
                    help="Deflated Sharpe: legacy (raw N, all-trial V[SR]) or clustered (round 4 effective N)")
    ap.add_argument("--round4", action="store_true", help="add the round-4 corrections section (report_v5)")
    ap.add_argument("--compare", type=Path, help="--round4: an earlier results.json to diff every verdict against")
    ap.add_argument("--long", action="store_true", help="ledger = the long sleeve (strategies_long; research)")
    given = list(sys.argv[1:] if argv is None else argv)
    a = ap.parse_args(given)
    path, tables = source(a.days60, a.db)
    fp = P.fingerprint(path, tables, file_hash=tables is None)
    raw = window(load_coins(a.days60, path), a.t0, a.t1)
    if P.fingerprint(path, tables, file_hash=False)["id"] != fp["id"]:
        fp["changed_during_load"] = True                         # e.g. a rebuild ran meanwhile: rerun this
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
    if a.long:
        from .strategies_long import trials_long
        led = trials_long()
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
    if a.no_save:
        print(md)
        return
    given = P.ensure_arg(given, "--db", path)
    inputs = {"hist_db" if a.days60 else "journal_study_coins": fp}
    if a.compare:
        inputs["compare"] = P.file_digest(a.compare)
    ledger = led if led is not None else trials()
    cfg = P.config("shortbot.backtest.run", given, pin=["--t0", str(m.t0), "--t1", str(m.t1)],
                   args={k: v for k, v in vars(a).items() if k not in ("out", "no_save")},
                   ledger=[{"name": t.name, "strategy": t.strategy, "params": t.params, "grid_idx": t.grid_idx,
                            "notes": t.notes} for t in ledger],
                   costs=asdict(COSTS), stress=asdict(STRESS), placebo_per=3)
    seeds = {"placebo": "run.placebo_seed(trial name) = zlib.crc32(name) % 10000, for both placebo draws",
             "per_trial": {n: placebo_seed(n) for n, r in res["trials"].items() if r["trades"]},
             "random_control": RANDOM.params.get("seed", 5)}
    prov = P.stamp(cfg, inputs, window=_window(m, rep, a), seeds=seeds)
    md += "\n\n" + P.footer(prov) + "\n"
    trades = [{k: v for k, v in t.items() if k != "meta"} for r in res["trials"].values() for t in r["trades"]]
    out = P.save(a.out or P.run_dir(label(a), prov["git"]), prov, {
        "report.md": md, "results.json": json.dumps({**rep, "provenance": prov}, indent=1, default=str),
        "trades.json": json.dumps(trades, default=str)})
    print(md)
    print(f"saved to {out}")


def label(a: argparse.Namespace) -> str:
    """bt60_r3, bt10, bt60_long, bt60_r4_legacy...: the default output dir's name."""
    rnd = next((f"_r{k}" for k in (4, 3, 2, 1) if getattr(a, f"round{k}")), "")
    return (f"bt{60 if a.days60 else 10}{rnd}{'_long' if a.long else ''}"
            f"{'_legacy' if 'legacy' in (a.placebo, a.dsr) else ''}")


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="minutes")


def _window(m: Market, rep: dict[str, Any], a: argparse.Namespace) -> dict[str, Any]:
    return {"t0_ms": m.t0, "t1_ms": m.t1, "t0": _iso(m.t0), "t1": _iso(m.t1), "mid_ms": rep.get("mid"),
            "coins": len(m.coins), "symbols_sha": hashlib.sha256(",".join(sorted(m.coins)).encode()).hexdigest()[:16],
            "asked": {"t0": a.t0, "t1": a.t1}}

if __name__ == "__main__":
    main()
