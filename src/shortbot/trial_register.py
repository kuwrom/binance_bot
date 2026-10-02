"""One append-only register of every trial any study ran (#31): the N behind the Deflated Sharpe and the count the
registry cards show. It lives in data/research/trials.db (SHORTBOT_TRIALS_DB overrides), beside the stamped run dirs,
not in the journal: research runs never write the live journal, and the guardian / MCP never need this file. It is
created on demand by a writer; to a reader a missing file is an empty register.

- A trial = (study, family, params): `spec` is its hash. Reruns append rows (the audit trail) but count once.
- Controls (placebo / random groups) are recorded with control=1 and never counted.
- Each row may carry its daily series ({UTC day: summed outcome}); trials_eff clusters them into independent families
  (effective N). A trial without a series counts as its own family (conservative).
- The old ledgers (journal pattern_trials, ladder trial_ledger) are copied in once by migrate_legacy (journal opened
  read-only; idempotent via `origin`) and are not written any more.
- SHORTBOT_TRIALS_ASOF=<id> pins a rerun to the register as it was (results.json "register" records max_id).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENV, ASOF_ENV = "SHORTBOT_TRIALS_DB", "SHORTBOT_TRIALS_ASOF"
SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    id INTEGER PRIMARY KEY, study TEXT NOT NULL, family TEXT, name TEXT, params TEXT NOT NULL, spec TEXT NOT NULL,
    metric TEXT, control INTEGER NOT NULL DEFAULT 0, n INTEGER, days INTEGER, coins INTEGER, mean REAL, se REAL,
    t REAL, t_kind TEXT, sr REAL, n_eff REAL, daily TEXT, run_dir TEXT, git_sha TEXT, code_sha TEXT, window TEXT,
    origin TEXT UNIQUE, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS trials_spec ON trials (spec);
CREATE INDEX IF NOT EXISTS trials_family ON trials (family);
CREATE TRIGGER IF NOT EXISTS trials_no_update BEFORE UPDATE ON trials
    BEGIN SELECT RAISE(ABORT, 'the trial register is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trials_no_delete BEFORE DELETE ON trials
    BEGIN SELECT RAISE(ABORT, 'the trial register is append-only'); END;
"""
COLS = ("study", "family", "name", "params", "spec", "metric", "control", "n", "days", "coins", "mean", "se", "t",
        "t_kind", "sr", "n_eff", "daily", "run_dir", "git_sha", "code_sha", "window", "origin", "created_at")
# legacy pattern_trials.source -> the study that writes those trials now (same params shape, so the same spec)
LEGACY_STUDY = {"bt60": "backtest", "flow_60d": "backtest", "long_sleeve_60d": "backtest", "rungs_60d": "rungs",
                "rungs_daily": "rungs_daily", "carry_regime_daily": "carry_regime",
                "funding_events_daily": "funding_events", "xs_daily": "xs_factors", "card_tests": "card_tests"}


def path(base: Path | None = None) -> Path:
    """SHORTBOT_TRIALS_DB, else <data dir>/research/trials.db (base: the data dir, e.g. the journal's parent)."""
    if os.getenv(ENV):
        return Path(os.environ[ENV]).expanduser()
    if base is None:
        from .provenance import data_dir
        base = data_dir()
    return Path(base) / "research" / "trials.db"


def _canon(params: Any) -> str:
    return json.dumps(params if params is not None else {}, sort_keys=True, default=str)


def spec_of(study: str, family: str | None, params: Any) -> str:
    return hashlib.sha256(json.dumps([study, family, _canon(params)]).encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(db_path: Path | None = None) -> sqlite3.Connection:
    p = db_path or path()
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(p), timeout=30)
    db.executescript(SCHEMA)
    return db


def _read(db_path: Path | None = None) -> sqlite3.Connection | None:
    p = db_path or path()
    if not p.exists():
        return None
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='trials'").fetchone():
        db.close()
        return None
    return db


def _r(x: Any, nd: int = 6) -> Any:
    return round(x, nd) if isinstance(x, float) else x


def _values(study: str, r: dict[str, Any], prov: dict[str, Any] | None, run_dir: str | None) -> tuple:
    git = (prov or {}).get("git") or {}
    daily = r.get("daily")
    return (study, r.get("family"), r.get("name"), _canon(r.get("params")), spec_of(study, r.get("family"),
            r.get("params")), r.get("metric"), int(bool(r.get("control"))), r.get("n"), r.get("days"), r.get("coins"),
            _r(r.get("mean")), _r(r.get("se")), _r(r.get("t"), 3), r.get("t_kind"), _r(r.get("sr"), 4),
            _r(r.get("n_eff"), 1),
            json.dumps({d: round(v, 4) for d, v in sorted(daily.items())}, separators=(",", ":")) if daily else None,
            None if run_dir is None else str(run_dir), git.get("sha"), (prov or {}).get("code_sha"),
            json.dumps((prov or {}).get("window"), default=str) if (prov or {}).get("window") else None,
            r.get("origin"), r.get("created_at") or _now())


def record(study: str, rows: Iterable[dict[str, Any]], prov: dict[str, Any] | None = None,
           run_dir: Path | str | None = None, db_path: Path | None = None) -> int:
    """Append one row per trial (keys: name, family, params, metric, control, n, days, coins, mean, se, t, t_kind,
    sr, n_eff, daily). prov: provenance.stamp's dict (git sha, code sha, window). Returns the rows written."""
    vals = [_values(study, r, prov, None if run_dir is None else str(run_dir)) for r in rows]
    if not vals:
        return 0
    db = _write(db_path)
    try:
        with db:
            db.executemany(f"INSERT OR IGNORE INTO trials ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})",
                           vals)
        return db.total_changes
    finally:
        db.close()


def _asof(asof: int | None) -> int | None:
    return asof if asof is not None else (int(os.environ[ASOF_ENV]) if os.getenv(ASOF_ENV) else None)


def summary(family: str | None = None, db_path: Path | None = None, asof: int | None = None) -> dict[str, Any]:
    """rows, distinct non-control specs, control specs, specs by study, max id. A missing register is empty."""
    out: dict[str, Any] = {"rows": 0, "specs": 0, "controls": 0, "by_study": {}, "max_id": 0}
    db = _read(db_path)
    if db is None:
        return out
    try:
        cond, args = ["id <= ?"], [_asof(asof) if _asof(asof) is not None else 1 << 62]
        if family is not None:
            cond.append("family = ?")
            args.append(family)
        w = " WHERE " + " AND ".join(cond)
        out["rows"], out["max_id"] = db.execute(f"SELECT COUNT(*), COALESCE(MAX(id), 0) FROM trials{w}",
                                                args).fetchone()
        out["controls"] = db.execute(f"SELECT COUNT(DISTINCT spec) FROM trials{w} AND control = 1", args).fetchone()[0]
        out["by_study"] = dict(db.execute(f"SELECT study, COUNT(DISTINCT spec) FROM trials{w} AND control = 0 GROUP BY"
                                          " study ORDER BY study", args).fetchall())
        out["specs"] = sum(out["by_study"].values())
    finally:
        db.close()
    return out


def count(family: str | None = None, db_path: Path | None = None) -> int:
    """Distinct non-control trials (of one family: a registry pattern id, its card's trial count). For the MCP and
    the guardian: a missing or unreadable register counts 0, never an error."""
    try:
        return summary(family, db_path)["specs"]
    except (sqlite3.Error, OSError, ValueError):
        return 0


def others(exclude: Iterable[str] = (), db_path: Path | None = None, asof: int | None = None
           ) -> tuple[dict[str, dict[str, float]], int]:
    """({spec: daily series} of every counted trial with a series, number of counted specs without one), outside
    `exclude` (the caller's own specs). The latest row of a spec wins."""
    db = _read(db_path)
    if db is None:
        return {}, 0
    skip, a = set(exclude), _asof(asof)
    series: dict[str, dict[str, float]] = {}
    bare: set[str] = set()
    try:
        for spec, daily in db.execute("SELECT spec, daily FROM trials WHERE control = 0 AND id <= ? ORDER BY id",
                                      (a if a is not None else 1 << 62,)):
            if spec in skip:
                continue
            if daily:
                series[spec] = json.loads(daily)
                bare.discard(spec)
            elif spec not in series:
                bare.add(spec)
    finally:
        db.close()
    return series, len(bare)


def backtest_row_key(strategy: str, name: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(family, params) under which the backtest records a trial (registry_seed's bt60 shape)."""
    return f"strategy.{strategy}", {"trial": name, **params}


def _ledger_match(p: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """A --flow / --long legacy row (recorded by hand, without the trial name) -> the ledger trial it was, if
    exactly one matches its params (and its strategy, when the row names one)."""
    from .backtest.strategies_flow import trials_flow
    from .backtest.strategies_long import trials_long
    rest = {k: v for k, v in p.items() if k != "strategy"}
    hits = [t for t in [*trials_flow(), *trials_long()]
            if t.params == rest and p.get("strategy") in (None, t.strategy)]
    return backtest_row_key(hits[0].strategy, hits[0].name, hits[0].params) if len(hits) == 1 else None


def _legacy_rows(j: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    have = {r[0] for r in j.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    pt, tl = [], []
    if "pattern_trials" in have:
        for i, pid, params, src, n, mean, t, created in j.execute(
                "SELECT id, pattern_id, params, source, n, mean, t, created_at FROM pattern_trials ORDER BY id"):
            p = json.loads(params or "{}")
            if src in ("flow_60d", "long_sleeve_60d") and (hit := _ledger_match(p)):
                pid, p = hit
            pt.append({"study": LEGACY_STUDY.get(src, f"legacy:{src}"), "family": pid, "params": p,
                       "name": p.get("trial") or pid, "n": n, "mean": mean, "t": t,
                       "t_kind": "welch_vs_placebo" if src == "bt60" else "legacy",
                       "control": p.get("group") == "random",
                       "origin": f"pattern_trials:{i}", "created_at": created})
    if "trial_ledger" in have:
        for variant, fam, params, created in j.execute("SELECT variant, family, params, created FROM trial_ledger"):
            p = json.loads(params or "{}")
            tl.append({"study": fam or "ladder", "family": fam or "ladder", "params": {"variant": variant, **p},
                       "name": variant, "control": bool(p.get("placebo")), "origin": f"trial_ledger:{variant}",
                       "created_at": created})
    return pt, tl


def migrate_legacy(journal: Path | str, db_path: Path | None = None) -> dict[str, int]:
    """Copy the journal's pattern_trials and trial_ledger rows in (journal read-only; idempotent)."""
    jp = Path(journal)
    if not jp.exists():
        return {"pattern_trials": 0, "trial_ledger": 0, "added": 0}
    j = sqlite3.connect(f"file:{jp}?mode=ro", uri=True, timeout=30)
    try:
        pt, tl = _legacy_rows(j)
    finally:
        j.close()
    by: dict[str, list[dict[str, Any]]] = {}
    for r in pt + tl:
        by.setdefault(r.pop("study"), []).append(r)
    added = sum(record(study, rows, db_path=db_path) for study, rows in by.items())
    return {"pattern_trials": len(pt), "trial_ledger": len(tl), "added": added}


def main(argv: list[str] | None = None) -> int:
    """python -m shortbot.trial_register summary | migrate [--journal PATH]  (migrate: once, after deploying #31)."""
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="python -m shortbot.trial_register")
    ap.add_argument("action", choices=("summary", "migrate"))
    ap.add_argument("--journal", type=Path, help="migrate from (default: SHORTBOT_DB, the journal)")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    out: dict[str, Any] = {"register": str(path())}
    if a.action == "migrate":
        from .provenance import desk_db
        out["migrated"] = migrate_legacy(a.journal or desk_db())
    out["summary"] = summary()
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
