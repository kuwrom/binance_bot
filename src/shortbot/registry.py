"""Pattern registry (learning #14, docs/REGISTRY.md): every tape state, script, setup, detector, strategy and filter
is a versioned record with a status, its episodes (as-of features + outcome) and every trial ever run on it.

    status: draft -> shadow -> live -> probation -> retired   (retired rows stay: they still count as trials)

- a changed definition / detector / metric = a NEW version (old episodes stay with the old version)
- promotion to live needs out-of-sample evidence (>= 30 OOS episodes on distinct coins, CI above 0, coin- and
  day-clustered t >= 3; no day-clustered t, e.g. all on one day, fails); the code refuses otherwise.
  Probation / retirement when the last-90-day CI crosses zero (decay alarm)
- a new pattern whose episodes mostly coincide with an existing one's is flagged as a duplicate
Evidence cards summarise a node for `read_coin`; lessons are full-text searchable (FTS5)."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from datetime import datetime, timezone
from typing import Any

STATUSES = ("draft", "shadow", "live", "probation", "retired")
LEVELS = ("regime", "lifecycle", "tape", "controller", "script", "setup", "detector", "strategy", "filter", "veto")
PROMOTE_MIN_OOS, PROMOTE_MIN_T = 30, 3.0
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


def register(db, p: dict[str, Any]) -> dict[str, Any]:
    """Insert a pattern, or a new version when its definition/detector/metric changed. Same fingerprint = no-op
    (metadata such as name/mechanism/source is refreshed in place)."""
    ensure(db)
    if p["level"] not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    fp, cur = _fp(p), latest(db, p["id"])
    now = _now()
    if cur and cur["fingerprint"] == fp:
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


def add_trial(db, pid: str, params: dict[str, Any], source: str, stats: dict[str, Any]) -> None:
    ver = (latest(db, pid) or {}).get("version") or 1
    db.execute("INSERT OR REPLACE INTO pattern_trials (pattern_id, version, params, source, n, n_coins, mean, t,"
               " oos_n, oos_mean, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (pid, ver, json.dumps(params, sort_keys=True), source, stats.get("n"), stats.get("n_coins"),
                stats.get("mean"), stats.get("t"), stats.get("oos_n"), stats.get("oos_mean"), _now()))
    db.commit()


def link_learning(db, pid: str, learning_id: int) -> None:
    ensure(db)
    db.execute("INSERT OR IGNORE INTO pattern_learnings VALUES (?,?)", (pid, learning_id))
    db.commit()


# ------------------------------------------------------------------ evidence
def _clustered_t(xs: list[tuple], key: int) -> float | None:
    by: dict[Any, list[float]] = {}
    for e in xs:
        by.setdefault(e[key] // DAY_MS if key == 4 else e[key], []).append(e[1])
    cm = [statistics.fmean(v) for v in by.values()]
    if len(cm) < 2:
        return None
    se = statistics.stdev(cm) / math.sqrt(len(cm))
    return round(statistics.fmean(cm) / se, 2) if se else None


def _stats(eps: list[tuple]) -> dict[str, Any] | None:
    """eps: (symbol, outcome, win, mae, ts_ms). Coin-clustered mean/CI; t_day clusters by calendar day too (scan
    outcomes of one sell-off week are one market move, not 30 independent coins)."""
    xs = [e for e in eps if e[1] is not None]
    if not xs:
        return None
    by: dict[str, list[float]] = {}
    for e in xs:
        by.setdefault(e[0], []).append(e[1])
    cm = [statistics.fmean(v) for v in by.values()]
    m = statistics.fmean(cm)
    se = statistics.stdev(cm) / math.sqrt(len(cm)) if len(cm) > 1 else None
    wins = [e[2] for e in xs if e[2] is not None]
    maes = sorted(e[3] for e in xs if e[3] is not None)
    return {"n": len(xs), "n_coins": len(by), "n_days": len({e[4] // DAY_MS for e in xs}), "mean": round(m, 3),
            "ci95": [round(m - 1.96 * se, 3), round(m + 1.96 * se, 3)] if se else None,
            "t": round(m / se, 2) if se else None, "t_day": _clustered_t(xs, 4),
            "win_pct": round(sum(wins) / len(wins) * 100, 1) if wins else None,
            "mae_p90": round(maes[int(len(maes) * 0.9)], 2) if maes else None}


def card(db, pid: str, now_ms: int | None = None) -> dict[str, Any] | None:
    """Evidence card of a pattern's latest version: all / out-of-sample / last 90 days, trials, status."""
    p = latest(db, pid)
    if not p:
        return None
    now_ms = now_ms or int(time.time() * 1000)
    rows = db.execute("SELECT symbol, outcome, win, mae_pct, oos, ts_ms, source FROM pattern_episodes WHERE"
                      " pattern_id=? AND version=?", (pid, p["version"])).fetchall()
    base = [(r[0], r[1], r[2], r[3], r[5] or 0) for r in rows]
    trials = db.execute("SELECT COUNT(*) FROM pattern_trials WHERE pattern_id=?", (pid,)).fetchone()[0]
    sources = dict(db.execute("SELECT source, COUNT(*) FROM pattern_episodes WHERE pattern_id=? AND version=? GROUP BY"
                              " source", (pid, p["version"])).fetchall())
    learn = [r[0] for r in db.execute("SELECT learning_id FROM pattern_learnings WHERE pattern_id=?", (pid,))]
    return {"id": pid, "version": p["version"], "level": p["level"], "parent": p["parent"], "name": p["name"],
            "status": p["status"], "status_reason": p["status_reason"], "metric": p["metric"],
            "mechanism": p["mechanism"],
            "all": _stats(base), "oos": _stats(_oos(rows)), "last_90d": _stats(_recent(rows, now_ms)),
            "trials": trials, "learnings": learn, "sources": sources,   # 'forensics' = hindsight-picked: anecdotes
            "verdict": verdict(p["status"], rows, now_ms)}


LIVE_SOURCES = ("scan", "live")    # decay is judged on what happened AFTER registration, never on the backtest
HINDSIGHT = ("forensics",)         # hand-picked episodes: shown, never evidence


def _oos(rows: list[tuple]) -> list[tuple]:
    return [(r[0], r[1], r[2], r[3], r[5] or 0) for r in rows if r[4] and r[6] not in HINDSIGHT]


def _recent(rows: list[tuple], now_ms: int) -> list[tuple]:
    return [(r[0], r[1], r[2], r[3], r[5] or 0) for r in rows
            if r[6] in LIVE_SOURCES and r[5] and r[5] >= now_ms - 90 * DAY_MS]


def verdict(status: str, rows: list[tuple], now_ms: int) -> str:
    """What the evidence allows (the human/analyst still makes the call through set_status). A missing day-clustered
    t fails the gate: one day's episodes are one market move, however many coins they span."""
    oos, recent = _stats(_oos(rows)), _stats(_recent(rows, now_ms))
    eligible = bool(oos and oos["n_coins"] >= PROMOTE_MIN_OOS and oos["ci95"] and oos["ci95"][0] > 0
                    and oos["t_day"] is not None and min(oos["t"] or 0, oos["t_day"]) >= PROMOTE_MIN_T)
    decayed = bool(recent and recent["n_coins"] >= 10 and recent["ci95"] and recent["ci95"][0] <= 0)
    if status == "live" and decayed:
        return "probation: last-90d CI crosses 0"
    if status == "probation" and recent and recent["n_coins"] >= 30 and recent["mean"] < 0:
        return "retire: edge gone"
    if status in ("draft", "shadow") and eligible:
        return "eligible for live"
    if status in ("draft", "shadow") and oos and oos["t_day"] is None:
        why = "all OOS episodes on one day" if oos["n_days"] < 2 else "no day-to-day variance"
        return f"keep {status}: no day-clustered t ({why})"
    return "keep " + status


def set_status(db, pid: str, status: str, reason: str) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    p = latest(db, pid)
    if not p:
        return {"ok": False, "error": f"unknown pattern {pid}"}
    if status == "live":
        c = card(db, pid)
        if not c or c["verdict"] != "eligible for live":
            return {"ok": False, "error": f"not eligible for live ({c and c['verdict']}): {c and c['oos']} (needs >= "
                                          f"{PROMOTE_MIN_OOS} OOS coins, CI above 0, coin- and day-clustered t >= "
                                          f"{PROMOTE_MIN_T}; a missing day-clustered t fails)"}
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
    return {"id": c["id"], "v": c["version"], "status": c["status"], "reason": c.get("status_reason"),
            "metric": c["metric"], "all": pick(c["all"]),
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
            out.append({"id": pid, "status": c["status"], "verdict": c["verdict"], "last_90d": c["last_90d"]})
    return out
