"""Compare a replayed scan with the scan the desk logged (task #29): universe, per-row decisions (tradable, setups,
tape state, veto reasons), feature errors, and the match rate with day-clustered intervals (research_stats).

A logged row MATCHES when the replay evaluated the same coin and agrees on tradable, the detected setups and the
tape state (rows logged before the tape state was stored skip that criterion)."""

from __future__ import annotations

import statistics
from collections import Counter
from typing import Any

from ..research import veto_key
from ..research_stats import clustered_wilson, day_of

NUMERIC = ("change_24h", "quote_volume", "last", "rsi_1h", "vwap_z", "off_high_pct", "relvol_1h", "sigma_h",
           "funding_8h", "oi_value", "oi_change_24h", "taker_ratio_now", "ti_z_1h", "vol_heat_z", "basis_bp",
           "listing_age_days", "lc_ret_7d_pct", "lc_dd_from_high_pct", "daily_vol_cv", "funding_7d_avg", "score")
CATEGORICAL = ("tape_state", "pump_state", "sweep_state", "lower_high", "crowded_longs", "vol_hot", "mention_hot",
               "hype_fade", "lifecycle_stage", "controller_label", "spot_available", "delisting_scheduled",
               "funding_flag", "liq_tier", "sector", "map_regime", "macro_next_kind")


def _set(xs: Any) -> set[str]:
    return {veto_key(x) for x in (xs or [])}


def compare_scan(logged: list[dict[str, Any]], replayed: list[dict[str, Any]]) -> dict[str, Any]:
    """Rows carry symbol, tradable, setups, vetoes, score and features (JSON already decoded)."""
    L = {r["symbol"]: r for r in logged}
    R = {r["symbol"]: r for r in replayed}
    common = L.keys() & R.keys()
    rows = []
    for s, lo in L.items():
        re_ = R.get(s)
        rec: dict[str, Any] = {"symbol": s, "in_replay": re_ is not None}
        if re_ is not None:
            lt, rt = lo["features"].get("tape_state"), re_["features"].get("tape_state")
            lv, rv = _set(lo["vetoes"]), _set(re_["vetoes"])
            rec.update(tradable=bool(lo["tradable"]) == bool(re_["tradable"]),
                       setups=sorted(lo["setups"] or []) == sorted(re_["setups"] or []),
                       tape=None if lt is None else lt == rt, vetoes=lv == rv,
                       vetoes_logged_only=sorted(lv - rv), vetoes_replay_only=sorted(rv - lv), tape_pair=[lt, rt],
                       logged_tradable=bool(lo["tradable"]), score_diff=(re_["score"] or 0) - (lo["score"] or 0))
            rec["match"] = rec["tradable"] and rec["setups"] and rec["tape"] is not False
        else:
            rec["match"] = False
        rows.append(rec)
    lt_set = {s for s, r in L.items() if r["tradable"]}
    rt_set = {s for s, r in R.items() if r["tradable"]}
    union = L.keys() | R.keys()
    return {"universe": {"logged": len(L), "replayed": len(R), "common": len(common),
                         "recall": len(common) / len(L) if L else None,
                         "precision": len(common) / len(R) if R else None,
                         "jaccard": len(common) / len(union) if union else None,
                         "missing": sorted(L.keys() - R.keys()), "extra": sorted(R.keys() - L.keys())},
            "tradable": {"logged": sorted(lt_set), "replayed": sorted(rt_set),
                         "jaccard": len(lt_set & rt_set) / len(lt_set | rt_set) if lt_set | rt_set else 1.0},
            "rows": rows, "features": [feature_diff(L[s]["features"], R[s]["features"], s) for s in sorted(common)]}


def feature_diff(lf: dict[str, Any], rf: dict[str, Any], symbol: str) -> dict[str, Any]:
    out: dict[str, Any] = {"symbol": symbol}
    for k in NUMERIC:
        a, b = lf.get(k), rf.get(k)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
            out[k] = (a, b)
    for k in CATEGORICAL:
        if k in lf:
            out[k] = (lf.get(k), rf.get(k))
    return out


def _rel(a: float, b: float) -> float:
    return abs(b - a) / max(abs(a), 1e-12)


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """results: [{"ts_ms", "args", "cmp": compare_scan(...)}]. Rates with day-clustered 95% intervals."""
    hits, days, present = [], [], []
    crit: dict[str, list[bool]] = {"tradable": [], "setups": [], "tape": [], "vetoes": []}
    crit_days: dict[str, list[str | None]] = {k: [] for k in crit}
    only_l: Counter[str] = Counter()   # vetoes only the logged scan had
    only_r: Counter[str] = Counter()   # ... only the replay had
    tape_pairs: Counter[str] = Counter()   # "logged -> replayed" tape states that differ
    for res in results:
        d = day_of(res["ts_ms"])
        for r in res["cmp"]["rows"]:
            hits.append(bool(r["match"]))
            days.append(d)
            present.append(r["in_replay"])
            if not r["in_replay"]:
                continue
            for k in crit:
                if r.get(k) is not None:
                    crit[k].append(bool(r[k]))
                    crit_days[k].append(d)
            only_l.update(r["vetoes_logged_only"])
            only_r.update(r["vetoes_replay_only"])
            if r["tape"] is False:
                tape_pairs[f"{r['tape_pair'][0]} -> {r['tape_pair'][1]}"] += 1
    uni = [res["cmp"]["universe"] for res in results]
    out = {"scans": len(results), "rows": len(hits),
           "match": clustered_wilson(hits, days), "in_replay": clustered_wilson(present, days),
           **{f"agree_{k}": clustered_wilson(v, crit_days[k]) for k, v in crit.items()},
           "universe_recall_mean": _mean([u["recall"] for u in uni]),
           "universe_precision_mean": _mean([u["precision"] for u in uni]),
           "universe_exact_share": _mean([1.0 if not u["missing"] and not u["extra"] else 0.0 for u in uni]),
           "tradable_jaccard_mean": _mean([res["cmp"]["tradable"]["jaccard"] for res in results]),
           "vetoes_logged_only": only_l.most_common(12), "vetoes_replay_only": only_r.most_common(12),
           "tape_confusion": tape_pairs.most_common(12), "features": feature_summary(results)}
    return out


def feature_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    num: dict[str, list[tuple[float, float]]] = {}
    cat: dict[str, list[bool]] = {}
    for res in results:
        for fd in res["cmp"]["features"]:
            for k, v in fd.items():
                if k in NUMERIC:
                    num.setdefault(k, []).append(v)
                elif k in CATEGORICAL:
                    cat.setdefault(k, []).append(v[0] == v[1])
    out: dict[str, Any] = {}
    for k, pairs in num.items():
        rel = [_rel(a, b) for a, b in pairs]
        out[k] = {"n": len(pairs), "median_rel_err": round(statistics.median(rel), 4),
                  "within_1pct": round(sum(x <= 0.01 for x in rel) / len(rel), 3),
                  "within_10pct": round(sum(x <= 0.10 for x in rel) / len(rel), 3)}
    for k, eq in cat.items():
        out[k] = {"n": len(eq), "agree": round(sum(eq) / len(eq), 3)}
    return out


def _mean(xs: list[float | None]) -> float | None:
    v = [x for x in xs if x is not None]
    return round(sum(v) / len(v), 4) if v else None
