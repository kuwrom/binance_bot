"""Round-4 report (docs/failures/ROUND4.md): the corrections, measured on the headline books.

1. Same-hour placebo, legacy vs matched: the paired edge net AND gross of costs, the donor's vs the placebo's ATR%
   and cost in R (the mechanism of the inflation), and the share of matched draws found in the donor's cell.
   The legacy placebo is re-drawn here with the legacy code and seed, so the numbers equal rounds 0-3's.
2. Deflated Sharpe: raw N vs effective N (correlated families), legacy vs family V[SR], per headline trial.
3. Verdict changes vs an earlier results.json (every trial's shadow-bar verdict, every strategy's verdict and the
   round-3 veto verdicts)."""

from __future__ import annotations

import json
import statistics
import zlib
from pathlib import Path
from typing import Any

from .features import Market
from .report import verdict
from .report_v3 import _f, verdict2
from .report_v4 import veto_verdict
from .stats import paired_edge
from .strategies_more import placebo_signals
from .strategies_v4 import BASES, VETOES

KEY = ("pump_fade[buf_atr=1.0,time=12]", "post_pump_decay[stop_atr=2.0,time=12]", "bounce_fade[drop=-15,time=24]",
       "pump_fade+discount[buf_atr=1.0,time=12]", "level_fade[level=round,entry=market,time=24]",
       "level_fade[level=round,entry=limit,time=24]", "pump_fade+fundbudget[buf_atr=1.0,time=12]",
       "pump_fade+discount+fundbudget[buf_atr=1.0,time=12]")


def legacy_time_placebo(m: Market, name: str, trades: list[dict], per: int = 3) -> list[dict]:
    """Rounds 0-3's same-hour placebo for one trial (run.run_all's seed and draw)."""
    from .run import Runner
    seed = zlib.crc32(name.encode()) % 10_000
    ps = placebo_signals(m, trades, per=per, seed=seed, mode="time")
    return Runner(m).execute(f"placebo_time:{name}", sorted(ps, key=lambda x: (x[0].symbol, x[1])), "legacy")["trades"]


def _atr(m: Market, trades: list[dict]) -> float | None:
    xs = [m.coins[t["symbol"]].atr[t["entry_i"]] for t in trades if t["symbol"] in m.coins]
    return statistics.fmean(xs) if xs else None


def _cost_r(trades: list[dict]) -> float | None:
    xs = [(t["fees_pct"] + t["slip_pct"]) / t["risk_pct"] for t in trades if t.get("risk_pct")]
    return statistics.fmean(xs) if xs else None


def _pe(p: dict, g: bool = False) -> str:
    k = "_gross" if g else ""
    if p.get("edge" + k) is None:
        return "–"
    return (f"{p['edge' + k]:+.2f} (t {_f(p.get('t_day' + k), 1)}) "
            f"{_f(p.get('is' + k))}/{_f(p.get('oos' + k))}")


def edge_rows(rep: dict[str, Any], res: dict[str, Any], m: Market, names=KEY) -> list[dict[str, Any]]:
    out = []
    for name in names:
        if name not in res["trials"]:
            continue
        tr = res["trials"][name]["trades"]
        new = (res["placebo_time"].get(name) or {}).get("trades") or []
        old = legacy_time_placebo(m, name, tr)
        cell = sum((t.get("meta") or {}).get("match") == "cell" for t in new)
        out.append({"trial": name, "n": len(tr), "legacy": paired_edge(tr, old, rep["mid"]),
                    "matched": paired_edge(tr, new, rep["mid"]), "atr": (_atr(m, tr), _atr(m, old), _atr(m, new)),
                    "cost_r": (_cost_r(tr), _cost_r(old), _cost_r(new)),
                    "cell_pct": round(cell / len(new) * 100) if new else None})
    return out


def dsr_rows(rep: dict[str, Any], names=KEY) -> list[str]:
    L = []
    for name in names:
        v = rep["trials"].get(name)
        if v:
            L.append(f"| {name} | {v['all'].get('n', 0)} | {_f(v.get('sr'), 3)} | {_f(v.get('dsr_legacy'))} | "
                     f"{_f(v.get('sr0_rawN'), 3)} | {_f(v.get('dsr_rawN'))} | {_f(v.get('sr0'), 3)} | "
                     f"{_f(v.get('dsr_effN'))} | {_f(v.get('psr0'))} |")
    return L


def verdicts(rep: dict[str, Any]) -> dict[str, str]:
    """Every verdict the rounds report, keyed for a diff: trial:<name>, strategy:<name>, veto:<veto>/<base>."""
    rows = rep["trials"]
    out = {f"trial:{n}": verdict2(v) for n, v in rows.items() if "oos" in v}
    out.update({f"strategy:{s}": verdict(v) for s, v in rep["selected"].items()})
    for veto in VETOES:
        for key in BASES:
            nb = next((n for n in rows if n.startswith(f"{key}|{veto}:blocked[")), None)
            na = next((n for n in rows if n.startswith(f"{key}|{veto}:allowed[")), None)
            if nb and na:
                out[f"veto:{veto}/{key}"] = veto_verdict(rows[nb], rows[na])
    return out


def verdict_changes(old_rep: dict[str, Any], new_rep: dict[str, Any]) -> list[tuple[str, str, str]]:
    a, b = verdicts(old_rep), verdicts(new_rep)
    return [(k, a[k], b[k]) for k in sorted(a) if k in b and a[k] != b[k]]


def round4_markdown(rep: dict[str, Any], res: dict[str, Any], m: Market, compare: Path | None = None) -> str:
    L = [f"# Round 4: corrections (placebo {rep.get('placebo_mode')}, DSR {rep.get('dsr_mode')})", "",
         "## Same-hour paired edge: legacy vs volatility/liquidity-matched placebo", "",
         "Edge = trade R − mean R of its own 3 placebos, day-clustered t, IS/OOS. Gross = before fees, slippage "
         "and funding. ATR% and cost in R: donor / legacy placebo / matched placebo.", "",
         "| trial | n | legacy net | legacy gross | matched net | matched gross | ATR% d/L/M | cost R d/L/M | "
         "matched in cell % |", "|" + "---|" * 9]
    for r in edge_rows(rep, res, m):
        L.append(f"| {r['trial']} | {r['n']} | {_pe(r['legacy'])} | {_pe(r['legacy'], True)} | {_pe(r['matched'])} | "
                 f"{_pe(r['matched'], True)} | {' / '.join(_f(x) for x in r['atr'])} | "
                 f"{' / '.join(_f(x, 3) for x in r['cost_r'])} | {_f(r['cell_pct'])} |")
    L += ["", f"## Deflated Sharpe: raw N {rep['n_trials']}, effective N {rep.get('n_eff')} (families with daily-R "
          f"correlation > 0.7), V[SR] legacy {rep.get('var_sr_legacy')} vs families with n ≥ 100 {rep['var_sr']}", "",
          "| trial | n | SR/trade | DSR legacy | SR* raw N | DSR raw N | SR* eff N | DSR eff N | PSR(0) |",
          "|" + "---|" * 9] + dsr_rows(rep)
    fams = [f for f in rep.get("families") or [] if len(f) > 1]
    L += ["", f"Multi-member families ({len(fams)}; the rest are singletons):", ""]
    L += [f"- {len(f)}: " + ", ".join(f[:6]) + (" …" if len(f) > 6 else "") for f in fams[:15]]
    if compare and Path(compare).exists():
        ch = verdict_changes(json.loads(Path(compare).read_text()), rep)
        L += ["", f"## Verdict changes vs {compare} ({len(ch)})", "", "| verdict | before | after |", "|---|---|---|"]
        L += [f"| {k} | {a} | {b} |" for k, a, b in ch]
    return "\n".join(L)
