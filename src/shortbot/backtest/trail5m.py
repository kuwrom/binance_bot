"""Round-4 re-test of round 2's "trailing adds nothing" on 5-minute bars (docs/failures/ROUND4.md).

Round 2 Q2 judged breakeven / trailing on 1h bars, where the order of events inside a bar is unknown (the sim puts
the stop first and moves breakeven / trail only after a bar closes). Here the SAME signals are replayed on 5m bars:
- signals: the 1h fixed-exit book of pump_fade (1 ATR, 12h) and bounce_fade (-15%, 24h) as the runner takes them
  (one trade per coin at a time), kept only when 5m bars cover the whole exit window without a gap;
- exits per signal: (i) fixed, (ii) + breakeven 1R, (iii) + live trail (setups.py %, in R per trade), (iv) both,
  and bounce_fade's backtest "current" exits (breakeven 1R + a 2R/1R trail), exactly as strategies_v3.with_exits;
- the 5m replay enters at the close of the 5m bar that closes with the 1h signal bar (same price), the time stop is
  12 x as many bars (same clock time), slippage uses the 1h 24h volume, funding prints map to their 5m bar;
- judged with round 2's pre-registered rule on the paired delta vs fixed at the SAME resolution: on only if > 0 in
  both halves, off if < 0 OOS, neutral if |delta| < 0.02R in both halves.

    .venv/bin/python -m shortbot.backtest.trail5m --fetch [--out DIR]   # data/hist5m.db, then trail5m.md/.json

Writes trail5m.md, trail5m.json and provenance.json (hist.db + hist5m.db fingerprints) to DIR, default
<data dir>/research/<UTC date>-<sha7>/trail5m/; --no-save prints only."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .. import provenance as P
from . import hist5m
from .features import Coin, Market
from .sim import H, Plan, _funding_index, simulate, vol24
from .stats import cluster_t, day_of
from .strategies import WARMUP
from .strategies_v2 import BASE, variant
from .strategies_v3 import LIVE_TRAIL, PUMP_FAMILY, with_exits

FAMILIES = {"pump_fade": ("fixed", "be", "trail", "both"),
            "bounce_fade": ("fixed", "be", "trail", "both", "current")}
PER_H = H // hist5m.M5          # 12 five-minute bars per hour


def base_plans(m: Market, key: str) -> dict[tuple[str, int], tuple[Coin, Plan]]:
    base, checks, _ = PUMP_FAMILY[key]
    fn = variant(base, checks)
    return {(c.symbol, i): (c, pl) for c in m.coins.values() for i, pl in fn(m, c, BASE[base][1])}


def exit_plan(coin: Coin, i: int, pl: Plan, key: str, mode: str) -> Plan:
    return pl if mode == "current" else with_exits(pl, coin.rows[i]["c"], mode, LIVE_TRAIL[PUMP_FAMILY[key][2]])


def signal_set(m: Market, key: str) -> list[tuple[Coin, int, Plan]]:
    """The fixed-exit book as run.Runner takes it: one open trade per coin, censored windows dropped."""
    out, busy = [], {}
    for (sym, i), (c, pl) in sorted(base_plans(m, key).items()):
        fx = exit_plan(c, i, pl, key, "fixed")
        if i <= busy.get(sym, -1) or i + fx.time_stop >= len(c.rows):
            continue
        r = simulate(c.rows, i, fx, fidx=c.fidx)
        if r and r.get("filled"):
            busy[sym] = r["exit_i"]
            out.append((c, i, pl))
    return out


def needed(sigs: list[tuple[Coin, int, Plan]]) -> set[tuple[str, str]]:
    return {(hist5m.raw_of(c.symbol), d) for c, i, pl in sigs
            for d in hist5m.days_between(c.rows[i]["ts"], c.rows[i]["ts"] + (pl.time_stop + 2) * H)}


class Five:
    """One coin's cached 5m bars with a ts index and the funding index at 5m resolution."""

    def __init__(self, coin: Coin, db_path: str | Path):
        self.rows = hist5m.load(hist5m.raw_of(coin.symbol), db_path)
        self.pos = {r["ts"]: k for k, r in enumerate(self.rows)}
        self.fidx = _funding_index(self.rows, coin.funding)

    def start(self, coin: Coin, i: int, time_stop: int) -> int | None:
        """5m index of the bar closing with 1h bar i, if the exit window is gap-free."""
        k = self.pos.get(coin.rows[i]["ts"] + H - hist5m.M5)
        n = time_stop * PER_H + 1
        if k is None or k + n >= len(self.rows) or self.rows[k + n]["ts"] - self.rows[k]["ts"] != n * hist5m.M5:
            return None
        return k


def replay(m: Market, key: str, db_path: str | Path = hist5m.DB_PATH) -> list[dict[str, Any]]:
    """Per covered signal: {mode: (R at 1h, R at 5m, exit reason 1h, exit reason 5m)} + day / entry ts."""
    out, five = [], {}
    for c, i, pl in signal_set(m, key):
        f = five.setdefault(c.symbol, Five(c, db_path))
        k = f.start(c, i, pl.time_stop)
        if k is None:
            continue
        row = {"symbol": c.symbol, "signal_i": i, "day": day_of(c.rows[i]["ts"]), "entry_ts": c.rows[i]["ts"] + H}
        v24 = vol24(c.rows, i)
        for mode in FAMILIES[key]:
            p1 = exit_plan(c, i, pl, key, mode)
            a = simulate(c.rows, i, p1, fidx=c.fidx)
            b = simulate(f.rows, k, replace(p1, time_stop=p1.time_stop * PER_H), fidx=f.fidx, vol24_usd=v24)
            row[mode] = (a["r"], b["r"], a["reason"], b["reason"])
        out.append(row)
    return out


def _delta(rows: list[dict], mode: str, res: int, mid: int, ref: str = "fixed") -> dict[str, Any]:
    d = [{"r": x[mode][res] - x[ref][res], "day": x["day"], "ts": x["entry_ts"]} for x in rows]
    if len(d) < 3:
        return {"n": len(d)}
    h = lambda a: [x["r"] for x in d if (x["ts"] < mid) == a]  # noqa: E731
    m = lambda xs: round(statistics.fmean(xs), 3) if xs else None  # noqa: E731
    return {"n": len(d), "d": m([x["r"] for x in d]), "t": cluster_t(d, "day")[0], "is": m(h(True)), "oos": m(h(False))}


def rule(dl: dict[str, Any]) -> str:
    """Round 2's pre-registered Q2 rule."""
    i, o = dl.get("is"), dl.get("oos")
    if i is None or o is None:
        return "needs data"
    if abs(i) < 0.02 and abs(o) < 0.02:
        return "neutral"
    if o < 0:
        return "off"
    return "on" if i > 0 else "neutral/off"


def study(m: Market, db_path: str | Path = hist5m.DB_PATH) -> dict[str, Any]:
    mid = (m.t0 + WARMUP * H + m.t1) // 2
    out = {}
    for key in FAMILIES:
        rows = replay(m, key, db_path)
        fam = {"covered": len(rows), "signals": len(signal_set(m, key)), "modes": {}}
        for mode in FAMILIES[key]:
            one = {}
            for res, lab in ((0, "1h"), (1, "5m")):
                rs = [x[mode][res] for x in rows]
                reasons: dict[str, int] = {}
                for x in rows:
                    reasons[x[mode][2 + res]] = reasons.get(x[mode][2 + res], 0) + 1
                one[lab] = {"avg_r": round(statistics.fmean(rs), 3) if rs else None,
                            "delta": _delta(rows, mode, res, mid) if mode != "fixed" else None,
                            "exits": {k: round(v / len(rows) * 100) for k, v in sorted(reasons.items())}}
                if mode != "fixed":
                    one[lab]["rule"] = rule(one[lab]["delta"])
            fam["modes"][mode] = one
        out[key] = fam
    return out


def markdown(st: dict[str, Any]) -> str:
    f = lambda x: "–" if x is None else f"{x:+.3f}"  # noqa: E731
    L = ["# Trailing / breakeven on 5-minute bars vs 1h (same signals)", "",
         "| family | exits | avg R 1h | avg R 5m | Δ vs fixed 1h (t) IS/OOS | rule 1h | Δ vs fixed 5m (t) IS/OOS | "
         "rule 5m | exits 5m % |", "|" + "---|" * 9]
    for key, fam in st.items():
        L.append(f"| {key} | covered {fam['covered']} of {fam['signals']} signals | | | | | | | |")
        for mode, v in fam["modes"].items():
            dd = lambda d: "–" if not d or d.get("d") is None else (  # noqa: E731
                f"{f(d['d'])} (t {d.get('t')}) {f(d.get('is'))}/{f(d.get('oos'))}")
            L.append(f"| {key} | {mode} | {f(v['1h']['avg_r'])} | {f(v['5m']['avg_r'])} | {dd(v['1h']['delta'])} | "
                     f"{v['1h'].get('rule', '')} | {dd(v['5m']['delta'])} | {v['5m'].get('rule', '')} | "
                     f"{' '.join(f'{k} {n}' for k, n in v['5m']['exits'].items())} |")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> None:
    from .run import load_coins
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="download the missing 5m files first")
    ap.add_argument("--db", type=Path, help="5m cache (default: <data dir>/hist5m.db)")
    ap.add_argument("--hist", type=Path, help="1h history (default: <data dir>/hist.db)")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--no-save", action="store_true", help="print the markdown only")
    given = list(sys.argv[1:] if argv is None else argv)
    a = ap.parse_args(given)
    a.db, a.hist = a.db or P.data_dir() / "hist5m.db", a.hist or P.data_dir() / "hist.db"
    m = Market(load_coins(True, a.hist))
    if a.fetch:
        pairs = set().union(*(needed(signal_set(m, k)) for k in FAMILIES))
        print(json.dumps(asyncio.run(hist5m.fetch(pairs, a.db))))
    inputs = {"hist_db": P.fingerprint(a.hist), "hist5m_db": P.fingerprint(a.db)}
    st = study(m, a.db)
    md = markdown(st)
    prov = out = None
    if not a.no_save:
        given = P.ensure_arg(P.ensure_arg([x for x in given if x != "--fetch"], "--db", a.db), "--hist", a.hist)
        prov = P.stamp(P.config("shortbot.backtest.trail5m", given), inputs,
                       window={"t0_ms": m.t0, "t1_ms": m.t1, "coins": len(m.coins)})
        md += "\n\n" + P.footer(prov) + "\n"
        out = P.save(a.out or P.run_dir("trail5m", prov["git"]), prov, {
            "trail5m.md": md, "trail5m.json": json.dumps({**st, "provenance": prov}, indent=1, default=str)})
        md += f"\nsaved to {out}"
    from .. import trial_register as TR
    from .register_rows import trail5m_rows
    print(md + f"\ntrial register: {TR.record('trail5m', trail5m_rows(st), prov, out)} rows")

if __name__ == "__main__":
    main()
