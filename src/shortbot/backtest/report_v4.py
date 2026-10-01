"""Round-3 report (docs/FAILURES.md "Round 3"): each live veto as blocked vs allowed on the same base book, the
premium buckets, the funding paid by the negative-funding trades, and the flush / squeeze event studies.

Per-veto verdict (fixed before the OOS run), per base:
  needs more data   blocked OOS n < 30
  INVERT-TO-SHADOW  the blocked trades pass the adopt-to-shadow bar (report_v3.verdict2) and do at least as well
                    OOS as the allowed ones
  KEEP              blocked trades do worse OOS than allowed ones in BOTH avg R and the same-hour paired edge
  DROP              anything else (blocked trades do as well or better OOS on at least one of the two)"""

from __future__ import annotations

import math
import statistics
from typing import Any

from .features import Market
from .report_v3 import _cols, _f, verdict2
from .strategies_v4 import BASES, PREM_BUCKETS, VETOES, base_name

MIN_OOS = 30
FWD_H = (4, 24, 72)


def _day_means(trades: list[dict], key: str = "r") -> list[float]:
    g: dict[str, list[float]] = {}
    for t in trades:
        g.setdefault(t["day"], []).append(t[key])
    return [statistics.fmean(v) for v in g.values()]


def diff_t(a: list[dict], b: list[dict]) -> tuple[float | None, float | None]:
    """mean(a) - mean(b) and its t with day-clustered standard errors (each side averaged within days first)."""
    if len(a) < 3 or len(b) < 3:
        return None, None
    da, db = _day_means(a), _day_means(b)
    d = statistics.fmean(t["r"] for t in a) - statistics.fmean(t["r"] for t in b)
    if len(da) < 3 or len(db) < 3:
        return round(d, 3), None
    se = math.sqrt(statistics.variance(da) / len(da) + statistics.variance(db) / len(db))
    return round(d, 3), (round(d / se, 2) if se else None)


def veto_verdict(blk: dict[str, Any], alw: dict[str, Any]) -> str:
    bo, ao = blk["oos"], alw["oos"]
    if bo.get("n", 0) < MIN_OOS or ao.get("n", 0) < MIN_OOS:
        return "needs more data"
    be = (blk.get("paired_time") or {}).get("oos")
    ae = (alw.get("paired_time") or {}).get("oos")
    if verdict2(blk) in ("SURVIVES", "adopt-to-shadow") and bo["avg_r"] >= ao["avg_r"]:
        return "INVERT-TO-SHADOW"
    if bo["avg_r"] < ao["avg_r"] and be is not None and ae is not None and be < ae:
        return "KEEP"
    return "DROP"


def funding_r(trades: list[dict]) -> dict[str, Any]:
    """Funding per trade in R (negative = paid by the short): mean, median, p10 (the worst decile), share paying
    >= 0.1R, and the mean in % of notional."""
    xs = sorted(t["funding_pct"] / t["risk_pct"] for t in trades if t.get("risk_pct"))
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "mean": round(statistics.fmean(xs), 3), "median": round(statistics.median(xs), 3),
            "p10": round(xs[int(0.1 * (len(xs) - 1))], 3), "paid_ge_0.1R": round(sum(x <= -0.1 for x in xs) / len(xs) * 100),
            "pct": round(statistics.fmean(t["funding_pct"] for t in trades), 3)}


def forward(m: Market, trades: list[dict], key: str = "signal_i") -> dict[int, dict[str, Any]]:
    """Raw short-side forward return from the signal close (no stops): median % and share of events lower."""
    out = {}
    for h in FWD_H:
        xs = []
        for t in trades:
            c = m.coins.get(t["symbol"])
            i = t[key]
            if c and i + h < len(c.rows):
                xs.append((c.rows[i]["c"] - c.rows[i + h]["c"]) / c.rows[i]["c"] * 100)
        out[h] = {"n": len(xs), "med": round(statistics.median(xs), 2) if xs else None,
                  "lower": round(sum(x > 0 for x in xs) / len(xs) * 100) if xs else None}
    return out


def _half(trades: list[dict], mid: int, is_: bool) -> list[dict]:
    return [t for t in trades if (t["entry_ts"] < mid) == is_]


HEAD = "n | IS R | OOS R (n) | edge vs same-hour plc (t) IS/OOS | stress R | t_day | DSR"


def _row(label: str, v: dict, extra: str = "") -> str:
    return f"| {label} | {v['all'].get('coins', 0)} | {_cols(v)} | {verdict2(v)} |{extra}"


def round3_markdown(rep: dict[str, Any], res: dict[str, Any], m: Market) -> str:
    mid, tri, rows = rep["mid"], res["trials"], rep["trials"]
    L = [f"# Round 3: live vetoes (N = {rep['n_trials']} trials, V[SR] = {rep['var_sr']})", ""]
    L += ["## Premium buckets at entry (perp premium index close of the signal bar)", "",
          f"| base | bucket | coins | {HEAD} | shadow bar | avg pnl % | p90 MAE % |", "|" + "---|" * 13]
    for key in BASES:
        for b in PREM_BUCKETS:
            name = next((n for n in rows if n.startswith(f"{key}|prem:{b}[")
                         or (b == PREM_BUCKETS[-1] and n.startswith(f"{key}|prem_hi:blocked["))), None)
            if not name:
                continue
            v = rows[name]
            L.append(_row(f"{key} | {b}", v, f" {_f(v['all'].get('avg_pnl_pct'))} | {_f(v['all'].get('mae_p90_pct'))} |"))
    L += ["", "## Blocked vs allowed, per veto and base", "",
          f"| veto | base | side | coins | {HEAD} | shadow bar | OOS blocked − allowed R (t) | verdict |",
          "|" + "---|" * 15]
    verdicts: dict[tuple[str, str], str] = {}
    for veto in VETOES:
        for key in BASES:
            nb = next((n for n in rows if n.startswith(f"{key}|{veto}:blocked[")), None)
            na = next((n for n in rows if n.startswith(f"{key}|{veto}:allowed[")), None)
            if not nb or not na:
                continue
            vb, va = rows[nb], rows[na]
            d, t = diff_t(_half(tri[nb]["trades"], mid, False), _half(tri[na]["trades"], mid, False))
            vv = veto_verdict(vb, va)
            verdicts[(veto, key)] = vv
            L.append(f"| {veto} | {key} | blocked | {vb['all'].get('coins', 0)} | {_cols(vb)} | {verdict2(vb)} | "
                     f"{_f(d)} (t {_f(t, 1)}) | **{vv}** |")
            L.append(f"| {veto} | {key} | allowed | {va['all'].get('coins', 0)} | {_cols(va)} | {verdict2(va)} | | |")
    L += ["", "## Funding paid, negative-funding veto (R per trade; negative = the short paid)", "",
          "| base | side | n | mean R | median R | p10 R | paid ≥ 0.1R % | mean % of notional |", "|---|---|---|---|---|---|---|---|"]
    for key in BASES:
        for side in ("blocked", "allowed"):
            n = next((x for x in rows if x.startswith(f"{key}|neg_funding:{side}[")), None)
            if n:
                f = funding_r(tri[n]["trades"])
                L.append(f"| {key} | {side} | {f['n']} | {_f(f.get('mean'), 3)} | {_f(f.get('median'), 3)} | "
                         f"{_f(f.get('p10'), 3)} | {f.get('paid_ge_0.1R', '–')} | {_f(f.get('pct'), 3)} |")
    L += ["", "## Event studies: long flush / short squeeze with OI contracts down ≥ 3% in the hour", "",
          f"| event | coins | {HEAD} | shadow bar | fwd short return, median % / % lower: 4h · 24h · 72h (IS; OOS) "
          f"| same-hour plc 24h · 72h (all) |", "|" + "---|" * 12]
    for ev in ("flush_short", "squeeze_short"):
        name = next((n for n in rows if n.startswith(ev + "[")), None)
        if not name:
            continue
        tr = tri[name]["trades"]
        fi, fo = forward(m, _half(tr, mid, True)), forward(m, _half(tr, mid, False))
        fp = forward(m, (res["placebo_time"].get(name) or {}).get("trades") or [])
        fwd = " · ".join(f"{_f(fi[h]['med'])}/{fi[h]['lower']}; {_f(fo[h]['med'])}/{fo[h]['lower']}" for h in FWD_H)
        plc = " · ".join(f"{_f(fp[h]['med'])}/{fp[h]['lower']}" for h in FWD_H[1:])
        L.append(_row(ev, rows[name], f" {fwd} | {plc} |"))
    L += ["", "## Verdict summary", "", "| veto | " + " | ".join(BASES) + " |", "|---|" + "---|" * len(BASES)]
    for veto in VETOES:
        L.append(f"| {veto} | " + " | ".join(verdicts.get((veto, k), "–") for k in BASES) + " |")
    L += ["", "Base books (unsplit): " + "; ".join(
        f"{k} n {rows[base_name(k)]['all'].get('n')} IS {_f(rows[base_name(k)]['is'].get('avg_r'))} "
        f"OOS {_f(rows[base_name(k)]['oos'].get('avg_r'))}" for k in BASES if base_name(k) in rows)]
    return "\n".join(L)
