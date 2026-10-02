"""Long-side research battery (task #32): which long strategies could the desk follow? Daily Binance USDⓈ-M perps,
2020-2026 (histdaily). Research only.

Fixed before the first run (every variant reported is a trial in the register, study long_studies):
- Universe: point-in-time top 50 by 30-day quote volume (xs_factors.Panel: >= $5M/day, listed >= 60 days, no stables).
- Costs: 10 bp per round trip (taker 5 bp a side), plus the funding the long pays.
- Excess = the trade's net return minus the equal-weight universe's return over the same days (a beta-1 benchmark:
  what being long anything returned).
- Inference: pooled mean, t clustered two ways (cluster_se.mean_inference): by entry time block (block = the hold,
  at least 7 days, so overlapping holds share a cluster) and by coin.
- Primary gate for the splits: BTC's 60-day return > 0 at the entry close. Splits are reported, never selected on.
- Tail diagnostics (long_stats.py, added after the first run showed both edges live in the tail; same variants, no
  new trials): median, 5%-trimmed mean, the top 5%'s share of the total, a 4-week block bootstrap of the mean.

L1 tsmom gate (card tsmom_trend_following). Gate on at close d when BTC's 20d / 60d / 120d return > 0, or BTC closes
   above its 200DMA (4 rules). Timed long BTC and timed long equal-weight top-30 basket, both gated by BTC; flat when
   off; 5 bp per switch. Versus buy-and-hold: mean daily return, t, Sharpe, max drawdown, exposure. Diagnostics (same
   rules, not extra trials): next-day return and volatility on gate-on vs gate-off days, and the basket's forward
   7-day return in gate-on vs gate-off weeks (Sunday closes).
L2 breakout_20d (card breakout_20d_overlay). The close exceeds the highest high of the previous 20 days, the coin's
   first such close in 20 days -> long at that close. Exits: hold 5 / 10 / 20 days, or a chandelier stop (a close
   below the highest close since entry minus 3x ATR(14)), max 20 days. 4 variants.
L3 residual momentum (cards beta_hedged_rs_pair and dominance_rotation's finding). Sunday closes, top 30 without BTC.
   Rank by the 7 / 14 / 28-day return residual to BTC (beta from 60 daily returns); long the top 5 for 7 days. Excess
   versus the 30. 3 variants.
L4 alt negative funding (card funding_negative_zscore_long, extended to alts). A coin's daily funding z-score
   (30 days) <= -1.5 -> long until z > 0 or 30 days, non-overlapping per coin. 1 variant.
L5 capitulation (card capitulation_forced_selling_long). A coin's 7-day return <= -30% and at least 20 pp below the
   universe's -> long; hold 3 / 7 / 14 days; first in 14 days per coin. 3 variants.
Trials: 8 (L1: 4 rules x 2 assets) + 4 + 3 + 1 + 3 = 19.

    .venv/bin/python -m shortbot.backtest.long_studies [--db data/daily.db] [--no-save]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import provenance as P
from .carry_regime import DAY
from .long_stats import _series_stats, _two_groups, summarise
from .xs_factors import Panel

COST, SWITCH = 0.001, 0.0005
TOP, BASKET = 50, 30
BTC = "BTCUSDT"
GATES = {"r20": 20, "r60": 60, "r120": 120, "sma200": 200}
PRIMARY_GATE = "r60"


class Study:
    """Shared, cached views of the panel: universes, the benchmark, BTC gates."""

    def __init__(self, p: Panel):
        self.p = p
        self._uni: dict[int, list[str]] = {}
        self._bench: dict[tuple[int, int], float | None] = {}
        self.btc = {d: b[4] for d, b in p.bars[BTC].items()}

    def uni(self, d: int, top: int = TOP) -> list[str]:
        if d not in self._uni:
            self._uni[d] = self.p.universe(d, TOP)
        return self._uni[d][:top]

    def net(self, s: str, d: int, h: int, cost: float = COST) -> float | None:
        r = self.p.ret(s, d, h)
        return None if r is None else r - self.p.funding(s, d, h) - cost

    def bench(self, d: int, h: int, names: list[str] | None = None) -> float | None:
        key = (d, h)
        if names is None and key in self._bench:
            return self._bench[key]
        rs = [x for s in (names or self.uni(d)) if (x := self.net(s, d, h, 0.0)) is not None]
        out = statistics.fmean(rs) if rs else None
        if names is None:
            self._bench[key] = out
        return out

    def gate(self, rule: str, d: int) -> bool | None:
        n = GATES[rule]
        past = [self.btc.get(d - k * DAY) for k in range(n + 1)] if rule == "sma200" else [self.btc.get(d - n * DAY)]
        if any(x is None for x in past) or d not in self.btc:
            return None
        return self.btc[d] > (statistics.fmean(past[:n]) if rule == "sma200" else past[0])


def tsmom(st: Study) -> dict[str, Any]:
    days = sorted(st.btc)
    out: dict[str, Any] = {}
    basket = {}
    for d in days[:-1]:
        b = st.bench(d, 1, st.uni(d, BASKET)) if st.uni(d, BASKET) else None
        if b is not None:
            basket[d] = b
    btc_ret = {d: st.net(BTC, d, 1, 0.0) for d in days[:-1]}
    for rule in GATES:
        g = {d: st.gate(rule, d) for d in days}
        for asset, series in (("btc", btc_ret), ("basket", basket)):
            timed, hold, prev, split = [], [], False, {True: [], False: []}
            for d in days[:-1]:
                if g[d] is None or series.get(d) is None:
                    continue
                r = series[d] if g[d] else 0.0
                timed.append(r - (SWITCH if g[d] != prev else 0.0))
                hold.append(series[d])
                split[g[d]].append(series[d])
                prev = g[d]
            if len(timed) < 3:
                out[f"{asset}|{rule}"] = {"timed": {"days": len(timed)}}
                continue
            out[f"{asset}|{rule}"] = {"timed": _series_stats(timed, len(split[True]) / len(timed)),
                                      "buy_hold": _series_stats(hold),
                                      "next_day_on_vs_off": _two_groups(split[True], split[False]),
                                      "vol_on_vs_off": [round(statistics.stdev(split[k]), 4) if len(split[k]) > 1 else None
                                                        for k in (True, False)]}
        weeks_on, weeks_off = [], []
        for d in days:
            if datetime.fromtimestamp(d / 1000, UTC).weekday() == 6 and g[d] is not None:
                f = st.bench(d, 7, st.uni(d, BASKET)) if st.uni(d, BASKET) else None
                if f is not None:
                    (weeks_on if g[d] else weeks_off).append(f)
        out[f"weekly_basket|{rule}"] = _two_groups(weeks_on, weeks_off)
    return out


def _event(st: Study, s: str, d: int, net: float | None, h: int) -> dict[str, Any] | None:
    b = st.bench(d, h)
    if net is None or b is None:
        return None
    return {"d": d, "s": s, "net": net, "excess": net - b, "gate": st.gate(PRIMARY_GATE, d)}


def _atr(bars: dict[int, tuple], d: int, n: int = 14) -> float | None:
    trs = []
    for k in range(n):
        b, prev = bars.get(d - k * DAY), bars.get(d - (k + 1) * DAY)
        if b is None or prev is None:
            return None
        trs.append(max(b[2] - b[3], abs(b[2] - prev[4]), abs(b[3] - prev[4])))
    return statistics.fmean(trs)


def chandelier(st: Study, s: str, d: int, mult: float = 3.0, max_h: int = 20) -> tuple[float, int] | None:
    """Long from close d; exit at the first close below the highest close since entry - mult x ATR(14), or max_h."""
    bars = st.p.bars[s]
    entry, hi = bars[d][4], bars[d][4]
    for k in range(1, max_h + 1):
        t = d + k * DAY
        if t > st.p.end:
            return None
        if t not in bars:                         # stopped trading: exit at its last close (delivery)
            last = st.p.last[s]
            return (bars[last][4] / entry - 1, k) if last > d else None
        c, atr = bars[t][4], _atr(bars, t)
        if (atr is not None and c < hi - mult * atr) or k == max_h:
            return c / entry - 1, k
        hi = max(hi, c)
    return None


def breakout_days(st: Study) -> list[tuple[int, str]]:
    """(day, coin) of every L2 signal: the close exceeds the previous 20 days' highest high, first in 20 days."""
    out, last = [], {}
    for d in st.p.days:
        for s in st.uni(d):
            bars = st.p.bars[s]
            highs = [bars.get(d - k * DAY) for k in range(1, 21)]
            if any(b is None for b in highs) or bars[d][4] <= max(b[2] for b in highs):
                continue
            if d - last.get(s, -10 ** 15) < 20 * DAY:
                continue
            last[s] = d
            out.append((d, s))
    return out


def breakout(st: Study) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {f"hold{h}": [] for h in (5, 10, 20)} | {"chandelier3": []}
    for d, s in breakout_days(st):
        for h in (5, 10, 20):
            if (e := _event(st, s, d, st.net(s, d, h), h)) is not None:
                out[f"hold{h}"].append(e)
        ch = chandelier(st, s, d)
        if ch is not None:
            r, k = ch
            if (e := _event(st, s, d, r - st.p.funding(s, d, k) - COST, k)) is not None:
                out["chandelier3"].append(e)
    return out


def _beta(st: Study, s: str, d: int, n: int = 60) -> float:
    xs, ys = [], []
    for k in range(n):
        t, t0 = d - k * DAY, d - (k + 1) * DAY
        b, b0, c, c0 = st.btc.get(t), st.btc.get(t0), st.p.bars[s].get(t), st.p.bars[s].get(t0)
        if None not in (b, b0, c, c0):
            xs.append(b / b0 - 1)
            ys.append(c[4] / c0[4] - 1)
    if len(xs) < 30 or statistics.pvariance(xs) == 0:
        return 1.0
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    return sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / len(xs) / statistics.pvariance(xs)


def rs_names(st: Study, d: int) -> list[str]:
    """The L1 universe at day d: the top 30 by 30-day volume without BTC ([] below 15 names)."""
    names = [s for s in st.uni(d, BASKET + 1) if s != BTC][:BASKET]
    return names if len(names) >= 15 else []


def rs_picks(st: Study, d: int, n: int, names: list[str], betas: dict[str, float] | None = None) -> list[str]:
    """The top 5 of `names` by their n-day return residual to BTC at close d (beta from 60 daily returns)."""
    b0 = st.btc.get(d - n * DAY)
    if b0 is None or d not in st.btc:
        return []
    betas = betas or {s: _beta(st, s, d) for s in names}
    resid = {s: st.p.bars[s][d][4] / st.p.bars[s][d - n * DAY][4] - 1 - betas[s] * (st.btc[d] / b0 - 1)
             for s in names if d - n * DAY in st.p.bars[s]}
    return sorted(resid, key=lambda x: -resid[x])[:5]


def residual_momentum(st: Study) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {f"lb{n}": [] for n in (7, 14, 28)}
    for d in st.p.days:
        if datetime.fromtimestamp(d / 1000, UTC).weekday() != 6 or not (names := rs_names(st, d)):
            continue
        b7 = st.bench(d, 7, names)
        if b7 is None:
            continue
        betas = {s: _beta(st, s, d) for s in names}
        for n in (7, 14, 28):
            for s in rs_picks(st, d, n, names, betas):
                net = st.net(s, d, 7)
                if net is not None:
                    out[f"lb{n}"].append({"d": d, "s": s, "net": net, "excess": net - b7,
                                          "gate": st.gate(PRIMARY_GATE, d)})
    return out


def _fz(f: dict[int, float], d: int) -> float | None:
    w = [f[d - k * DAY] for k in range(1, 31) if d - k * DAY in f]
    if len(w) < 25 or d not in f:
        return None
    sd = statistics.pstdev(w)
    return (f[d] - statistics.fmean(w)) / sd if sd else None


def alt_negative_funding(st: Study) -> list[dict[str, Any]]:
    out, busy = [], {}
    for d in st.p.days:
        for s in st.uni(d):
            f = st.p.fund.get(s, {})
            if busy.get(s, -1) >= d or (z := _fz(f, d)) is None or z > -1.5:
                continue
            h = 1
            while h < 30 and ((z2 := _fz(f, d + h * DAY)) is None or z2 <= 0):
                h += 1
            busy[s] = d + h * DAY
            if (e := _event(st, s, d, st.net(s, d, h), h)) is not None:
                out.append(e)
    return out


def capitulation(st: Study) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {f"hold{h}": [] for h in (3, 7, 14)}
    last: dict[str, int] = {}
    for d in st.p.days:
        uni = st.uni(d)
        r7 = {s: st.p.bars[s][d][4] / st.p.bars[s][d - 7 * DAY][4] - 1 for s in uni if d - 7 * DAY in st.p.bars[s]}
        if len(r7) < 10:
            continue
        m = statistics.fmean(r7.values())
        for s, r in r7.items():
            if r > -0.30 or r - m > -0.20 or d - last.get(s, -10 ** 15) < 14 * DAY:
                continue
            last[s] = d
            for h in (3, 7, 14):
                if (e := _event(st, s, d, st.net(s, d, h), h)) is not None:
                    out[f"hold{h}"].append(e)
    return out


def run(db_path: str) -> dict[str, Any]:
    from ..histdaily import load
    st = Study(Panel(load(db_path)))
    res: dict[str, Any] = {"tsmom": tsmom(st)}
    for name, groups, block in (("breakout_20d", breakout(st), {"hold5": 5, "hold10": 10, "hold20": 20,
                                                                "chandelier3": 20}),
                                ("residual_momentum", residual_momentum(st), {"lb7": 7, "lb14": 7, "lb28": 7}),
                                ("alt_negative_funding", {"z-1.5": alt_negative_funding(st)}, {"z-1.5": 30}),
                                ("capitulation", capitulation(st), {"hold3": 3, "hold7": 7, "hold14": 14})):
        res[name] = {k: summarise(v, block[k]) for k, v in groups.items()}
    return res


FAMILIES = {"tsmom": "lib.tsmom_trend_following", "breakout_20d": "lib.breakout_20d_overlay",
            "residual_momentum": "lib.beta_hedged_rs_pair", "alt_negative_funding": "lib.funding_negative_zscore_long",
            "capitulation": "lib.capitulation_forced_selling_long"}


def rows(res: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for key, v in res["tsmom"].items():
        if key.startswith("weekly_basket"):
            continue
        t = v["timed"]
        if "t" not in t:
            continue
        out.append({"name": f"tsmom|{key}", "family": FAMILIES["tsmom"], "params": {"variant": key},
                    "n": t["days"], "days": t["days"], "mean": t["mean_daily"], "t": t["t"], "t_kind": "iid_daily",
                    "metric": "daily_ret"})
    for study in ("breakout_20d", "residual_momentum", "alt_negative_funding", "capitulation"):
        for k, s in res[study].items():
            out.append({"name": f"{study}|{k}", "family": FAMILIES[study], "params": {"variant": k}, "n": s["n"],
                        "coins": s.get("coins"), "mean": s.get("excess"), "se": s.get("se"), "t": s.get("t_2way"),
                        "t_kind": "2way_block_coin", "n_eff": s.get("n_eff"), "daily": s.get("daily"),
                        "metric": "excess_ret"})
    return out


def main(argv: list[str] | None = None) -> None:
    from .. import trial_register as TR
    ap = argparse.ArgumentParser(prog="shortbot.backtest.long_studies")
    ap.add_argument("--db", type=Path, help="default: <data dir>/daily.db")
    ap.add_argument("--out", type=Path, help="JSON file (default: <data dir>/research/<date>-<sha7>/long_studies/)")
    ap.add_argument("--no-save", action="store_true", help="print only, record nothing")
    given = list(sys.argv[1:] if argv is None else argv)
    a = ap.parse_args(given)
    db = a.db or P.data_dir() / "daily.db"
    fp = P.fingerprint(db)
    res = run(str(db))
    if not a.no_save:
        prov = P.stamp(P.config("shortbot.backtest.long_studies", P.ensure_arg(given, "--db", db)), {"daily_db": fp})
        print("saved to", out := P.write_json("long_studies", prov, res, a.out))
        print("trial register:", TR.record("long_studies", rows(res), prov, out), "rows")
    for study in ("breakout_20d", "residual_momentum", "alt_negative_funding", "capitulation"):
        for v in res[study].values():
            v.pop("daily", None)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
