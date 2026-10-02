"""What each backtest-family study records in the trial register (#31, trial_register.py): pure functions from a
study's result to register rows. The (family, params) shapes match the rows the old journal ledger held, so a rerun
of a migrated trial counts once.

Every row is one trial the analyst saw: a parameter set, an exit variant, a hedge variant, a split cell. Controls
(placebo / random groups) are recorded with control=True and never counted. A row with a `daily` series
({UTC day: summed outcome}) can be clustered with correlated trials; without one it counts as its own family."""

from __future__ import annotations

from typing import Any

from .. import trial_register as TR
from .stats import clustered, sharpe


def daily(trades: list[dict], key: str = "r") -> dict[str, float]:
    out: dict[str, float] = {}
    for t in trades:
        out[t["day"]] = out.get(t["day"], 0.0) + t[key]
    return out


def book_row(trades: list[dict], **kw: Any) -> dict[str, Any]:
    """A book of trades (fields r, day, symbol) as a register row: pooled mean, two-way t, n_eff, daily series."""
    if not trades:
        return {"n": 0, "metric": "trade_r", **kw}
    rs = [t["r"] for t in trades]
    cl = clustered(trades)
    return {"n": len(rs), "days": len({t["day"] for t in trades}), "coins": len({t.get("symbol") for t in trades}),
            "mean": sum(rs) / len(rs), "se": cl["se_2way"], "t": cl["t_2way"], "t_kind": "2way", "sr": sharpe(rs),
            "n_eff": cl["n_eff"], "daily": daily(trades), "metric": "trade_r", **kw}


def split_cells(res: dict[str, Any], edges: list[float]) -> list[dict[str, Any]]:
    """Every feature-split cell report.md shows for the strategy group (pooled trades, n >= 8): {name, family, params,
    trades}. Placebo / random cells are controls. The per-strategy splits sit in results.json only (about 7,700 cells
    on the round-3 ledger, mostly clones and n 5-20 slivers): a filter read from them becomes a trial when it is run
    as a variant (as +discount was), and is counted then."""
    from .report import buckets, dedupe
    pooled = dedupe([t for r in res["trials"].values() for t in r["trades"] if "session" not in (r["params"] or {})])
    groups = {"pooled": (pooled, 8)}
    out = []
    for group, (trades, min_n) in groups.items():
        for feat, fn in buckets(edges).items():
            cells: dict[str, list[dict]] = {}
            for t in trades:
                try:
                    k = str(fn(t["f"]))
                except (TypeError, KeyError):
                    k = "n/a"
                cells.setdefault(k, []).append(t)
            out += [{"name": f"{group}|{feat}={k}", "family": f"split.{feat}",
                     "params": {"group": group, "feature": feat, "bucket": k}, "trades": v}
                    for k, v in sorted(cells.items()) if len(v) >= min_n]
    return out


def _trial_spec(name: str, r: dict[str, Any]) -> str:
    return TR.spec_of("backtest", *TR.backtest_row_key(r["strategy"], name, r.get("params") or {}))


def register_deflate(res: dict[str, Any], rows: dict[str, Any], selected: dict[str, Any], edges: list[float],
                     dsr_mode: str, asof: int | None = None) -> dict[str, Any]:
    """The DSR with N = the effective number of independent trials over the whole search: this run's trials and
    split cells plus every other trial in the register (its own earlier records excluded, so a rerun reproduces).
    Updates rows / selected in place (dsr = the register-wide one in clustered mode) and re-judges the verdicts."""
    from .report import verdict
    from .trials_eff import deflate
    cells = split_cells(res, edges)
    own = {_trial_spec(n, r) for n, r in res["trials"].items()}
    own |= {TR.spec_of("backtest.splits", c["family"], c["params"]) for c in cells}
    series, bare = TR.others(exclude=own, asof=asof)
    extra = {**series, **{f"cell:{c['name']}": daily(c["trades"]) for c in cells}}
    defl = deflate({n: r["trades"] for n, r in res["trials"].items()}, dsr_mode, others=(extra, bare))
    for name, v in rows.items():
        v.update({k: x for k, x in defl["per_trial"][name].items() if k in ("dsr", "sr0", "dsr_register",
                                                                           "sr0_register")})
    for s in selected.values():
        s.update({k: rows[s["trial"]].get(k) for k in ("dsr", "sr0", "dsr_register", "sr0_register")})
        s["verdict"] = verdict(s)
    return {"n_eff_register": defl.get("n_eff_register"),
            "register": {**TR.summary(asof=asof), "other_series": len(series), "other_bare": bare,
                         "split_cells": len(cells)}}


def backtest_rows(rep: dict[str, Any], res: dict[str, Any]) -> list[dict[str, Any]]:
    """Every trial of the ledger (zero-trade ones too) and every split cell shown in the report."""
    rows = []
    for name, r in res["trials"].items():
        fam, params = TR.backtest_row_key(r["strategy"], name, r.get("params") or {})
        rows.append(book_row(r["trades"], name=name, family=fam, params=params))
    for c in split_cells(res, rep.get("atr_tercile_edges") or [1.0, 2.0]):
        rows.append({**book_row(c["trades"], name=c["name"], family=c["family"], params=c["params"]),
                     "study": "backtest.splits"})
    return rows


def split_by_study(rows: list[dict[str, Any]], default: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        r = dict(r)
        out.setdefault(r.pop("study", default), []).append(r)
    return out


def record_backtest(rep: dict[str, Any], res: dict[str, Any], prov: dict | None = None, run_dir: Any = None) -> int:
    return sum(TR.record(study, rows, prov, run_dir)
               for study, rows in split_by_study(backtest_rows(rep, res), "backtest").items())


def hedge_rows(js: dict[str, Any]) -> list[dict[str, Any]]:
    """hedge_report.json: every (book, strategy, hedge variant) cell."""
    rows = []
    for book in ("strategies", "desk"):
        for s, sec in (js.get(book) or {}).items():
            for v, st in ((sec or {}).get("stats") or {}).items():
                rows.append({"name": f"{book}|{s}|{v}", "family": f"strategy.{s}" if s != "book" else "desk.book",
                             "params": {"book": book, "strategy": s, "variant": v}, "metric": "trade_r",
                             "n": st.get("n"), "coins": st.get("coins"), "mean": st.get("avg_r"),
                             "t": st.get("t_day"), "t_kind": "day", "sr": st.get("sr")})
    return rows


def trail5m_rows(st: dict[str, Any]) -> list[dict[str, Any]]:
    """trail5m.study: every exit variant at 1h and 5m, as its paired delta vs fixed exits."""
    rows = []
    for key, fam in st.items():
        for mode, one in (fam.get("modes") or {}).items():
            for lab, cell in one.items():
                d = cell.get("delta")
                if d:
                    rows.append({"name": f"{key}|{mode}|{lab}", "family": f"strategy.{key}",
                                 "params": {"exit": mode, "bars": lab}, "metric": "delta_r_vs_fixed", "n": d.get("n"),
                                 "mean": d.get("d"), "t": d.get("t"), "t_kind": "day"})
    return rows


def rungs_rows(res: dict[str, Any], study: str) -> list[dict[str, Any]]:
    """rungs.evaluate: every (group, rung variant) uplift; the random-entry group is a control."""
    rows = []
    for group, g in (res.get("eval") or {}).items():
        for v, cell in g.items():
            if isinstance(cell, dict):
                rows.append({"name": f"{group}|{v}", "family": "lib.liquidation_wick_harvest",
                             "params": {"group": group, "variant": v}, "control": group == "random",
                             "metric": "uplift_r", "n": g.get("n"), "mean": cell.get("uplift"), "t": cell.get("t_day"),
                             "t_kind": "day"})
    return rows


def card_rows(res: dict[str, Any]) -> list[dict[str, Any]]:
    """card_tests daily or hourly results, in the shapes the journal ledger held (source card_tests)."""
    rows = []

    def add(fam: str, params: dict, n: Any, mean: Any, t: Any = None, kind: str = "iid") -> None:
        rows.append({"name": f"{fam}|{params}", "family": fam, "params": params, "n": n, "mean": mean, "t": t,
                     "t_kind": kind})
    if z := res.get("funding_negative_zscore_long"):
        add("lib.funding_negative_zscore_long", {"rule": "z<=-1.5, exit z>0 or 30d"}, z.get("n"), z.get("mean"),
            z.get("t"))
    for fam, key, hs in (("lib.negative_funding_streak_long", "negative_funding_streak_long", ("30d", "60d")),
                         ("lib.cycle_low_accumulation", "cycle_low_accumulation", ("90d", "180d"))):
        for h in hs:
            if c := (res.get(key) or {}).get(h):
                add(fam, {"h": h}, c.get("n"), c.get("mean"), c.get("t"))
    if d := res.get("dominance_rotation_long"):
        add("lib.dominance_rotation_long", {"regime": "on-off"},
            (d.get("regime_on_top5") or {}).get("n", 0) + (d.get("regime_off_top5") or {}).get("n", 0),
            d.get("on_minus_off"))
    drift = res.get("funding_settlement_drift") or {}
    for key, w in (("hour_into_print_payer_loss", "into print"), ("two_hours_after_payer_loss", "2h after")):
        if c := drift.get(key):
            add("lib.funding_settlement_drift", {"window": w}, c.get("n"), c.get("mean"), c.get("t_day"), "day")
    for h, c in ((res.get("intraday_hour_seasonality") or {}).get("by_hour") or {}).items():
        mean = None if c.get("mean_bp") is None else c["mean_bp"] / 1e4
        add("lib.intraday_hour_seasonality", {"hour": int(h)}, c.get("n"), mean, c.get("t_day"), "day")
    for key in ("after_btc_up_1pct", "after_btc_down_1pct"):
        if c := (res.get("btc_alt_lead_lag") or {}).get(key):
            add("lib.btc_alt_lead_lag", {"after": key}, c.get("n"), c.get("mean"), c.get("t_day"), "day")
    return rows


def lifecycle_rows(coins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """lifecycle_report's short grid (entry day x hold): every cell the report shows (n >= 10)."""
    from .. import lifecycle_study as ls
    rows = []
    for entry in list(coins[0]["trades"]) if coins else []:
        for hold in ls.HOLDS:
            s = ls.trade_stats([c["trades"][entry][hold] for c in coins])
            if s["n"] >= 10:
                rows.append({"name": f"{entry}|{hold}d", "family": "lifecycle.short",
                             "params": {"entry": entry, "hold_d": hold}, "metric": "short_ret", "n": s["n"],
                             "mean": s["mean"]})
    return rows


def patterns_rows(rep: dict[str, Any]) -> list[dict[str, Any]]:
    """patterns.report: each detector vs its control, and each split cell shown."""
    rows = []
    for kind, k in (rep.get("kinds") or {}).items():
        rows.append({"name": kind, "family": f"detector.{kind}", "params": {"kind": kind},
                     "metric": "short_ret_24h_pct", "n": k.get("n"), "coins": k.get("n_coins"), "mean": k.get("short_ret_24h_mean"),
                     "t": k.get("t_vs_control"), "t_kind": "welch_vs_control"})
        for split, cells in k.items():
            if split.startswith("by_") and isinstance(cells, dict):
                for b, c in cells.items():
                    rows.append({"name": f"{kind}|{split}={b}", "family": f"detector.{kind}",
                                 "params": {"kind": kind, "split": split, "bucket": b}, "metric": "short_ret_24h_pct",
                                 "n": c.get("n"), "coins": c.get("n_coins"), "mean": c.get("short_ret_24h_mean")})
    return rows


def laggard_rows(rep: dict[str, Any]) -> list[dict[str, Any]]:
    """laggard.report: laggard minus control per horizon (mean of day means with its own t)."""
    return [{"name": h, "family": "lib.laggard", "params": {"h": h}, "metric": "short_ret_pct_vs_control",
             "n": c.get("events"), "days": c.get("days"), "mean": c.get("laggard_minus_control_short_ret_pct"),
             "t": c.get("t_by_day"), "t_kind": "day_means"}
            for h, c in rep.items() if h.endswith("h") and isinstance(c, dict)]


def ladder_rows(rep: dict[str, Any]) -> list[dict[str, Any]]:
    """shadow_eval.evaluate: each ladder variant vs holding (the ladder's old trial_ledger rows, same params)."""
    from ..ladder import VARIANTS
    return [{"name": v, "family": "ladder", "params": {"variant": v, **VARIANTS.get(v, {})},
             "control": bool(VARIANTS.get(v, {}).get("placebo")), "metric": "delta_r_vs_hold", "n": c.get("n_trades"),
             "mean": c.get("mean_delta_R_vs_hold"), "se": c.get("se"), "t": c.get("t"), "t_kind": "iid"}
            for v, c in (rep.get("vs_hold") or {}).items()]
