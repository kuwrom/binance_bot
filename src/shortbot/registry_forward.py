"""Forward-only, pre-registered scoring (#26, docs/REGISTRY.md "Forward test"): the only evidence that can promote a
pattern to live, and the evidence its decay is judged on.

- Plan: every pattern version freezes, at registration, its metric, target sample and decision rule, plus a hash of
  the whole spec (pattern_plans; the table refuses UPDATE/DELETE). Changing any of it is a new version: a new
  hypothesis with a new date. Versions older than #26 get the default plan frozen at backfill, so their clock starts
  then, never at a date when outcomes were already labelled (Nosek et al. 2018: prediction vs postdiction).
- Data: only `scan` / `live` episodes newer than the plan (registry_seed.feed_candidates: scan symbol-episodes and
  closed trades). Backtest / week-study rows, rebuilt weekly from hist.db, never enter.
- Metric (baseline- and cost-adjusted, `adj`): scan = 24h short return - 0.1% round trip + funding carry - median of
  the same scan's unflagged coins; trades = net R (exchange truth) - the control's drift over the hold, in R.
- Decision: once, on the first target_n observations that span min_days UTC days (a stopping point that never
  depends on outcomes, so peeking costs nothing; Miller 2010). One-sided day-clustered t (CV3, t(days-1)); pass iff
  p <= alpha / M, M = forward-decidable non-draft versions registered so far (Bonferroni; HLZ 2016). Before that,
  the interim mean is shown without an interval. A failed version stays failed: re-register to test again (M + 1).
- Decay: last-90-day forward rows, day-clustered 95% CI (finding F: the coin-clustered CI hid day-level fades).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from . import research_stats as rs

ALPHA = 0.025                  # one-sided, family-wise: the two-sided 95% convention
DAY_MS = 86_400_000
SCAN_METRIC = "short_ret_24h_pct"
SCAN_PREFIXES = ("lifecycle.", "tape.", "controller.", "setup.")
SCAN_DETECTORS = ("detector.pump_fade_discount_scan",)
FORWARD_SOURCES = ("scan", "live")
MIN_TARGET_N = 30
DECAY_WINDOW_MS, DECAY_MIN_DAYS, RETIRE_MIN_DAYS = 90 * DAY_MS, 10, 20
RULE = ("decide once, on the first target_n forward observations that span >= min_days UTC days: pass iff the "
        "one-sided p of mean adj > 0 (day-clustered CV3 se, t(days-1)) <= alpha / M, M = forward-decidable "
        "non-draft versions at evaluation")
PLANS = {
    "scan": {"metric": "net_excess_24h_pct", "unit": "symbol-episode (24h; rescans within 24h are one)",
             "control": "median short_ret_24h of the same scan's candidates outside the node (>= 5 symbols)",
             "costs": "0.1% round trip; + scan-time funding carry over 24h", "target_n": 100, "min_days": 20},
    "trades": {"metric": "net_r_minus_market_r", "unit": "closed trade, attributed by its setup",
               "control": "median short_ret_24h of the entry scan's coins without the setup, x hold / 24h, in R",
               "costs": "exchange truth: fees and funding are in the pnl", "target_n": 30, "min_days": 20}}


def setup_strategy() -> dict[str, str]:
    from .read import SETUP_STRATEGY    # setup -> strategy node: the same map read_coin's registry gate uses
    return SETUP_STRATEGY


def feed_of(pid: str, metric: str | None) -> str | None:
    if metric == SCAN_METRIC and (pid.startswith(SCAN_PREFIXES) or pid in SCAN_DETECTORS):
        return "scan"
    if metric == "trade_r" and pid in setup_strategy().values():
        return "trades"
    return None


def make_plan(pid: str, metric: str | None, override: dict[str, Any] | None = None) -> dict[str, Any]:
    """The default plan of a node, with optional target_n / min_days overrides (floors: 30 obs, 20 days)."""
    feed = feed_of(pid, metric)
    if not feed:
        return {"v": 1, "metric": metric, "feed": None, "decidable": False,
                "rule": "no forward feed for this node: not promotable; re-register it with a feed"}
    plan = {"v": 1, "feed": feed, "decidable": True, **PLANS[feed], "alpha": ALPHA, "rule": RULE}
    for k in ("target_n", "min_days"):
        if override and override.get(k) is not None:
            plan[k] = int(override[k])
    if plan["target_n"] < MIN_TARGET_N or plan["min_days"] < rs.MIN_DAYS:
        raise ValueError(f"plan floors: target_n >= {MIN_TARGET_N}, min_days >= {rs.MIN_DAYS}")
    return plan


def spec_hash(p: dict[str, Any], plan: dict[str, Any]) -> str:
    raw = json.dumps([p["id"], p.get("level"), p.get("definition"), p.get("detector"), p.get("metric"), plan],
                     sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def freeze(db, p: dict[str, Any], version: int, plan: dict[str, Any], at: str) -> None:
    """Write a version's plan once (concurrent seeders: the first one wins). The caller commits."""
    db.execute("INSERT OR IGNORE INTO pattern_plans VALUES (?,?,?,?,?)",
               (p["id"], version, at, spec_hash(p, plan), json.dumps(plan, sort_keys=True)))


def plan_of(db, pid: str, version: int) -> dict[str, Any] | None:
    r = db.execute("SELECT registered_at, spec_hash, plan FROM pattern_plans WHERE pattern_id=? AND version=?",
                   (pid, version)).fetchone()
    if not r:
        return None
    return {"registered_at": r[0], "registered_ms": int(datetime.fromisoformat(r[0]).timestamp() * 1000),
            "spec_hash": r[1], "plan": json.loads(r[2])}


def freeze_missing(db, at: str | None = None) -> int:
    """Versions registered before #26 get their default plan, frozen now: their forward clock starts here."""
    from .registry import _now
    cur = db.execute("SELECT * FROM patterns p WHERE NOT EXISTS (SELECT 1 FROM pattern_plans q WHERE"
                     " q.pattern_id=p.id AND q.version=p.version)")
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    at = at or _now()
    for p in rows:
        freeze(db, p, p["version"], make_plan(p["id"], p["metric"]), at)
    db.commit()
    return len(rows)


def family(db) -> dict[str, Any]:
    """M: forward tests registered so far (non-draft versions with a decidable plan, retired and superseded ones
    included). Exploratory trials (pattern_trials) are not in M: forward data is new to every one of them, and the
    backtest's deflated Sharpe already accounts for them at selection."""
    m = db.execute("SELECT COUNT(*) FROM patterns p JOIN pattern_plans q ON q.pattern_id=p.id AND q.version=p.version"
                   " WHERE p.status<>'draft' AND json_extract(q.plan, '$.decidable')").fetchone()[0]
    trials = db.execute("SELECT COUNT(*) FROM pattern_trials").fetchone()[0]
    return {"M": max(m, 1), "alpha": ALPHA, "alpha_per_test": ALPHA / max(m, 1), "trials_logged": trials}


def _obs(db, pid: str, version: int, since_ms: int) -> list[dict[str, Any]]:
    q = ("SELECT symbol, ts_ms, outcome, json_extract(features, '$.adj'), source FROM pattern_episodes WHERE"
         f" pattern_id=? AND version=? AND source IN {FORWARD_SOURCES} AND ts_ms >= ? ORDER BY ts_ms, symbol")
    return [{"symbol": s, "ts_ms": t, "raw": o, "adj": a, "source": src}
            for s, t, o, a, src in db.execute(q, (pid, version, since_ms))]


def _r(x: float | None, n: int = 3) -> float | None:
    return None if x is None else round(x, n)


def _test(sample: list[dict[str, Any]], fam: dict[str, Any], alpha: float) -> dict[str, Any]:
    c = rs.clustered_mean([o["adj"] for o in sample], rs.days_of(sample))
    t = c["mean"] / c["se"] if c.get("se") else None
    df = c["days"] - 1
    thr = alpha / fam["M"]
    p = rs.t_sf(t, df) if t is not None else 1.0
    return {"n": c["n"], "days": c["days"], "mean": _r(c["mean"]), "se": _r(c["se"]), "t": _r(t, 2), "df": df,
            "p": float(f"{p:.3g}"), "M": fam["M"], "alpha_per_test": thr, "t_crit": rs.t_ppf(1 - thr, df),
            "sample_end": rs.day_of(sample[-1]["ts_ms"]), "decision": "pass" if p <= thr else "fail"}


def forward(db, pid: str, version: int, fam: dict[str, Any] | None = None) -> dict[str, Any]:
    pl = plan_of(db, pid, version)
    if not pl:
        return {"status": "unplanned", "note": "registered before #26: the next feed freezes a default plan"}
    plan = pl["plan"]
    out = {"registered_at": pl["registered_at"], "spec_hash": pl["spec_hash"], "metric": plan["metric"]}
    if not plan.get("decidable"):
        return {**out, "status": "no_feed"}
    obs = [o for o in _obs(db, pid, version, pl["registered_ms"]) if o["adj"] is not None]
    need, days, k = (plan["target_n"], plan["min_days"]), set(), None
    for i, o in enumerate(obs):
        days.add(rs.day_of(o["ts_ms"]))
        if i + 1 >= need[0] and len(days) >= need[1]:
            k = i + 1
            break
    out["target"] = {"n": need[0], "days": need[1]}
    if k is None:
        mean = sum(o["adj"] for o in obs) / len(obs) if obs else None
        return {**out, "status": "collecting", "interim": {
            "n": len(obs), "days": len(days), "mean": _r(mean),
            "note": "interim: not a test (Miller 2010). The decision waits for the fixed sample; no interval yet"}}
    t = _test(obs[:k], fam or family(db), plan["alpha"])
    return {**out, "status": "decided", "decision": t["decision"], "test": t}


def recent(db, pid: str, version: int, now_ms: int) -> dict[str, Any] | None:
    """Last 90 days of forward rows, day-clustered (decay). Adjusted values when the feed wrote them; a node with none
    (hand-fed or pre-#26 live rows) is judged on its raw outcomes, still clustered by day."""
    obs = _obs(db, pid, version, now_ms - DECAY_WINDOW_MS)
    adj = [o for o in obs if o["adj"] is not None]
    use, key = (adj, "adj") if adj else (obs, "raw")
    if not use:
        return None
    c = rs.clustered_mean([o[key] for o in use], rs.days_of(use))
    return {"n": c["n"], "days": c["days"], "mean": _r(c["mean"]), "basis": key,
            "ci95": [_r(c["ci95"][0]), _r(c["ci95"][1])] if c["ci95"] else None}


def verdict(status: str, fwd: dict[str, Any] | None, rec: dict[str, Any] | None) -> str:
    if status == "live" and rec and rec["days"] >= DECAY_MIN_DAYS and rec["ci95"] and rec["ci95"][0] <= 0:
        return "probation: last-90d forward CI (day-clustered) crosses 0"
    if status == "probation" and rec and rec["days"] >= RETIRE_MIN_DAYS and rec["mean"] < 0:
        return "retire: edge gone"
    if status not in ("draft", "shadow") or not fwd:
        return "keep " + status
    st = fwd["status"]
    if st == "decided" and fwd["decision"] == "pass":
        return "eligible for live"
    if st == "decided":
        t = fwd["test"]
        return (f"keep {status}: forward test failed (p {t['p']:.3g} > {t['alpha_per_test']:.3g} = alpha/{t['M']});"
                " a new version starts a new test")
    if st == "collecting":
        i, n = fwd["interim"], fwd["target"]
        return f"keep {status}: forward {i['n']}/{n['n']} obs, {i['days']}/{n['days']} days"
    return f"keep {status}: " + ("no forward feed (not promotable)" if st == "no_feed" else "no frozen plan yet")


def report(db, pid: str | None = None) -> dict[str, Any]:
    """Every non-draft pattern's forward status (or one pattern's), with the family the decisions are corrected for."""
    fam = family(db)
    q = ("SELECT id, version, status FROM patterns p WHERE version=(SELECT MAX(version) FROM patterns x"
         " WHERE x.id=p.id)"
         + (" AND id=?" if pid else " AND status<>'draft'") + " ORDER BY id")
    nodes, no_feed = [], []
    for i, v, st in db.execute(q, (pid,) if pid else ()):
        f = forward(db, i, v, fam)
        if f["status"] == "no_feed" and not pid:
            no_feed.append(i)
            continue
        nodes.append({"id": i, "v": v, "status": st, "forward": f, "plan": (plan_of(db, i, v) or {}).get("plan")})
    return {"family": fam, "nodes": nodes, "no_forward_feed": no_feed,
            "how_to_read": "Only forward evidence promotes: episodes and trades newer than the frozen plan, scored "
                           "vs the same scan's unflagged coins after costs (adj). Each version is decided once, on "
                           "its first target_n observations spanning min_days days, at alpha / M. interim = "
                           "progress, not a test."}


def summary(db) -> dict[str, Any]:
    """report() for research_report: every non-draft node's forward status; each plan is left to its card."""
    from .registry import ensure
    ensure(db)
    r = report(db)
    return {**r, "nodes": [{k: v for k, v in n.items() if k != "plan"} for n in r["nodes"]]}
