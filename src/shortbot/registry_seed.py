"""Seed the pattern registry (registry.py) from what the desk already knows: tape states, lifecycle stages,
controller labels, setups, the week-study detectors (with their episodes), the forensics script catalog
(docs/SCRIPTS.md, data/scripts_catalog.json) and the 60-day backtest (trials + the selected variant's trades).
Idempotent: re-running adds only what is new; changed definitions become new versions."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import inspect

from . import registry as R

TAPE = {  # state -> (parent lifecycle-ish context, mechanism)
    "pump_in_progress": "late longs chase; shorts get squeezed", "liquidation_cascade": "forced sellers overshoot",
    "accepted_breakout": "stops above the level cascaded; trend continues", "spot_led_rally": "real spot demand",
    "attention_rising": "new eyes keep buying", "pre_unlock": "unlock recipients hedge / sell into the date",
    "hype_fade": "attention peaked; late buyers are the exit liquidity",
    "failed_sweep": "stops above the level ran, no follow-through: trapped breakout buyers",
    "post_pump_decay": "pump buyers underwater sell every bounce", "exhausting_push": "buying dries up into highs",
    "crowded_longs": "long-heavy positioning = forced sellers on a drop", "no_edge": "nothing forced"}
SCRIPT_LEARN = {"retail_short_fuel_pump": 16, "discount_squeeze": 16, "delisting_squeeze": 16,
                "abandonment_collapse": 16}
STRAT_LEARN = {"pump_fade": 17, "post_pump_decay": 17, "bounce_fade": 17, "level_fade": 17, "listing_fade": 15}


def _reg(db, **p: Any) -> None:
    R.register(db, {"status": "shadow", **p})


def _src(fn: Any) -> str:
    """The detector's own code: a threshold edit changes it (new version), a comment/evidence edit elsewhere does not."""
    try:
        return inspect.getsource(fn).strip()
    except (OSError, TypeError):
        return repr(fn)


SCAN_METRIC = "short_ret_24h_pct"   # what feed_candidates writes: only nodes with this metric receive scan episodes


def seed_static(db) -> int:
    from .setups import SETUPS
    from .signals.controller import LABELS, controller_labels
    from .signals.lifecycle import STAGES, stage_of
    from .tape import RULES
    n = 0
    for s in STAGES:
        _reg(db, id=f"lifecycle.{s}", level="lifecycle", name=s, definition=f"stage {s}\n{_src(stage_of)}",
             detector="signals.lifecycle", metric=SCAN_METRIC, source="docs/COIN_PROFILE.md")
        n += 1
    for lab in LABELS:
        _reg(db, id=f"controller.{lab}", level="controller", name=lab,
             definition=f"label {lab}\n{_src(controller_labels)}", detector="signals.controller", metric=SCAN_METRIC,
             source="docs/COIN_PROFILE.md")
        n += 1
    for i, rule in enumerate(RULES):   # rule order matters (first match wins), so it is part of the definition
        _reg(db, id=f"tape.{rule.state}", level="tape", name=rule.state, mechanism=TAPE.get(rule.state),
             definition=f"order {i}: {_src(rule.when)}", detector="tape.classify", metric=SCAN_METRIC,
             source="docs/TAPE.md")
        n += 1
    for s in SETUPS.values():
        _reg(db, id=f"setup.{s.name}", level="setup", name=s.name, definition=f"{s.description}\n{_src(s.detect)}",
             detector="setups.detect_all", metric=SCAN_METRIC, source="src/shortbot/setups.py")
        n += 1
    _reg(db, id="detector.pump_fade_discount_scan", level="detector", name="pump_fade at a discount (live scans)",
         definition="setups has pump_fade AND (raw funding_rate <= -0.1% OR funding interval 1h OR basis_bp <= -100); "
                    "scan-time features (backtest version: strategy.pump_fade+discount, R units)",
         detector="registry_seed.feed_candidates", metric=SCAN_METRIC, source="docs/failures/ROUND1.md")
    return n + 1


def seed_week_study(db, coins: list[dict[str, Any]]) -> int:
    from . import patterns as P
    from .patterns_detect import DEDUP_H, __doc__ as defs
    res = P.run(coins)
    n = 0
    for kind in DEDUP_H:
        line = next((ln.strip() for ln in (defs or "").splitlines() if ln.strip().startswith(kind)), kind)
        _reg(db, id=f"detector.{kind}", level="detector", name=kind, definition=line, detector="patterns_detect",
             metric="short_ret_24h_pct", source="docs/PATTERNS.md")
        rows = [{"symbol": e["symbol"], "ts_ms": e["ts"], "source": "week_study", "oos": False,
                 "outcome": e["ret_24h"], "win": e["ret_24h"] > 0.1, "mae_pct": e["mae_24h"],
                 "features": {k: e.get(k) for k in ("oi_bucket", "vol_bucket", "session", "leverage", "has_spot")}}
                for e in res["events"] if e["kind"] == kind]
        n += R.add_episodes(db, f"detector.{kind}", rows)
    return n


def seed_scripts(db, path: str | Path = "data/scripts_catalog.json") -> int:
    p = Path(path)
    if not p.exists():
        return 0
    eps = json.loads(p.read_text())["episodes"]
    n = 0
    for name in sorted({s for e in eps for s in e.get("scripts") or [e["script_primary"]]}):
        _reg(db, id=f"script.{name}", level="script", name=name, definition=f"docs/SCRIPTS.md: {name}",
             detector="forensics (manual label)", metric="short_ret_72h_pct_at_35pct_cross", source="docs/SCRIPTS.md")
        rows = []
        for e in eps:
            if name not in (e.get("scripts") or [e["script_primary"]]):
                continue
            x = e.get("short_at_35pct_cross") or {}
            ts = _ts(x.get("cross_utc") or (e.get("window_utc") or "")[:16])
            rows.append({"symbol": e["symbol"], "ts_ms": ts, "source": "forensics", "oos": False,
                         "outcome": x.get("net_72h_pct"), "win": None if x.get("net_72h_pct") is None
                         else x["net_72h_pct"] > 0, "mae_pct": x.get("mae_72h_pct"),
                         "features": {"kind": e.get("kind"), "move_pct": e.get("move_pct"), "id": e.get("id")}})
        n += R.add_episodes(db, f"script.{name}", rows)
        if name in SCRIPT_LEARN:
            R.link_learning(db, f"script.{name}", SCRIPT_LEARN[name])
    return n


def seed_backtest(db, results_path: str | Path, trades_path: str | Path) -> dict[str, int]:
    res = json.loads(Path(results_path).read_text())
    mid = res.get("mid") or 0
    for tname, t in res["trials"].items():
        strat = t["strategy"]
        _reg(db, id=f"strategy.{strat}", level="strategy", name=strat, definition=f"backtest/strategies*.py {strat}",
             detector="backtest", metric="trade_r", source="docs/BACKTEST.md")
        a, o = t.get("all") or {}, t.get("oos") or {}
        R.add_trial(db, f"strategy.{strat}", {"trial": tname, **(t.get("params") or {})}, "bt60",
                    {"n": a.get("n"), "n_coins": a.get("coins"), "mean": a.get("avg_r"), "t": t.get("t_vs_placebo"),
                     "oos_n": o.get("n"), "oos_mean": o.get("avg_r")})
        if strat in STRAT_LEARN:
            R.link_learning(db, f"strategy.{strat}", STRAT_LEARN[strat])
    chosen = {v["trial"] for v in (res.get("selected") or {}).values() if isinstance(v, dict) and v.get("trial")}
    trades = json.loads(Path(trades_path).read_text())
    by: dict[str, list[dict[str, Any]]] = {}
    for tr in trades:
        if tr.get("trial") in chosen and tr.get("filled", True) and tr.get("r") is not None:
            by.setdefault(tr["strategy"], []).append(
                {"symbol": tr["symbol"], "ts_ms": tr["entry_ts"], "source": "bt60", "oos": tr["entry_ts"] >= mid,
                 "outcome": tr["r"], "win": tr["r"] > 0, "mae_pct": tr.get("mae_pct"),
                 "features": {"trial": tr["trial"], "exit": tr.get("reason")}})
    return {s: R.add_episodes(db, f"strategy.{s}", rows) for s, rows in by.items()}


def _ts(s: str) -> int:
    try:
        return int(datetime.fromisoformat(s.strip().replace(" ", "T") + "+00:00").timestamp() * 1000)
    except ValueError:
        return 0


def seed_all(journal, bt_dir: str | Path | None = None, week_coins: list | None = None) -> dict[str, Any]:
    db = journal.db
    R.ensure(db)
    out: dict[str, Any] = {"static": seed_static(db), "scripts": seed_scripts(db)}
    if week_coins:
        out["week_study_episodes"] = seed_week_study(db, week_coins)
    if bt_dir and Path(bt_dir, "results.json").exists():
        out["backtest_episodes"] = seed_backtest(db, Path(bt_dir, "results.json"), Path(bt_dir, "trades.json"))
    out["lessons_indexed"] = R.reindex_lessons(db)
    return out


def _discount(f: dict[str, Any]) -> bool:
    """Same rule as the backtest's discount state, on the scan-time features (the 24h-min premium isn't logged, so
    the current basis stands in for it). Missing values never count as a discount."""
    rate, iv, basis = f.get("funding_rate"), f.get("funding_interval_h"), f.get("basis_bp")
    return bool((rate is not None and rate <= -0.001) or iv == 1 or (basis is not None and basis <= -100))


def feed_candidates(journal, limit: int = 5000) -> int:
    """Live episodes: every labelled scan candidate (72h outcome known) becomes an episode of the hierarchy nodes
    it sat in AT SCAN TIME. Only candidates scanned after the node's current version was registered count (older
    ones were classified by an older rule), and only nodes whose metric is the scan metric (% short return)."""
    from .tape import classify
    R.ensure(journal.db)
    n = 0
    by: dict[str, list[dict[str, Any]]] = {}
    for c in journal.candidates(labelled_only=True, limit=limit):
        if c.get("short_ret_24h") is None:
            continue
        f = c.get("features") or {}
        tape_state = f.get("tape_state") or (classify(f)["state"] if f else None)   # logged at scan time since #91
        nodes = [f"lifecycle.{f.get('lifecycle_stage')}" if f.get("lifecycle_stage") else None,
                 f"tape.{tape_state}" if tape_state else None,
                 *(f"controller.{x}" for x in f.get("controller_labels") or []),
                 *(f"setup.{s}" for s in c.get("setups") or [])]
        if "pump_fade" in (c.get("setups") or []) and _discount(f):   # learning #19 shadow candidate, live
            nodes.append("detector.pump_fade_discount_scan")
        ep = {"symbol": c["symbol"], "ts_ms": c["ts_ms"], "source": "scan", "oos": True,
              "outcome": c["short_ret_24h"], "win": c["short_ret_24h"] > 0.1, "mae_pct": c.get("max_adverse_24h"),
              "features": {"regime": c.get("regime"), "tradable": c.get("tradable"), "score": c.get("score")}}
        for node in filter(None, nodes):
            by.setdefault(node, []).append(ep)
    for node, rows in by.items():
        p = R.latest(journal.db, node)
        if not p or p["metric"] != SCAN_METRIC:
            continue                                   # never mix units (R vs %) inside one node
        born = int(datetime.fromisoformat(p["created_at"]).timestamp() * 1000)
        n += R.add_episodes(journal.db, node, [r for r in rows if r["ts_ms"] >= born], version=p["version"])
    return n
