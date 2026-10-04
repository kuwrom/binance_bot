"""The backup drill (#46): `shortbot backup drill [--dir D]`. A backup only counts once it has been restored.

The newest local copy of the journal (SHORTBOT_BACKUP_DIR, or --dir) is restored to a fresh temp directory, never
under data/, through backup.restore and its verification (the manifest's sha256 of the .gz and of the database,
integrity_check, every row count), then compared with the live journal (read-only):
- every table of the copy is in the live journal with at least as many rows (the journal only grows), except the ones
  whose rows legitimately go: SHRINKS;
- the copy's newest trade is in the live journal, the same trade (symbol, opened_at);
- the copy is under MAX_AGE_H old (the daily job ran).
The temp copy is deleted after. Every run journals a drill_result (drill_log.py); a pass also sets kv
backup_drill_last_ok. The off-box half (restoring a copy from the bucket on another machine) is the owner's:
`shortbot drill attest backup-offbox --evidence TEXT`."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import audit, backup, drill_log
from .backup_rotate import SUFFIX

MAX_AGE_H = 36.0
KV_KEY = "backup_drill_last_ok"
# rows that go by design: pruned kv markers, the coin watchlist, registry episodes rebuilt per seed, FTS shadow tables
SHRINKS = ("kv", "watchlist", "pattern_episodes")


def newest(d: Path, stem: str) -> Path | None:
    """The newest <stem>-<UTC>.db.gz in d (the stamp sorts by time)."""
    copies = sorted(p for p in d.glob(f"{stem}-*{SUFFIX}") if p.is_file())
    return copies[-1] if copies else None


def _shrinks(table: str) -> bool:
    return table in SHRINKS or "_fts" in table


def _trade(path: Path, tid: int | None) -> tuple[Any, ...] | None:
    if tid is None:
        return None
    db = backup._ro(path)
    try:
        row = db.execute("SELECT id, symbol, opened_at FROM trades WHERE id=?", (tid,)).fetchone()
    finally:
        db.close()
    return tuple(row) if row else None


def _counts(path: Path) -> tuple[dict[str, int], int | None]:
    db = backup._ro(path)
    try:
        counts = backup._counts(db)
        top = db.execute("SELECT MAX(id) FROM trades").fetchone()[0] if "trades" in counts else None
    finally:
        db.close()
    return counts, top


def compare(restored: Path, live: Path) -> dict[str, dict[str, Any]]:
    """The checks against the live journal (module doc)."""
    got, top = _counts(restored)
    now, _ = _counts(live)
    worse = {t: {"copy": n, "live": now.get(t)} for t, n in got.items()
             if not _shrinks(t) and (now.get(t) is None or n > now[t])}
    mine, theirs = _trade(restored, top), _trade(live, top)
    return {"tables_le_live": drill_log.check(not worse, worse or f"{len(got)} tables"),
            "newest_trade_live": drill_log.check(top is None or (mine is not None and mine == theirs),
                                                 {"trade_id": top, "copy": mine, "live": theirs})}


def drill(db: Path, d: Path, now: datetime | None = None) -> dict[str, Any]:
    """Restore and compare (module doc); the checks, the copy and the restore's own answer. Raises nothing."""
    now = now or datetime.now(UTC)
    checks: dict[str, dict[str, Any]] = {}
    copy = newest(d, db.stem)
    out: dict[str, Any] = {"dir": str(d.resolve()), "copy": copy.name if copy else None, "checks": checks}
    if copy is None:
        checks["restore"] = drill_log.check(False, f"no {db.stem}-*{SUFFIX} in {d}")
        return out
    tmp = Path(tempfile.mkdtemp(prefix="shortbot-backup-drill-"))
    try:
        if tmp.resolve().is_relative_to(db.resolve().parent):   # never beside the live journal
            raise backup.BackupError(f"temp dir {tmp} is under {db.parent}: refusing to restore there")
        t0 = time.monotonic()
        res = backup.restore(copy, tmp / "restored.db")
        out["restore_s"] = round(time.monotonic() - t0, 2)
        checks["restore"] = drill_log.check(res["rows"] is not None, res["checked"])
        checks.update(compare(tmp / "restored.db", db))
        made = datetime.fromisoformat(res["created_at"]) if res.get("created_at") else None
        age = (now - made).total_seconds() / 3600 if made else None
        out.update(created_at=res.get("created_at"), age_h=None if age is None else round(age, 1))
        checks["fresh"] = drill_log.check(age is not None and age < MAX_AGE_H,
                                          f"{age:.1f} h old (< {MAX_AGE_H:g} h)" if age is not None else "no manifest")
    except Exception as e:  # noqa: BLE001 - the finding: recorded as the failed check, the run still journals it
        checks["restore"] = drill_log.check(False, f"{type(e).__name__}: {e}"[:500])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out["temp_removed"] = not tmp.exists()
    return out


def run(db: Path | None = None, d: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """One drill, journalled in the desk journal: drill_result, and kv backup_drill_last_ok on a pass."""
    from .config import Settings
    from .journal import Journal
    db = db or backup.desk_db()
    d = d or backup.BackupConfig.from_env().dir
    started = datetime.now(UTC).isoformat(timespec="seconds")
    with audit.acting("cli:drill:backup"):
        out = drill(db, d, now)
        try:
            mode = Settings.from_env().mode
        except Exception:  # noqa: BLE001 - a settings problem never hides the drill's finding; demo is the default
            mode = "demo"
        j = Journal(db, mode)
        try:
            v = drill_log.verdict(out["checks"])
            res = drill_log.result(j, "backup", v, out.pop("checks"), started=started, pid=os.getpid(), **out)
            if v == "pass":
                j.set(KV_KEY, json.dumps({"ts": res["finished"], "copy": res["copy"],
                                                    "created_at": res.get("created_at"), "age_h": res.get("age_h"),
                                                    "decision_id": res["decision_id"]}))
        finally:
            j.db.close()
    return res
