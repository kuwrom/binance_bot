"""Pattern registry (learning #14, docs/REGISTRY.md): every tape state, script, setup, detector, strategy and filter
is a versioned record with a status, its episodes (as-of features + outcome) and every trial ever run on it.

    status: draft -> shadow -> live -> probation -> retired   (retired rows stay: they still count as trials)

- a changed definition / detector / metric / plan = a NEW version (old episodes stay with the old version)
- every version freezes a plan at registration (metric, target sample, decision rule, spec hash: pattern_plans)
- promotion to live needs a passed FORWARD test (registry_forward.py, #26): episodes and trades newer than the plan,
  baseline- and cost-adjusted, decided once on a fixed sample at alpha / M. Backtest "OOS" rows never promote.
  Probation / retirement when the last-90-day forward CI (day-clustered) crosses zero (decay alarm)
- a new pattern whose episodes mostly coincide with an existing one's is flagged as a duplicate
Evidence cards summarise a node for `read_coin`; lessons are full-text searchable (FTS5)."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

from . import cluster_se as C
from . import registry_forward as F
from . import research_stats as rs

STATUSES = ("draft", "shadow", "live", "probation", "retired")
LEVELS = ("regime", "lifecycle", "tape", "controller", "script", "setup", "detector", "strategy", "filter", "veto")
DUP_OVERLAP = 0.7
DAY_MS = 86_400_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS patterns (
    id TEXT NOT NULL, version INTEGER NOT NULL, level TEXT NOT NULL, parent TEXT, name TEXT, mechanism TEXT,
    definition TEXT, detector TEXT, metric TEXT, status TEXT NOT NULL, source TEXT, fingerprint TEXT,
    created_at TEXT, updated_at TEXT, status_reason TEXT, PRIMARY KEY (id, version));
CREATE TABLE IF NOT EXISTS pattern_episodes (
    id INTEGER PRIMARY KEY, pattern_id TEXT NOT NULL, version INTEGER NOT NULL, symbol TEXT, ts_ms INTEGER,
    source TEXT, oos INTEGER DEFAULT 0, outcome REAL, win INTEGER, mae_pct REAL, features TEXT,
    UNIQUE (pattern_id, version, symbol, ts_ms, source));
CREATE TABLE IF NOT EXISTS pattern_trials (
    id INTEGER PRIMARY KEY, pattern_id TEXT NOT NULL, version INTEGER NOT NULL, params TEXT, source TEXT,
    n INTEGER, n_coins INTEGER, mean REAL, t REAL, oos_n INTEGER, oos_mean REAL, created_at TEXT,
    UNIQUE (pattern_id, version, params, source));
CREATE TABLE IF NOT EXISTS pattern_plans (
    pattern_id TEXT NOT NULL, version INTEGER NOT NULL, registered_at TEXT NOT NULL, spec_hash TEXT NOT NULL,
    plan TEXT NOT NULL, PRIMARY KEY (pattern_id, version));
CREATE TRIGGER IF NOT EXISTS pattern_plans_no_update BEFORE UPDATE ON pattern_plans
    BEGIN SELECT RAISE(ABORT, 'pattern plans are frozen: register a new version'); END;
CREATE TRIGGER IF NOT EXISTS pattern_plans_no_delete BEFORE DELETE ON pattern_plans
    BEGIN SELECT RAISE(ABORT, 'pattern plans are frozen: register a new version'); END;
CREATE TABLE IF NOT EXISTS pattern_learnings (pattern_id TEXT, learning_id INTEGER, PRIMARY KEY (pattern_id, learning_id));
CREATE VIRTUAL TABLE IF NOT EXISTS lessons_fts USING fts5(kind, ref, text);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(db) -> None:
    db.executescript(SCHEMA)


def _fp(p: dict[str, Any]) -> str:
    raw = json.dumps([p.get("definition"), p.get("detector"), p.get("metric")], sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def latest(db, pid: str) -> dict[str, Any] | None:
    ensure(db)
    cur = db.execute("SELECT * FROM patterns WHERE id=? ORDER BY version DESC LIMIT 1", (pid,))
    row = cur.fetchone()
    return dict(zip([c[0] for c in cur.description], row)) if row else None


def _same_plan(db, cur: dict[str, Any], plan: dict[str, Any]) -> bool:
    frozen = F.plan_of(db, cur["id"], cur["version"])
    return (frozen["plan"] if frozen else F.make_plan(cur["id"], cur["metric"])) == plan


def register(db, p: dict[str, Any]) -> dict[str, Any]:
    """Insert a pattern, or a new version when its definition/detector/metric changed, or when an explicit
    p["plan"] ({target_n, min_days}) differs from the frozen one. Same spec = no-op (metadata such as
    name/mechanism/source is refreshed in place). A new version freezes its plan with its registration time."""
    ensure(db)
    if p["level"] not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    fp, cur = _fp(p), latest(db, p["id"])
    plan = F.make_plan(p["id"], p.get("metric"), p.get("plan"))
    now = _now()
    if cur and cur["fingerprint"] == fp and (p.get("plan") is None or _same_plan(db, cur, plan)):
        db.execute("UPDATE patterns SET name=?, mechanism=?, parent=?, source=?, updated_at=? WHERE id=? AND version=?",
                   (p.get("name"), p.get("mechanism"), p.get("parent"), p.get("source"), now, p["id"], cur["version"]))
        db.commit()
        return {"id": p["id"], "version": cur["version"], "new": False}
    ver = (cur["version"] + 1) if cur else 1
    status = p.get("status") or "draft"
    if status == "live":
        raise ValueError("register as draft/shadow; promote to live with set_status once the evidence exists")
    db.execute("INSERT OR IGNORE INTO patterns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",   # concurrent seeders
               (p["id"], ver, p["level"], p.get("parent"), p.get("name"), p.get("mechanism"), p.get("definition"),
                p.get("detector"), p.get("metric"), status, p.get("source"), fp, now, now, "registered"))
    F.freeze(db, p, ver, plan, now)
    db.commit()
    return {"id": p["id"], "version": ver, "new": True}


def add_episodes(db, pid: str, rows: list[dict[str, Any]], version: int | None = None) -> int:
    """rows: {symbol, ts_ms, source, oos?, outcome (R or % per the metric), win?, mae_pct?, features?}."""
    ver = version or (latest(db, pid) or {}).get("version")
    if not ver:
        raise ValueError(f"unknown pattern {pid}")
    before = db.total_changes
    db.executemany(
        "INSERT OR IGNORE INTO pattern_episodes (pattern_id, version, symbol, ts_ms, source, oos, outcome, win,"
        " mae_pct, features) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(pid, ver, r.get("symbol"), int(r.get("ts_ms") or 0), r.get("source", "unknown"), int(bool(r.get("oos"))),
          r.get("outcome"), None if r.get("win") is None else int(bool(r["win"])), r.get("mae_pct"),
          json.dumps(r.get("features") or {}, default=str)) for r in rows])
    db.commit()
    return db.total_changes - before


def add_trial(pid: str, params: dict[str, Any], source: str, stats: dict[str, Any]) -> None:
    """A trial of pattern `pid` into the one trial register (#31, trial_register.py). The journal's pattern_trials
    is legacy: migrated into the register once, never written again."""
    from . import trial_register as TR
    TR.record(TR.LEGACY_STUDY.get(source, source), [
        {"name": params.get("trial") or pid, "family": pid, "params": params, "n": stats.get("n"),
         "coins": stats.get("n_coins"), "mean": stats.get("mean"), "t": stats.get("t"), "t_kind": stats.get("t_kind")}])


def trial_count(db, pid: str) -> int:
    """Trials of a pattern: the register's count, or the legacy table's when the register lacks them (an
    unmigrated or absent register, e.g. on the AWS guardian). Never raises on a missing register."""
    from . import trial_register as TR
    legacy = db.execute("SELECT COUNT(*) FROM pattern_trials WHERE pattern_id=?", (pid,)).fetchone()[0]
    return max(TR.count(pid), legacy)


def link_learning(db, pid: str, learning_id: int) -> None:
    ensure(db)
    db.execute("INSERT OR IGNORE INTO pattern_learnings VALUES (?,?)", (pid, learning_id))
    db.commit()


# ------------------------------------------------------------------ evidence
def _stats(eps: list[tuple]) -> dict[str, Any] | None:
    """eps: (symbol, outcome, win, mae, ts_ms). Descriptive: the pooled mean of raw outcomes with its coin-clustered
    CI and t, and its day-clustered t (scan outcomes of one sell-off week are one market move, not 30 independent
    coins). CV3 floored at iid, t(G-1) (research_stats). #31: before, mean and t were over equal-weighted coin / day
    means, not the pooled mean."""
    xs = [e for e in eps if e[1] is not None]
    if not xs:
        return None
    vals = [e[1] for e in xs]
    c = rs.clustered_mean(vals, [e[0] for e in xs])
    m, se = c["mean"], c["se"]
    t_day, _ = C.one_way_t(vals, [e[4] // DAY_MS for e in xs])
    wins = [e[2] for e in xs if e[2] is not None]
    maes = sorted(e[3] for e in xs if e[3] is not None)
    return {"n": len(xs), "n_coins": c["days"], "n_days": len({e[4] // DAY_MS for e in xs}), "mean": round(m, 3),
            "ci95": [round(c["ci95"][0], 3), round(c["ci95"][1], 3)] if c["ci95"] and se else None,
            "t": round(m / se, 2) if se else None, "t_day": None if t_day is None else round(t_day, 2),
            "win_pct": round(sum(wins) / len(wins) * 100, 1) if wins else None,
            "mae_p90": round(maes[int(len(maes) * 0.9)], 2) if maes else None}


def card(db, pid: str, now_ms: int | None = None) -> dict[str, Any] | None:
    """Evidence card of a pattern's latest version: all / out-of-sample / last 90 days (raw, descriptive), the
    forward test and the last-90-day forward rows (what promotion and decay are decided on), trials, status."""
    p = latest(db, pid)
    if not p:
        return None
    now_ms = now_ms or int(time.time() * 1000)
    rows = db.execute("SELECT symbol, outcome, win, mae_pct, oos, ts_ms, source FROM pattern_episodes WHERE"
                      " pattern_id=? AND version=?", (pid, p["version"])).fetchall()
    base = [(r[0], r[1], r[2], r[3], r[5] or 0) for r in rows]
    trials = trial_count(db, pid)
    sources = dict(db.execute("SELECT source, COUNT(*) FROM pattern_episodes WHERE pattern_id=? AND version=? GROUP BY"
                              " source", (pid, p["version"])).fetchall())
    learn = [r[0] for r in db.execute("SELECT learning_id FROM pattern_learnings WHERE pattern_id=?", (pid,))]
    fwd, rec = F.forward(db, pid, p["version"]), F.recent(db, pid, p["version"], now_ms)
    return {"id": pid, "version": p["version"], "level": p["level"], "parent": p["parent"], "name": p["name"],
            "status": p["status"], "status_reason": p["status_reason"], "metric": p["metric"],
            "mechanism": p["mechanism"],
            "all": _stats(base), "oos": _stats(_oos(rows)), "last_90d": _stats(_recent(rows, now_ms)),
            "forward": fwd, "forward_90d": rec,
            "trials": trials, "learnings": learn, "sources": sources,   # 'forensics' = hindsight-picked: anecdotes
            "verdict": F.verdict(p["status"], fwd, rec)}


LIVE_SOURCES = ("scan", "live")    # decay is judged on what happened AFTER registration, never on the backtest
HINDSIGHT = ("forensics",)         # hand-picked episodes: shown, never evidence


def _oos(rows: list[tuple]) -> list[tuple]:
    return [(r[0], r[1], r[2], r[3], r[5] or 0) for r in rows if r[4] and r[6] not in HINDSIGHT]


def _recent(rows: list[tuple], now_ms: int) -> list[tuple]:
    return [(r[0], r[1], r[2], r[3], r[5] or 0) for r in rows
            if r[6] in LIVE_SOURCES and r[5] and r[5] >= now_ms - 90 * DAY_MS]


def set_status(db, pid: str, status: str, reason: str) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    p = latest(db, pid)
    if not p:
        return {"ok": False, "error": f"unknown pattern {pid}"}
    if status == "live":
        c = card(db, pid)
        if not c or c["verdict"] != "eligible for live":
            return {"ok": False, "error": f"not eligible for live ({c and c['verdict']}): needs a passed forward test "
                                          f"(the frozen plan's first target_n observations over >= min_days days, "
                                          f"adjusted for the same scan's control and costs, p <= alpha / M): "
                                          f"{c and c['forward']}"}
        dup = [d for d in duplicates(db, pid) if (latest(db, d["id"]) or {}).get("level") == p["level"]]
        if dup:
            return {"ok": False, "error": f"duplicate of an active pattern at the same level: {dup}"}
    db.execute("UPDATE patterns SET status=?, status_reason=?, updated_at=? WHERE id=? AND version=?",
               (status, reason, _now(), pid, p["version"]))
    db.commit()
    return {"ok": True, "id": pid, "version": p["version"], "status": status}


def duplicates(db, pid: str, window_ms: int = 6 * 3_600_000) -> list[dict[str, Any]]:
    """Other active patterns whose episodes coincide (same coin within 6h) with most of this one's."""
    mine = db.execute("SELECT symbol, ts_ms FROM pattern_episodes WHERE pattern_id=?", (pid,)).fetchall()
    if not mine:
        return []
    out = []
    for (oid,) in db.execute("SELECT DISTINCT id FROM patterns WHERE id<>? AND status IN ('shadow','live')", (pid,)):
        theirs: dict[str, list[int]] = {}
        for s, t in db.execute("SELECT symbol, ts_ms FROM pattern_episodes WHERE pattern_id=?", (oid,)):
            theirs.setdefault(s, []).append(t)
        hit = sum(any(abs(t - u) <= window_ms for u in theirs.get(s, ())) for s, t in mine)
        if hit / len(mine) >= DUP_OVERLAP:
            out.append({"id": oid, "overlap": round(hit / len(mine), 2)})
    return out


def tree(db) -> dict[str, list[dict[str, Any]]]:
    ensure(db)
    out: dict[str, list[dict[str, Any]]] = {}
    for r in db.execute("SELECT p.id, p.version, p.level, p.parent, p.status, (SELECT COUNT(*) FROM pattern_episodes e"
                        " WHERE e.pattern_id=p.id AND e.version=p.version) FROM patterns p WHERE p.version = (SELECT"
                        " MAX(version) FROM patterns q WHERE q.id=p.id) ORDER BY p.level, p.id"):
        out.setdefault(r[2], []).append({"id": r[0], "v": r[1], "parent": r[3], "status": r[4], "episodes": r[5]})
    return out


# ------------------------------------------------------------------ lessons search (FTS5)
def reindex_lessons(db) -> int:
    ensure(db)
    db.execute("DELETE FROM lessons_fts")
    rows = [("learning", f"#{i}", f"{s or ''} {st} {ev or ''}")
            for i, s, st, ev in db.execute("SELECT id, setup, statement, evidence FROM learnings")]
    rows += [("pattern", pid, f"{n or ''} {m or ''} {d or ''}")
             for pid, n, m, d in db.execute("SELECT id, name, mechanism, definition FROM patterns")]
    db.executemany("INSERT INTO lessons_fts (kind, ref, text) VALUES (?,?,?)", rows)
    db.commit()
    return len(rows)


def search(db, query: str, limit: int = 8) -> list[dict[str, Any]]:
    ensure(db)
    q = " OR ".join(w for w in query.replace('"', " ").split() if len(w) > 2) or query
    try:
        rows = db.execute("SELECT kind, ref, snippet(lessons_fts, 2, '[', ']', '…', 12) FROM lessons_fts WHERE"
                          " lessons_fts MATCH ? ORDER BY rank LIMIT ?", (q, limit)).fetchall()
    except Exception:  # noqa: BLE001 - FTS syntax errors on odd input: no results, never a crash
        return []
    return [{"kind": k, "ref": r, "snippet": s} for k, r, s in rows]


def compact(c: dict[str, Any] | None) -> dict[str, Any] | None:
    """One-line evidence card for read_coin."""
    if not c:
        return None
    pick = lambda s: s and {k: s[k] for k in ("n", "n_coins", "mean", "ci95") if k in s}  # noqa: E731
    f = c["forward"]
    fwd = {"status": f["status"], **({"n": f["interim"]["n"], "target": f["target"]} if "interim" in f else {}),
           **({"decision": f["decision"], "p": f["test"]["p"], "mean": f["test"]["mean"]} if "test" in f else {})}
    return {"id": c["id"], "v": c["version"], "status": c["status"], "reason": c.get("status_reason"),
            "metric": c["metric"], "forward": fwd, "all": pick(c["all"]),
            "oos": pick(c["oos"]), "last_90d": pick(c["last_90d"]), "trials": c["trials"], "sources": c["sources"],
            "verdict": c["verdict"]}


def node_cards(db, ids: list[str]) -> list[dict[str, Any]]:
    ensure(db)
    return [x for pid in dict.fromkeys(ids) if (x := compact(card(db, pid)))]


def decay_alarms(db, now_ms: int | None = None) -> list[dict[str, Any]]:
    """Live / probation patterns whose evidence now says probation or retire (checked by the guardian)."""
    ensure(db)
    out = []
    for (pid,) in db.execute("SELECT DISTINCT id FROM patterns WHERE status IN ('live','probation')"):
        c = card(db, pid, now_ms)
        if c and c["status"] in ("live", "probation") and not c["verdict"].startswith("keep"):
            out.append({"id": pid, "status": c["status"], "verdict": c["verdict"], "forward_90d": c["forward_90d"]})
    return out
