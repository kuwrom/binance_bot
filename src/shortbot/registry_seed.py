"""Seed the pattern registry (registry.py) from what the desk already knows: tape states, lifecycle stages,
controller labels, setups, the week-study detectors (with their episodes), the forensics script catalog
(docs/SCRIPTS.md, data/scripts_catalog.json) and the 60-day backtest (trials + the selected variant's trades).
Idempotent: re-running adds only what is new; changed definitions become new versions. Forward rows (labelled scans,
closed trades: feed_candidates) are rebuilt from the journal on every feed (registry_forward.py, #26)."""

from __future__ import annotations

import inspect
import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

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
    for setup in SETUPS.values():
        _reg(db, id=f"setup.{setup.name}", level="setup", name=setup.name,
             definition=f"{setup.description}\n{_src(setup.detect)}",
             detector="setups.detect_all", metric=SCAN_METRIC, source="src/shortbot/setups.py")
        n += 1
    _reg(db, id="detector.pump_fade_discount_scan", level="detector", name="pump_fade at a discount (live scans)",
         definition="setups has pump_fade AND (raw funding_rate <= -0.1% OR funding interval 1h OR basis_bp <= -100); "
                    "scan-time features (backtest version: strategy.pump_fade+discount, R units)",
         detector="registry_seed.feed_candidates", metric=SCAN_METRIC, source="docs/failures/ROUND1.md")
    return n + 1


def seed_week_study(db, coins: list[dict[str, Any]]) -> int:
    from . import patterns as P
    from .patterns_detect import DEDUP_H
    from .patterns_detect import __doc__ as defs
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
        R.add_trial(f"strategy.{strat}", {"trial": tname, **(t.get("params") or {})}, "bt60",
                    {"n": a.get("n"), "n_coins": a.get("coins"), "mean": a.get("avg_r"), "t": a.get("t_2way"),
                     "t_kind": "2way", "oos_n": o.get("n"), "oos_mean": o.get("avg_r")})
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
    from .library_seed import seed_library
    out: dict[str, Any] = {"static": seed_static(db), "scripts": seed_scripts(db), "library": seed_library(db)}
    if week_coins:
        out["week_study_episodes"] = seed_week_study(db, week_coins)
    if bt_dir and Path(bt_dir, "results.json").exists():
        out["backtest_episodes"] = seed_backtest(db, Path(bt_dir, "results.json"), Path(bt_dir, "trades.json"))
    out["lessons_indexed"] = R.reindex_lessons(db)            # pattern definitions
    out["learnings_indexed"] = journal.rebuild_learnings_index()   # kept current by triggers: this repairs drift
    return out


def _discount(f: dict[str, Any]) -> bool:
    """Same rule as the backtest's discount state, on the scan-time features (the 24h-min premium isn't logged, so
    the current basis stands in for it). Missing values never count as a discount."""
    rate, iv, basis = f.get("funding_rate"), f.get("funding_interval_h"), f.get("basis_bp")
    return bool((rate is not None and rate <= -0.001) or iv == 1 or (basis is not None and basis <= -100))


def scan_nodes(c: dict[str, Any]) -> set[str]:
    """The hierarchy nodes a scan candidate sat in AT SCAN TIME (tape state logged at scan time since #91)."""
    from .tape import classify
    f, setups = c.get("features") or {}, c.get("setups") or []
    tape_state = f.get("tape_state") or (classify(f)["state"] if f else None)
    nodes = {f"lifecycle.{f.get('lifecycle_stage')}" if f.get("lifecycle_stage") else None,
             f"tape.{tape_state}" if tape_state else None,
             *(f"controller.{x}" for x in f.get("controller_labels") or []), *(f"setup.{s}" for s in setups)}
    if "pump_fade" in setups and _discount(f):           # learning #19 shadow candidate, live
        nodes.add("detector.pump_fade_discount_scan")
    return {n for n in nodes if n is not None}


def _targets(db, feed: str) -> dict[str, tuple[int, int]]:
    """node -> (latest version, forward start) for the latest versions whose frozen plan takes this feed."""
    from . import registry_forward as F
    out = {}
    for (pid,) in db.execute("SELECT DISTINCT id FROM patterns"):
        if (p := R.latest(db, pid)) is None:   # (an id read from the table: only if deleted meanwhile)
            continue
        pl = F.plan_of(db, pid, p["version"])
        if pl and pl["plan"].get("feed") == feed:
            out[pid] = (p["version"], pl["registered_ms"])
    return out


def _replace(db, pid: str, version: int, source: str, rows: list[dict[str, Any]]) -> int:
    """Rewrite a node's forward rows of one source (relabels and late labels propagate; never INSERT OR IGNORE's
    stale first outcome). Returns the net change in rows."""
    q = "SELECT COUNT(*) FROM pattern_episodes WHERE pattern_id=? AND version=? AND source=?"
    before = db.execute(q, (pid, version, source)).fetchone()[0]
    db.execute("DELETE FROM pattern_episodes WHERE pattern_id=? AND version=? AND source=?", (pid, version, source))
    R.add_episodes(db, pid, rows, version=version)                    # one transaction with the delete
    return db.execute(q, (pid, version, source)).fetchone()[0] - before


def _median(xs: dict[str, float]) -> float | None:
    from .research_stats import MIN_SCAN_SYMBOLS
    return statistics.median(xs.values()) if len(xs) >= MIN_SCAN_SYMBOLS else None


def feed_candidates(journal) -> int:
    """Forward episodes (registry_forward.py, #26): every labelled scan candidate scanned after a node's frozen plan
    becomes, once per symbol-episode (rescans within 24h are the same bet), a `scan` episode of the nodes it sat in
    AT SCAN TIME, scored against the same scan's unflagged coins after costs (features.adj); then closed trades
    (feed_trades). Each node's forward rows are rebuilt on every call. Returns the net change in rows."""
    from .research import COST_PCT, HORIZON_H, funding_carry_24h
    from .research_stats import episodes
    db = journal.db
    R.ensure(db)
    targets = _targets(db, "scan")
    n = 0
    if targets:
        rows = []
        for r in db.execute("SELECT symbol, ts_ms, short_ret_24h, max_adverse_24h, features, setups, regime, tradable,"
                            " score FROM candidates WHERE labelled_at IS NOT NULL AND short_ret_24h IS NOT NULL AND"
                            " ts_ms >= ? ORDER BY ts_ms, id", (min(t for _, t in targets.values()),)):
            c = {"symbol": r[0], "ts_ms": r[1], "ret": r[2], "adv": r[3], "features": json.loads(r[4] or "{}"),
                 "setups": json.loads(r[5] or "[]"), "regime": r[6], "tradable": r[7], "score": r[8]}
            c["nodes"] = scan_nodes(c)
            rows.append(c)
        by_scan: dict[int, list[dict[str, Any]]] = {}
        for c in rows:
            by_scan.setdefault(c["ts_ms"], []).append(c)
        mkt = {ts: _median({c["symbol"]: c["ret"] for c in v}) for ts, v in by_scan.items()}
        for node, (ver, since) in targets.items():
            eps = []
            for e in episodes([c for c in rows if node in c["nodes"] and c["ts_ms"] >= since], HORIZON_H):
                ctl = _median({c["symbol"]: c["ret"] for c in by_scan[e["ts_ms"]] if node not in c["nodes"]})
                net = e["ret"] - COST_PCT + funding_carry_24h(e["features"])
                eps.append({"symbol": e["symbol"], "ts_ms": e["ts_ms"], "source": "scan", "oos": True,
                            "outcome": e["ret"], "win": e["ret"] > 0.1, "mae_pct": e["adv"], "features": {
                                "regime": e["regime"], "tradable": e["tradable"], "score": e["score"], "net": net,
                                "vs_market": None if mkt[e["ts_ms"]] is None else e["ret"] - mkt[e["ts_ms"]],
                                "vs_control": None if ctl is None else e["ret"] - ctl,
                                "adj": None if ctl is None else net - ctl,
                                "control_n": sum(node not in c["nodes"] for c in by_scan[e["ts_ms"]])}})
            n += _replace(db, node, ver, "scan", eps)
    return n + feed_trades(journal)


def _trade_market_r(db, t: dict[str, Any], opened_ms: int, hold_h: float) -> tuple[float | None, int]:
    """The market's drift over the hold, in the trade's R: median 24h short return of the coins without the trade's
    setup in the last scan within 6h before entry, x hold / 24h, / the % move that is 1R. None until labelled."""
    scan = db.execute("SELECT MAX(ts_ms) FROM candidates WHERE ts_ms <= ? AND ts_ms >= ?",
                      (opened_ms, opened_ms - 6 * 3_600_000)).fetchone()[0]
    rows = db.execute("SELECT symbol, short_ret_24h, setups FROM candidates WHERE ts_ms=? AND short_ret_24h IS NOT"
                      " NULL", (scan,)).fetchall() if scan is not None else []
    ctl = {s: r for s, r, st in rows if s != t["symbol"] and t["setup"] not in json.loads(st or "[]")}
    size = (t.get("entry_price") or 0) * (t.get("amount") or 0)
    med, one_r_pct = _median(ctl), (t["risk_usdt"] / size * 100 if size else None)
    return (None if med is None or not one_r_pct else med * hold_h / 24 / one_r_pct), len(ctl)


def feed_trades(journal) -> int:
    """Closed trades opened after a strategy node's frozen plan become its `live` episodes (setup -> node as in
    read.SETUP_STRATEGY): outcome = net R (exchange truth: fees and funding included), adj = R - market drift."""
    from . import registry_forward as F
    db = journal.db
    targets = _targets(db, "trades")
    if not targets:
        return 0
    by: dict[str, list[dict[str, Any]]] = {node: [] for node in targets}
    for t in journal.closed_results(all_modes=True):      # demo and live fills are both market evidence
        node = F.setup_strategy().get(t.get("setup") or "")
        if node not in targets or t.get("r_multiple") is None or not t.get("risk_usdt"):
            continue
        opened = int(datetime.fromisoformat(t["opened_at"]).timestamp() * 1000)
        if opened < targets[node][1]:
            continue
        hold_h = (datetime.fromisoformat(t["closed_at"]).timestamp() * 1000 - opened) / 3_600_000
        mkt, n_ctl = _trade_market_r(db, t, opened, hold_h)
        by[node].append({"symbol": t["symbol"], "ts_ms": opened, "source": "live", "oos": True,
                         "outcome": t["r_multiple"], "win": t["r_multiple"] > 0, "mae_pct": t.get("mae_pct"),
                         "features": {"trade_id": t["id"], "hold_h": round(hold_h, 2), "market_r": mkt,
                                      "adj": None if mkt is None else t["r_multiple"] - mkt, "control_n": n_ctl}})
    return sum(_replace(db, node, targets[node][0], "live", rows) for node, rows in by.items())
