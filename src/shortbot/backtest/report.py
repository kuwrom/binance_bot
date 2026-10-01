"""Backtest report: per trial (strategy x param set) stats after costs, placebo edge, walk-forward (select the best
param set of each strategy on the first half, judge it on the second), trial ledger + Deflated Sharpe, and feature
splits (pooled strategy trades vs the placebo and the random baseline in the same bucket).

Verdict (per strategy, on the IS-selected param set):
  SURVIVES      OOS avg R > 0, IS avg R > 0, beats BOTH placebos (same coin / same hour), stays > 0 under the
                cost stress, day-clustered t >= 2 and DSR >= 0.95
  promising     OOS and IS > 0 and beats both placebos, but not significant after the multiple-testing haircut
  fails         anything else;  insufficient: fewer than 10 trades (or < 5 out of sample)
  "(relative edge only)": absolute R fails, but the trade beats its same-hour placebo in both halves with a
                day-clustered t >= 3 (better than shorting anything else that hour; not money for a short-only book)"""

from __future__ import annotations

import statistics
from typing import Any

from .features import Market
from .sim import H
from .stats import paired_edge, sharpe, summarize, welch_t
from .strategies import WARMUP
from .trials_eff import deflate


def cut(v: float | None, edges: list[float], fmt: str = "{:g}") -> str:
    if v is None:
        return "n/a"
    for k, e in enumerate(edges):
        if v < e:
            return f"<{fmt.format(e)}" if k == 0 else f"{fmt.format(edges[k - 1])}-{fmt.format(e)}"
    return f">={fmt.format(edges[-1])}"


def _atr_edges(trades: list[dict]) -> list[float]:
    xs = sorted(t["f"]["atr_pct"] for t in trades if t["f"].get("atr_pct") is not None)
    return [xs[len(xs) // 3], xs[2 * len(xs) // 3]] if len(xs) >= 9 else [1.0, 2.0]


def buckets(atr_edges: list[float]) -> dict[str, Any]:
    return {
        "liquidity_24h_vol_$M": lambda f: cut(f["vol24_m"], [2, 5, 20, 100]),
        "oi_$M": lambda f: cut(f["oi_m"], [5, 20, 60]),
        "oi_over_vol": lambda f: cut(f["oi_vol"], [0.5, 1.5]),
        "volatility_atr_tercile": lambda f: {0: "low", 1: "mid", 2: "high"}[
            sum(f["atr_pct"] >= e for e in atr_edges)],
        "session": lambda f: f["session"],
        "weekend": lambda f: f["weekend"],
        "hour_block_utc": lambda f: f"{f['hour'] // 4 * 4:02d}-{f['hour'] // 4 * 4 + 3:02d}",
        "listing_age_d": lambda f: cut(f["age_d"], [30, 180, 365]),
        "has_spot": lambda f: f["has_spot"],
        "alpha_tag": lambda f: f["alpha"],
        "sector": lambda f: f["sector"],
        "sync_vs_idio_4h": lambda f: f["sync"],
        "btc_trend_24h_pct": lambda f: cut(f["btc_ret_24h"], [-2, 0, 2]),
        "btc_beta_72h": lambda f: cut(f["beta"], [0.8, 1.5]),
        "btc_corr_72h": lambda f: cut(f["corr"], [0.3, 0.6]),
        "who_is_long": lambda f: f["who_long"],
        "taker_ratio": lambda f: cut(f["taker_ratio"], [0.9, 1.1]),
        "taker_imbalance_4h": lambda f: cut(f["ti_4h"], [-0.05, 0.05]),
        "oi_chg_24h_pct": lambda f: cut(f["oi_chg_24h"], [-5, 10]),
        "funding_pct": lambda f: cut(f["funding_pct"], [0, 0.01, 0.05]),
        "premium_bp": lambda f: cut(f["premium_bp"], [-5, 5, 20]),
        "trend_er_24h": lambda f: cut(f["er_24h"], [0.2, 0.45]),
        "ret_24h_pct": lambda f: cut(f["ret_24h"], [-10, 0, 10, 30]),
        "dist_24h_high_pct": lambda f: cut(f["dist_high24_pct"], [1, 5, 15]),
        "dist_round_pct": lambda f: cut(f["dist_round_pct"], [0.5, 2]),
        "false_break_24h": lambda f: f["false_break_24h"],
    }


def _cell(ts: list[dict]) -> dict[str, Any] | None:
    if not ts:
        return None
    rs = [t["r"] for t in ts]
    return {"n": len(rs), "coins": len({t["symbol"] for t in ts}), "avg_r": round(statistics.fmean(rs), 3),
            "win": round(sum(r > 0 for r in rs) / len(rs) * 100)}


def dedupe(trades: list[dict]) -> list[dict]:
    """One trade per (coin, entry bar): param sets of one strategy share entries; pooling must not double count."""
    seen, out = set(), []
    for t in trades:
        k = (t["symbol"], t["entry_i"])
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def splits(groups: dict[str, list[dict]], edges: list[float], min_n: int = 8) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, fn in buckets(edges).items():
        cells: dict[str, dict[str, Any]] = {}
        for g, ts in groups.items():
            by: dict[str, list[dict]] = {}
            for t in ts:
                try:
                    k = str(fn(t["f"]))
                except (TypeError, KeyError):
                    k = "n/a"
                by.setdefault(k, []).append(t)
            for k, v in by.items():
                if len(v) >= min_n:
                    cells.setdefault(k, {})[g] = _cell(v)
        out[name] = dict(sorted(cells.items()))
    return out


def verdict(sel: dict[str, Any]) -> str:
    a, i, o = sel["all"], sel["is"], sel["oos"]
    if a.get("n", 0) < 10 or o.get("n", 0) < 5 or i.get("n", 0) < 5:
        return "insufficient"
    ok = (o["avg_r"] > 0 and i["avg_r"] > 0 and (sel.get("edge_vs_placebo") or 0) > 0
          and (sel.get("edge_vs_time") or 0) > 0)
    if ok and (a.get("t_day") or 0) >= 2 and (sel.get("dsr") or 0) >= 0.95 and (sel.get("stress_r") or 0) > 0:
        return "SURVIVES"
    if ok:
        return "promising"
    pe = sel.get("paired_time") or {}
    if (pe.get("is") or 0) > 0 and (pe.get("oos") or 0) > 0 and (pe.get("t_day") or 0) >= 3:
        return "fails (relative edge only)"
    return "fails"


def _edge(tr: list[dict], ctl: list[dict]) -> float | None:
    return round(statistics.fmean(t["r"] for t in tr) - statistics.fmean(t["r"] for t in ctl), 3) if tr and ctl else None


def build(res: dict[str, Any], m: Market, stress: dict[str, Any] | None = None, dsr_mode: str = "legacy"
          ) -> dict[str, Any]:
    mid = (m.t0 + WARMUP * H + m.t1) // 2
    q_len = (m.t1 - m.t0 - WARMUP * H) / 4

    def quarters(tr: list[dict]) -> list[float | None]:
        """avg R per quarter of the window: an edge should not live in one quarter only."""
        out = []
        for q in range(4):
            a = m.t0 + WARMUP * H + q * q_len
            xs = [t["r"] for t in tr if a <= t["entry_ts"] < a + q_len or (q == 3 and t["entry_ts"] >= a)]
            out.append(round(statistics.fmean(xs), 2) if xs else None)
        return out
    rows: dict[str, Any] = {}
    for name, r in res["trials"].items():
        tr = r["trades"]
        pl = (res["placebo"].get(name) or {}).get("trades") or []
        pt = (res["placebo_time"].get(name) or {}).get("trades") or []
        st_tr = ((stress or {}).get("trials", {}).get(name) or {}).get("trades") or []
        s = summarize(tr)
        rows[name] = {"strategy": r["strategy"], "grid_idx": r["grid_idx"], "params": r["params"],
                      "signals": r["signals"], "unfilled": r["unfilled"], "censored": r["censored"], "all": s,
                      "is": summarize([t for t in tr if t["entry_ts"] < mid]),
                      "oos": summarize([t for t in tr if t["entry_ts"] >= mid]), "quarters": quarters(tr),
                      "quarters_time_placebo": quarters(pt),
                      "placebo": summarize(pl), "placebo_time": summarize(pt),
                      "edge_vs_placebo": _edge(tr, pl), "edge_vs_time": _edge(tr, pt),
                      "paired_time": paired_edge(tr, pt, mid), "paired_coin": paired_edge(tr, pl, mid),
                      "t_vs_placebo": welch_t([t["r"] for t in tr], [t["r"] for t in pl]),
                      "t_vs_time": welch_t([t["r"] for t in tr], [t["r"] for t in pt]),
                      "stress_r": round(statistics.fmean(t["r"] for t in st_tr), 3) if st_tr else None,
                      "sr": sharpe([t["r"] for t in tr])}
    # EVERY param set tried counts; "clustered" = round 4's effective N and family V[SR] (trials_eff)
    defl = deflate({n: r["trades"] for n, r in res["trials"].items()}, dsr_mode)
    n_trials, var_sr = defl["n_trials"], defl["var_sr"]
    for name, v in rows.items():
        v.update(defl["per_trial"][name])
    strategies: dict[str, Any] = {}
    for name, v in rows.items():
        strategies.setdefault(v["strategy"], []).append((name, v))
    selected = {}
    for st, items in strategies.items():
        cands = [(n, v) for n, v in items if v["is"].get("n", 0) >= 5] or items
        n_sel, v_sel = max(cands, key=lambda x: (x[1]["is"].get("avg_r", -99), x[0]))
        selected[st] = {"trial": n_sel, "n_param_sets": len(items), **v_sel}
        selected[st]["verdict"] = verdict(v_sel)
    pooled = dedupe([t for n, r in res["trials"].items() for t in r["trades"] if "session" not in r["params"]])
    placebo = dedupe([t for r in res["placebo"].values() for t in r["trades"]])
    ptime = dedupe([t for r in res["placebo_time"].values() for t in r["trades"]])
    rnd = res["controls"]["random"]["trades"]
    edges = _atr_edges(pooled + placebo + ptime + rnd)
    per_strategy_splits = {}
    for st, s in selected.items():
        tr = res["trials"][s["trial"]]["trades"]
        if len(tr) >= 20:
            per_strategy_splits[st] = splits({"strategy": tr, "placebo": (res["placebo"].get(s["trial"]) or {})
                                              .get("trades") or [], "time_placebo": (res["placebo_time"].get(
                                                  s["trial"]) or {}).get("trades") or []}, edges, min_n=5)
    return {"window": res["window"], "mid": mid, "n_trials": n_trials, "var_sr": round(var_sr, 4),
            "dsr_mode": dsr_mode, "n_eff": defl["n_eff"], "var_sr_legacy": defl["var_sr_legacy"],
            "families": defl.get("families"), "placebo_mode": res.get("placebo_mode", "legacy"),
            "trials": rows, "selected": selected,
            "pooled": {"strategy": summarize(pooled), "placebo": summarize(placebo), "time_placebo": summarize(ptime),
                       "random": summarize(rnd)},
            "splits": splits({"strategy": pooled, "placebo": placebo, "time_placebo": ptime, "random": rnd}, edges),
            "per_strategy_splits": per_strategy_splits, "atr_tercile_edges": [round(e, 2) for e in edges]}


ABBR = {"strategy": "S", "placebo": "Pc", "time_placebo": "Pt", "random": "Rnd"}


def _pe(p: dict | None) -> str:
    if not p or p.get("edge") is None:
        return "–"
    return f"{p['edge']:+.2f} (t {_f(p.get('t_day'), 1)}; {_f(p.get('is'))}/{_f(p.get('oos'))})"


def _f(x: Any, nd: int = 2) -> str:
    return "–" if x is None else f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def markdown(rep: dict[str, Any]) -> str:
    w = rep["window"]
    L = [f"# Backtest {w['t0']} → {w['t1']} ({w['coins']} perps; L/S data: {w['has_ls']}; delist data: "
         f"{w['has_delist']})", "",
         f"Trials in the ledger: **{rep['n_trials']}** (effective N {rep.get('n_eff')}; var of trial Sharpe "
         f"{rep['var_sr']}, legacy {rep.get('var_sr_legacy')}; DSR mode {rep.get('dsr_mode', 'legacy')}, placebo "
         f"{rep.get('placebo_mode', 'legacy')}). "
         "OOS = entries after the window midpoint. R after fees, slippage and funding.", "",
         "## Per strategy (param set picked on the first half, judged on the second)", "",
         "| strategy | picked | sets | n | coins | IS avg R | OOS n | OOS avg R | OOS win% | all avg R | gross R | "
         "PF | maxDD R | squeeze% | vs coin plc | vs time plc (paired t_day; IS/OOS) | stress R | t_day | DSR | R by quarter (time plc) | verdict |",
         "|" + "---|" * 21]
    for st, s in rep["selected"].items():
        a, i, o = s["all"], s["is"], s["oos"]
        L.append(f"| {st} | {s['trial'].split('[', 1)[-1].rstrip(']') if '[' in s['trial'] else ''} | "
                 f"{s['n_param_sets']} | {a.get('n', 0)} | {a.get('coins', 0)} | {_f(i.get('avg_r'))} | "
                 f"{o.get('n', 0)} | {_f(o.get('avg_r'))} | {_f(o.get('win_pct'), 0)} | {_f(a.get('avg_r'))} | "
                 f"{_f(a.get('avg_r_gross'))} | {_f(a.get('pf'))} | {_f(a.get('max_dd_r'))} | "
                 f"{_f(a.get('squeeze_pct'), 0)} | {_f(s.get('edge_vs_placebo'))} | {_pe(s.get('paired_time'))} | "
                 f"{_f(s.get('stress_r'))} | {_f(a.get('t_day'))} | "
                 f"{_f(s.get('dsr'))} | {' '.join(_f(x) for x in s['quarters'])} "
                 f"({' '.join(_f(x) for x in s['quarters_time_placebo'])}) | {s['verdict']} |")
    L += ["", "## Every trial (the ledger)", "",
          "| trial | signals | n | coins | win% | avg R | gross R | cost % | funding % | PF | IS R | OOS R | "
          "coin plc R | time plc R | paired vs time plc (t_day) | stress R | SR | DSR |", "|" + "---|" * 18]
    for name, v in rep["trials"].items():
        a = v["all"]
        L.append(f"| {name} | {v['signals']} | {a.get('n', 0)} | {a.get('coins', 0)} | {_f(a.get('win_pct'), 0)} | "
                 f"{_f(a.get('avg_r'))} | {_f(a.get('avg_r_gross'))} | {_f(a.get('avg_cost_pct'))} | "
                 f"{_f(a.get('avg_funding_pct'), 3)} | {_f(a.get('pf'))} | {_f(v['is'].get('avg_r'))} | "
                 f"{_f(v['oos'].get('avg_r'))} | {_f(v['placebo'].get('avg_r'))} | {_f(v['placebo_time'].get('avg_r'))} | "
                 f"{_pe(v['paired_time'])} | {_f(v['stress_r'])} | "
                 f"{_f(v['sr'])} | {_f(v.get('dsr'))} |")
    p = rep["pooled"]
    L += ["", "## Pooled (deduplicated) vs controls", "",
          "| group | n | coins | win% | avg R | gross R | squeeze% | t_day |", "|---|---|---|---|---|---|---|---|"]
    for g, s in p.items():
        L.append(f"| {g} | {s.get('n', 0)} | {s.get('coins')} | {_f(s.get('win_pct'), 0)} | {_f(s.get('avg_r'))} | "
                 f"{_f(s.get('avg_r_gross'))} | {_f(s.get('squeeze_pct'), 0)} | {_f(s.get('t_day'))} |")
    L += ["", f"## Feature splits (avg R [n]; ATR% tercile edges {rep['atr_tercile_edges']})", ""]
    for feat, cells in rep["splits"].items():
        L.append(f"**{feat}**: " + "; ".join(
            f"{k}: " + " / ".join(f"{ABBR.get(g, g)} {_f(c['avg_r'])} [{c['n']}]" for g, c in v.items())
            for k, v in cells.items()))
        L.append("")
    return "\n".join(L)
