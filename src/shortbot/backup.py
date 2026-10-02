"""Verified online backups of the desk journal (data/shortbot.db) and, weekly, the research DBs. Schedule, off-box
examples and the restore procedure: deploy/DEPLOY.md (Backups).

One read transaction on a read-only source connection first counts every table's rows, then the SQLite online backup
API copies the whole database in a single step INSIDE that transaction (sqlite3_backup_step reuses an open read
transaction), so the copy is exactly the counted snapshot while the guardian and MCP server keep writing (WAL readers
don't block writers). The raw file is never copied. The copy must pass PRAGMA integrity_check and match every count;
it is then gzipped, the gzip is decompressed and hashed against the verified bytes, and only then renamed into place
next to a JSON manifest. Then the optional off-box command, rotation (backup_rotate.py) and kv `backup_last_ok`.
Any failure: exit 1 + a `backup_failed` alert (journal + Telegram); a verified copy whose kv mark or rotation
failed is still listed. No journal table is ever pruned.

    .venv/bin/shortbot backup                       # SHORTBOT_DB -> SHORTBOT_BACKUP_DIR (default data/backups)
    .venv/bin/shortbot backup restore data/backups/shortbot-<UTC>.db.gz --to /tmp/drill.db
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import gzip
import hashlib
import json
import os
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from .backup_rotate import STAMP, SUFFIX, manifest_of, rotate, rotate_log
from .config import Settings

METHOD = "sqlite3 online backup API, one step inside the read transaction that took the row counts"
GZIP_LEVEL = 6
LOCK_DEADLINE_S = 600             # CPython retries a busy source forever; give up after this
FREE_MARGIN = 512 * 2**20         # never leave less than this free on the backup disk (may be the guardian's)
OFFBOX_TIMEOUT_S = 1800
CHUNK = 2**20
KV_RETRIES, KV_BACKOFF_S = 3, 1.0     # a busy journal must not turn a verified copy into a failed run
TMP_DIR, LOCK_FILE = ".shortbot-backup-tmp", ".shortbot-backup.lock"   # our own names: --dir may be any folder


class BackupError(RuntimeError):
    pass


def desk_db() -> Path:
    return Path(os.getenv("SHORTBOT_DB", "data/shortbot.db"))


@dataclass(frozen=True)
class BackupConfig:
    dir: Path
    keep_daily: int
    keep_weekly: int
    offbox: str                   # command template, run once per new file ('' = local copies only)

    @classmethod
    def from_env(cls) -> "BackupConfig":
        return cls(Path(os.getenv("SHORTBOT_BACKUP_DIR", "data/backups")),
                   int(os.getenv("SHORTBOT_BACKUP_KEEP_DAILY", 7)), int(os.getenv("SHORTBOT_BACKUP_KEEP_WEEKLY", 4)),
                   os.getenv("SHORTBOT_BACKUP_OFFBOX", "").strip())


# ------------------------------------------------------------ copy + verify
def _ro(path: Path, **kw: Any) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, **kw)


def _counts(db: sqlite3.Connection) -> dict[str, int]:
    names = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {n: db.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def _sha256(path: Path, opener: Callable = open) -> str:
    h = hashlib.sha256()
    with opener(path, "rb") as f:
        while b := f.read(CHUNK):
            h.update(b)
    return h.hexdigest()


def snapshot(src: Path, dst: Path, deadline_s: float = LOCK_DEADLINE_S) -> dict[str, int]:
    """Copy src into the new file dst as one consistent snapshot; returns that snapshot's row counts per table."""
    s, d = _ro(src, isolation_level=None, timeout=30), sqlite3.connect(str(dst))
    give_up = time.monotonic() + deadline_s

    def deadline(status: int, _remaining: int, _total: int) -> None:
        if status != sqlite3.SQLITE_DONE and time.monotonic() > give_up:
            raise TimeoutError(f"source stayed locked for {deadline_s:.0f}s")
    try:
        s.execute("BEGIN")                          # deferred: the snapshot starts at the first read, in _counts
        rows = _counts(s)
        s.backup(d, pages=-1, progress=deadline)    # one step = one shared lock, inside the counted snapshot
        s.execute("COMMIT")
        d.execute("PRAGMA journal_mode=DELETE")     # the copy inherits the WAL flag: make it one self-contained file
        return rows
    finally:
        d.close()
        s.close()


def verify(path: Path, expect: dict[str, int] | None) -> str | None:
    """None if `path` passes PRAGMA integrity_check and has exactly `expect` rows per table (when given), else why."""
    try:
        db = _ro(path)
        try:
            check = [r[0] for r in db.execute("PRAGMA integrity_check")]
            got = _counts(db) if check == ["ok"] else None
        finally:
            db.close()
    except sqlite3.DatabaseError as e:
        return f"unreadable: {e}"
    if got is None:
        return "integrity_check: " + "; ".join(check[:5])
    if expect is not None and got != expect:
        return "row counts differ (expected, found): " + str(
            {t: (expect.get(t), got.get(t)) for t in sorted(set(expect) | set(got)) if expect.get(t) != got.get(t)})
    return None


def make_copy(src: Path, out_dir: Path, now: datetime) -> dict[str, Any]:
    """Snapshot, verify, gzip and re-verify src into out_dir/<stem>-<UTC>.db.gz; returns its manifest (not written)."""
    if not src.is_file():
        raise BackupError(f"{src} does not exist")
    name = f"{src.stem}-{now.strftime(STAMP)}{SUFFIX}"
    (tmp := out_dir / TMP_DIR).mkdir(exist_ok=True)
    final, tmp_db, tmp_gz = out_dir / name, tmp / f"{src.stem}.db", tmp / name
    if final.exists():
        raise BackupError(f"{final} already exists")
    t0 = time.monotonic()
    try:
        rows = snapshot(src, tmp_db)
        if err := verify(tmp_db, rows):
            raise BackupError(f"copy failed verification: {err}")
        db_sha, db_bytes = _sha256(tmp_db), tmp_db.stat().st_size
        with open(tmp_db, "rb") as f, open(tmp_gz, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", compresslevel=GZIP_LEVEL, fileobj=raw, mtime=0) as gz:
                shutil.copyfileobj(f, gz, CHUNK)
            raw.flush()
            os.fsync(raw.fileno())
        if _sha256(tmp_gz, gzip.open) != db_sha:
            raise BackupError("the gzip does not decompress to the verified copy")
        os.replace(tmp_gz, final)
    finally:
        tmp_db.unlink(missing_ok=True)
        tmp_gz.unlink(missing_ok=True)
    return {"name": name, "source": str(src.resolve()), "created_at": now.isoformat(timespec="seconds"),
            "host": socket.gethostname(), "method": METHOD, "sqlite_version": sqlite3.sqlite_version,
            "integrity_check": "ok", "rows": rows, "db_bytes": db_bytes, "db_sha256": db_sha,
            "file_bytes": final.stat().st_size, "file_sha256": _sha256(final),
            "seconds": round(time.monotonic() - t0, 2), "offbox_ok": None}


def restore(file: Path, to: Path) -> dict[str, Any]:
    """Decompress a backup to a NEW path after checking it against its manifest (sha256 of the .gz and of the
    database, integrity_check, row counts). Without the manifest only integrity_check runs. Never overwrites."""
    for p in (to, *(Path(f"{to}{s}") for s in ("-wal", "-shm", "-journal"))):
        if p.exists():
            raise BackupError(f"{p} exists: restore only writes a new path (deploy/DEPLOY.md, Backups)")
    side = manifest_of(file)
    man = json.loads(side.read_text()) if side.is_file() else None
    if man and _sha256(file) != man["file_sha256"]:
        raise BackupError(f"{file.name} does not match the sha256 in its manifest")
    to.parent.mkdir(parents=True, exist_ok=True)
    part = to.with_name(f"{to.name}.partial")
    try:
        with gzip.open(file, "rb") as f, open(part, "xb") as o:
            shutil.copyfileobj(f, o, CHUNK)
            o.flush()
            os.fsync(o.fileno())
        if man and _sha256(part) != man["db_sha256"]:
            raise BackupError("the decompressed database does not match the sha256 in its manifest")
        if err := verify(part, man["rows"] if man else None):
            raise BackupError(f"restored database failed verification: {err}")
        os.link(part, to)                           # unlike a rename, fails if `to` appeared meanwhile
    finally:
        part.unlink(missing_ok=True)
    return {"ok": True, "restored": str(to.resolve()), "from": str(file),
            "checked": "manifest sha256s + integrity_check + row counts" if man else "integrity_check only (no manifest)",
            "created_at": man["created_at"] if man else None, "rows": man["rows"] if man else None}


# ------------------------------------------------------------ one run: copies, off-box, rotation, logs
def offbox(template: str, path: Path) -> str | None:
    """Run the owner's off-box command for one file, without a shell. None on success, else why it failed."""
    argv = [os.path.expanduser(a).replace("{file}", str(path.resolve())).replace("{name}", path.name)
            for a in shlex.split(template)]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=OFFBOX_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"{type(e).__name__}: {e}"
    return None if r.returncode == 0 else f"exit {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}"


def _write_manifest(out_dir: Path, man: dict[str, Any]) -> Path:
    path = manifest_of(out_dir / man["name"])
    tmp = out_dir / TMP_DIR / path.name
    tmp.write_text(json.dumps(man, indent=2) + "\n")
    os.replace(tmp, path)
    return path


def _backup_one(src: Path, cfg: BackupConfig, now: datetime) -> dict[str, Any]:
    size = sum(p.stat().st_size for p in (src, Path(f"{src}-wal")) if p.exists())
    free = shutil.disk_usage(cfg.dir).free
    if free < 2 * size + FREE_MARGIN:
        raise BackupError(f"only {free / 2**20:.0f} MB free in {cfg.dir}, need {(2 * size + FREE_MARGIN) / 2**20:.0f}")
    man = make_copy(src, cfg.dir, now)
    if cfg.offbox:
        err = offbox(cfg.offbox, cfg.dir / man["name"])
        man["offbox_ok"] = err is None
        if err:
            man["offbox_error"] = err
    side = _write_manifest(cfg.dir, man)
    if man["offbox_ok"] and (err := offbox(cfg.offbox, side)):
        man.update(offbox_ok=False, offbox_error=f"manifest: {err}")
        _write_manifest(cfg.dir, man)
    return man


def _mark_ok(man: dict[str, Any]) -> str | None:
    """kv backup_last_ok in the desk journal (the newest verified copy, for a missed-run check). Retried with backoff
    when the journal is busy; returns a warning instead of raising, because the copy itself is good."""
    from .journal import Journal
    value = json.dumps({"ts": man["created_at"], "name": man["name"], "offbox_ok": man["offbox_ok"]})
    err: Exception | None = None
    for attempt in range(KV_RETRIES):
        try:
            j = Journal(desk_db())
            try:
                j.set("backup_last_ok", value)
                return None
            finally:
                j.db.close()
        except sqlite3.OperationalError as e:      # "database is locked": try again shortly
            err = e
            if attempt < KV_RETRIES - 1:
                time.sleep(KV_BACKOFF_S * 2**attempt)
        except Exception as e:  # noqa: BLE001
            err = e
            break
    return f"{man['name']} is verified, but kv backup_last_ok was not recorded: {type(err).__name__}: {err}"


def _alert(kind: str, message: str) -> None:
    """backup_failed / backup_warning -> journal alert + Telegram when configured (notify.Notifier). Never raises."""
    try:
        from .journal import Journal
        from .notify import Notifier
        s = Settings.from_env()
        asyncio.run(Notifier(s, Journal(s.db_path)).send(kind, message[:1500], cooldown_s=0))
    except Exception as e:  # noqa: BLE001 - the exit code / JSON still report it
        print(f"{kind} alert not delivered: {type(e).__name__}: {e}", file=sys.stderr)


@contextmanager
def _lock(d: Path) -> Iterator[None]:
    with open(d / LOCK_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)     # BlockingIOError: another run holds it
        yield


def run(dbs: Sequence[Path], cfg: BackupConfig, rotate_logs: Sequence[Path] = (), now: datetime | None = None,
        alert: Callable[[str, str], None] = _alert) -> dict[str, Any]:
    """Back up each database (own copy, manifest, rotation), then copytruncate the given logs. ok=False on any error;
    a missing kv mark is only a warning (the copy is verified)."""
    now = now or datetime.now(timezone.utc)
    cfg.dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {"ok": True, "dir": str(cfg.dir.resolve()), "copies": [], "errors": [], "warnings": []}
    try:
        with _lock(cfg.dir):
            shutil.rmtree(cfg.dir / TMP_DIR, ignore_errors=True)    # left by a killed run; the lock says none runs
            for src in dbs:
                try:
                    man = _backup_one(src, cfg, now)
                except Exception as e:  # noqa: BLE001 - reported, alerted, exit code 1
                    out["errors"].append(f"{src}: {type(e).__name__}: {e}")
                    continue
                out["copies"].append(man)
                if man["offbox_ok"] is False:
                    out["errors"].append(f"{man['name']}: off-box copy failed: {man['offbox_error']}")
                try:
                    man["rotated_out"] = rotate(cfg.dir, src.stem, cfg.keep_daily, cfg.keep_weekly)
                except Exception as e:  # noqa: BLE001
                    out["errors"].append(f"rotating {src.stem} copies failed ({man['name']} is fine): "
                                         f"{type(e).__name__}: {e}")
                if src.resolve() == desk_db().resolve() and (warning := _mark_ok(man)):
                    out["warnings"].append(warning)
            for log in rotate_logs:
                try:
                    out.setdefault("logs", {})[str(log)] = rotate_log(log)
                except (OSError, ValueError) as e:
                    out["errors"].append(f"{log}: {type(e).__name__}: {e}")
    except BlockingIOError:
        out["errors"].append(f"another backup is running ({cfg.dir / LOCK_FILE} is locked)")
    if out["errors"]:
        out["ok"] = False
        alert("backup_failed", "; ".join(out["errors"]))
    if out["warnings"]:
        alert("backup_warning", "; ".join(out["warnings"]))
    return out


def default_dbs() -> list[Path]:
    """The journal, plus the trial register (data/research/trials.db, #31) once a study has created it: it holds
    every trial behind the Deflated Sharpe's N and is not rebuildable. Same copy, verify and rotation."""
    from .trial_register import path as trials_path
    reg = trials_path(desk_db().parent)
    return [desk_db(), *([reg] if reg.exists() else [])]


# ------------------------------------------------------------ CLI (`shortbot backup` and `python -m shortbot.backup`)
def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", nargs="?", choices=("run", "restore"), default="run")
    p.add_argument("file", nargs="?", help="restore: the .db.gz (keep its .json manifest beside it)")
    p.add_argument("--to", type=Path, help="restore: a NEW database path (an existing one is never overwritten)")
    p.add_argument("--db", type=Path, action="append", help="database to back up, repeatable (default: SHORTBOT_DB)")
    p.add_argument("--dir", type=Path, help="backup directory (default: SHORTBOT_BACKUP_DIR or data/backups)")
    p.add_argument("--keep-daily", type=int, help="default: SHORTBOT_BACKUP_KEEP_DAILY or 7")
    p.add_argument("--keep-weekly", type=int, help="default: SHORTBOT_BACKUP_KEEP_WEEKLY or 4")
    p.add_argument("--no-offbox", action="store_true", help="skip SHORTBOT_BACKUP_OFFBOX for this run")
    p.add_argument("--rotate-log", type=Path, action="append", default=[],
                   help="copytruncate this *.log when it is over 10 MB (keeps 5 .gz); repeatable")


def run_cli(args: argparse.Namespace) -> int:
    """Prints the JSON result; returns the exit code (1 on any failure, for cron/launchd)."""
    if args.action == "restore":
        try:
            if not (args.file and args.to):
                raise BackupError("usage: shortbot backup restore FILE --to NEW_PATH")
            out = restore(Path(args.file), args.to)
        except Exception as e:  # noqa: BLE001
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    else:
        env = BackupConfig.from_env()
        cfg = replace(env, dir=args.dir or env.dir, offbox="" if args.no_offbox else env.offbox,
                      keep_daily=env.keep_daily if args.keep_daily is None else args.keep_daily,
                      keep_weekly=env.keep_weekly if args.keep_weekly is None else args.keep_weekly)
        out = run(args.db or default_dbs(), cfg, args.rotate_log)
    print(json.dumps(out, indent=2, default=str))
    return 0 if out["ok"] else 1


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m shortbot.backup", description="verified journal backup / restore")
    add_args(p)
    raise SystemExit(run_cli(p.parse_args()))


if __name__ == "__main__":
    main()
