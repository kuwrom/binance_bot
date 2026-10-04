"""Round-2 report (docs/FAILURES.md): entry-type table with fill rates, exit-management table with paired
differences on identical signals and post-entry continuation stats (Kaminski & Lo 2014: stops and trailing add value
only when returns continue after entry), and the re-entry / funding-budget table. Verdicts use the FAILURES.md bar."""

from __future__ import annotations

import math
import statistics
from typing import Any

from .features import Market
from .stats import cluster_t, paired_edge
from .strategies_v3 import BD_DROPS, EXIT_MODES, PUMP_FAMILY, REFERENCE


def verdict2(v: dict[str, Any]) -> str:
    """FAILURES.md: needs data (OOS n < 30) > SURVIVES > adopt-to-shadow > reject."""
    i, o, pt = v["is"], v["oos"], v.get("paired_time") or {}
    if o.get("n", 0) < 30:
        return "needs more data"
    beats = (v.get("edge_vs_placebo") or 0) > 0 and (v.get("edge_vs_time") or 0) > 0
    stress = v["stress_r"] if v.get("stress_r") is not None else -1.0
    if (o["avg_r"] > 0 and i.get("avg_r", -1) > 0 and beats and stress > 0
            and (v["all"].get("t_day") or 0) >= 2 and (v.get("dsr") or 0) >= 0.95):
        return "SURVIVES"
    if o["avg_r"] > 0 and (pt.get("is") or 0) > 0 and (pt.get("oos") or 0) > 0 and stress >= 0:
        return "adopt-to-shadow"
    return "reject"


def paired_diff(a: list[dict], b: list[dict], mid: int, key: str = "signal_i") -> dict[str, Any]:
    """Same signal, two treatments: mean of (R_a - R_b) over signals both took, day-clustered t, IS / OOS means."""
    bb = {(t["symbol"], t[key]): t["r"] for t in b}
    d = [{"r": t["r"] - bb[k], "day": t["day"], "ts": t["entry_ts"]} for t in a if (k := (t["symbol"], t[key])) in bb]
    if len(d) < 3:
        return {"n": len(d)}
    half = lambda f: [x["r"] for x in d if (x["ts"] < mid) == f]  # noqa: E731
    return {"n": len(d), "d": round(statistics.fmean(x["r"] for x in d), 3), "t": cluster_t(d, "day")[0],
            "is": round(statistics.fmean(half(True)), 3) if half(True) else None,
            "oos": round(statistics.fmean(half(False)), 3) if half(False) else None}


def continuation(trades: list[dict], m: Market) -> dict[str, Any]:
    """Post-entry price path over the time-stop window, ignoring exits except the original stop:
    ac1   lag-1 autocorrelation of hourly returns (scaled by the trade's risk), pooled
    p2    P(+2R before back to entry | +1R reached before the stop); 0.5 = random walk, > 0.5 = continuation
    drift mean price move in R from entry to the time stop (short's view, before costs)"""
    xs, ys, hit, cont, back, drift = [], [], 0, 0, 0, []
    for t in trades:
        c = m.coins.get(t["symbol"])
        if not c:
            continue
        r, e, risk = c.rows, t["entry_px"], t["risk_pct"] / 100
        a, z = t["entry_i"], min(len(r) - 1, t["entry_i"] + t["time_stop"])
        rets = [math.log(r[k]["c"] / r[k - 1]["c"]) / risk for k in range(a + 1, z + 1)]
        xs += rets[:-1]
        ys += rets[1:]
        drift.append((e - r[z]["c"]) / e / risk)
        stage = 0
        for k in range(a + 1, z + 1):
            b = r[k]
            if stage == 0:
                if b["h"] >= t["stop0"]:
                    break
                if b["l"] <= e * (1 - risk):
                    stage, hit = 1, hit + 1
            elif b["h"] >= e:
                back += 1
                break
            elif b["l"] <= e * (1 - 2 * risk):
                cont += 1
                break
    ac1 = None
    if len(xs) >= 10:
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
        sxx, syy = sum((x - mx) ** 2 for x in xs), sum((y - my) ** 2 for y in ys)
        ac1 = round(sxy / math.sqrt(sxx * syy), 3) if sxx and syy else None
    return {"n": len(drift), "ac1": ac1, "hit1": hit, "p2": round(cont / (cont + back), 2) if cont + back else None,
            "back_pct": round(back / hit * 100) if hit else None,
            "drift": round(statistics.fmean(drift), 3) if drift else None}


def _f(x: Any, nd: int = 2) -> str:
    return "–" if x is None else f"{x:+.{nd}f}" if isinstance(x, float) else str(x)


def _pd(p: dict) -> str:
    return "–" if p.get("d") is None else f"{p['d']:+.2f} (t {_f(p.get('t'), 1)}) {_f(p.get('is'))}/{_f(p.get('oos'))}"


def _cols(v: dict) -> str:
    pt = v.get("paired_time") or {}
    edge = "–" if pt.get("edge") is None else (f"{pt['edge']:+.2f} (t {_f(pt.get('t_day'), 1)}) "
                                               f"{_f(pt.get('is'))}/{_f(pt.get('oos'))}")
    return (f"{v['all'].get('n', 0)} | {_f(v['is'].get('avg_r'))} | {_f(v['oos'].get('avg_r'))} "
            f"({v['oos'].get('n', 0)}) | {edge} | {_f(v.get('stress_r'))} | {_f(v['all'].get('t_day'), 1)} | "
            f"{_f(v.get('dsr'))}")


HEAD = "n | IS R | OOS R (n) | edge vs same-hour plc (t) IS/OOS | stress R | t_day | DSR"


def _name(rep: dict, prefix: str) -> str | None:
    return next((k for k in rep["trials"] if k.startswith(prefix + "[")), None)


def round2_markdown(rep: dict[str, Any], res: dict[str, Any], m: Market) -> str:
    mid, tri = rep["mid"], res["trials"]
    L = [f"# Round 2 (N = {rep['n_trials']} trials, V[SR] = {rep['var_sr']})", "",
         "## Q1 entry type in breakdowns (same arms, same stop / targets / exits)", "",
         f"| trial | arms | fill % | R per arm | {HEAD} | verdict |", "|" + "---|" * 12]
    bd = {}
    for d in BD_DROPS:
        for k in ("limit", "stop", "market"):
            name = f"bd_entry[drop={d},entry={k},time=24]"
            if name not in rep["trials"]:
                continue
            v, r = rep["trials"][name], tri[name]
            bd[(d, k)] = r["trades"]
            tried = len(r["trades"]) + r["unfilled"]
            fill = len(r["trades"]) / tried * 100 if tried else 0
            per = sum(t["r"] for t in r["trades"]) / tried if tried else 0
            L.append(f"| {name} | {r['signals']} | {fill:.0f} | {per:+.3f} | {_cols(v)} | {verdict2(v)} |")
    L += ["", "Adverse selection: the market-at-arm trade's R on arms where the limit / sell-stop filled vs did not.", "",
          "| drop | entry | market R where filled (n) | where not filled (n) | paired entry − market (t) IS/OOS |",
          "|---|---|---|---|---|"]
    for d in BD_DROPS:
        mk = bd.get((d, "market")) or []
        for k in ("limit", "stop"):
            got = {(t["symbol"], t["arm_i"]) for t in bd.get((d, k)) or []}
            yes = [t["r"] for t in mk if (t["symbol"], t["arm_i"]) in got]
            no = [t["r"] for t in mk if (t["symbol"], t["arm_i"]) not in got]
            pdf = paired_diff(bd.get((d, k)) or [], mk, mid, key="arm_i")
            L.append(f"| {d} | {k} | {_f(statistics.fmean(yes)) if yes else '–'} ({len(yes)}) | "
                     f"{_f(statistics.fmean(no)) if no else '–'} ({len(no)}) | {_pd(pdf)} |")
    L += ["", "## Q2 exit management on identical signals", "",
          f"| family | exits | {HEAD} | Δ vs fixed, paired (t) IS/OOS | exits | verdict |", "|" + "---|" * 12]
    cont_rows = []
    for key in PUMP_FAMILY:
        names = {mode: (REFERENCE.get(f"{key}+exit_{mode}") or _name(rep, f"{key}+exit_{mode}")) for mode in EXIT_MODES}
        if key == "bounce_fade":
            names["current (be + 2R/1R trail)"] = REFERENCE["bounce_fade+current"]
        fixed = tri.get(names["fixed"] or "", {}).get("trades") or []
        for mode, trial in names.items():
            if not trial or trial not in rep["trials"]:
                continue
            v, tr = rep["trials"][trial], tri[trial]["trades"]
            ex = v["all"].get("exits") or {}
            exs = " ".join(f"{k} {round(n / max(1, len(tr)) * 100)}%" for k, n in ex.items())
            diff = paired_diff(tr, fixed, mid) if mode != "fixed" else {}
            L.append(f"| {key} | {mode} | {_cols(v)} | {_pd(diff)} | {exs} | {verdict2(v)} |")
        if names["fixed"]:
            plc = (res["placebo_time"].get(names["fixed"]) or {}).get("trades") or []
            for grp, tr in (("IS", [t for t in fixed if t["entry_ts"] < mid]), ("all", fixed), ("same-hour plc", plc)):
                cont_rows.append((key, grp, continuation(tr, m)))
    L += ["", "Post-entry continuation (fixed-exit trades; path over the time-stop window, ignoring exits but the "
          "stop). p2 = P(+2R before back to entry | +1R first); 0.5 = random walk.", "",
          "| family | group | n | ac1 (1h) | reached +1R | p2 | gave back to entry % | drift to time stop R |",
          "|---|---|---|---|---|---|---|---|"]
    for key, grp, c in cont_rows:
        L.append(f"| {key} | {grp} | {c['n']} | {_f(c['ac1'], 3)} | {c['hit1']} | {_f(c['p2'])} | "
                 f"{c['back_pct'] if c['back_pct'] is not None else '–'} | {_f(c['drift'])} |")
    L += ["", "## Q3 re-entry after a stop-out, funding budget", "",
          f"| trial | {HEAD} | Δ vs base | added / changed trades: n, avg R, IS/OOS, edge vs plc | verdict |",
          "|" + "---|" * 11]
    for key, base in (("pump_fade", REFERENCE["pump_fade+exit_be"]),
                      ("pump_fade+discount", REFERENCE["pump_fade+discount+exit_be"])):
        for var in ("reentry", "fundbudget"):
            found = _name(rep, f"{key}+{var}")
            if not found:
                continue
            name = found
            v, tr = rep["trials"][name], tri[name]["trades"]
            bt = tri.get(base, {}).get("trades") or []
            if var == "reentry":
                sub = [t for t in tr if (t.get("meta") or {}).get("reentry")]
                extra = ""
            else:
                sub = [t for t in tr if t["reason"] == "fund_budget"]
                extra = f"; Δ paired {_pd(paired_diff(tr, bt, mid))}"
            pe = paired_edge(sub, (res["placebo_time"].get(name) or {}).get("trades") or [], mid)
            s_is = [t["r"] for t in sub if t["entry_ts"] < mid]
            s_oos = [t["r"] for t in sub if t["entry_ts"] >= mid]
            desc = (f"{len(sub)}, {_f(statistics.fmean(t['r'] for t in sub)) if sub else '–'}, "
                    f"{_f(statistics.fmean(s_is)) if s_is else '–'}/{_f(statistics.fmean(s_oos)) if s_oos else '–'}, "
                    f"{_f(pe.get('edge'))}{extra}")
            dv = v["all"].get("avg_r", 0) - (rep["trials"].get(base) or {}).get("all", {}).get("avg_r", 0)
            L.append(f"| {name} | {_cols(v)} | {dv:+.3f} | {desc} | {verdict2(v)} |")
    return "\n".join(L)
