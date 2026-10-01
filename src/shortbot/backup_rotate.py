"""Retention for backup.py: grandfather-father-son rotation of the verified copies, and copytruncate rotation for a log
that a running process holds open (the nohup'd guardian's `>> data/logs/guardian.log` on the Mac).

Rotation only ever deletes names backup.py generated (`<prefix>-YYYYMMDDTHHMMSSZ.db.gz` and its `.json` manifest), so a
copy renamed by hand (e.g. `hist-build-0929.db.gz`) is pinned forever."""

from __future__ import annotations

import gzip
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

STAMP = "%Y%m%dT%H%M%SZ"
SUFFIX = ".db.gz"
LOG_MAX_BYTES = 10 * 2**20
LOG_KEEP = 5


def manifest_of(copy: Path) -> Path:
    """shortbot-20261001T031700Z.db.gz -> shortbot-20261001T031700Z.json"""
    return copy.with_name(copy.name.removesuffix(SUFFIX) + ".json")


def snapshots(d: Path, prefix: str) -> list[tuple[datetime, Path]]:
    """This prefix's generated copies in d, newest first."""
    pat = re.compile(re.escape(prefix) + r"-(\d{8}T\d{6}Z)" + re.escape(SUFFIX))
    out = []
    for p in d.iterdir():
        if m := pat.fullmatch(p.name):
            out.append((datetime.strptime(m[1], STAMP).replace(tzinfo=timezone.utc), p))
    return sorted(out, reverse=True)


def keep_set(stamps: list[datetime], keep_daily: int, keep_weekly: int) -> set[datetime]:
    """Newest copy of each of the last keep_daily UTC days and of the last keep_weekly ISO weeks that have one
    (restic's --keep-daily/--keep-weekly). The newest copy is always kept, even with 0/0."""
    newest_first = sorted(stamps, reverse=True)
    keep, days, weeks = set(newest_first[:1]), set(), set()
    for t in newest_first:
        day, week = t.date(), t.isocalendar()[:2]
        if day not in days and len(days) < keep_daily:
            days.add(day)
            keep.add(t)
        if week not in weeks and len(weeks) < keep_weekly:
            weeks.add(week)
            keep.add(t)
    return keep


def rotate(d: Path, prefix: str, keep_daily: int, keep_weekly: int) -> list[str]:
    """Delete this prefix's copies (with their manifests) that no rule keeps; returns the deleted names."""
    snaps = snapshots(d, prefix)
    keep = keep_set([t for t, _ in snaps], keep_daily, keep_weekly)
    gone = []
    for t, p in snaps:
        if t not in keep:
            p.unlink()
            manifest_of(p).unlink(missing_ok=True)
            gone.append(p.name)
    return gone


def rotate_log(path: Path, max_bytes: int = LOG_MAX_BYTES, keep: int = LOG_KEEP) -> str:
    """copytruncate: when the log is over max_bytes, copy it aside, truncate it in place, then gzip the copy to
    <log>.1.gz (older archives shift up to .<keep>.gz). The writer keeps its O_APPEND descriptor (`>>`), so its next
    line lands at offset 0. Lines written between the copy and the truncate are lost (same window as logrotate's
    copytruncate); the copy is a plain file copy, so that window is tiny. If gzipping fails, the plain copy stays
    behind as .<log>.rotating and later runs refuse to rotate until a human has looked at it."""
    raw, gz = path.with_name(f".{path.name}.rotating"), path.with_name(f".{path.name}.rotating.gz")
    if raw.exists():
        raise FileExistsError(f"{raw} is left from a failed rotation; keep or delete it by hand")
    if not path.exists() or path.stat().st_size <= max_bytes:
        return "skipped"
    shutil.copyfile(path, raw)
    os.truncate(path, 0)
    with open(raw, "rb") as f, gzip.open(gz, "wb") as g:
        shutil.copyfileobj(f, g, 2**20)
    arch = [path.with_name(f"{path.name}.{i}.gz") for i in range(keep + 1)]   # arch[0] unused
    arch[keep].unlink(missing_ok=True)
    for i in range(keep - 1, 0, -1):
        if arch[i].exists():
            os.replace(arch[i], arch[i + 1])
    os.replace(gz, arch[1])
    raw.unlink()
    return f"rotated to {path.name}.1.gz"
