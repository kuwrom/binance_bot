"""Scheduled supply events: token unlocks (strategy #1, docs/STRATEGY.md). No free unlock API exists, so events
are entered with their SOURCE (e.g. from project docs or public unlock pages researched by the analyst).
Keyrock (16k unlocks): ~90% show negative pressure starting ~30 days before; team/cliff unlocks worst (~−25%),
investor mild, ecosystem slightly positive; larger unlocks fall harder."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS unlocks (
    id INTEGER PRIMARY KEY, base TEXT NOT NULL, unlock_ts_ms INTEGER NOT NULL, pct_supply REAL,
    recipient TEXT NOT NULL, source TEXT NOT NULL, notes TEXT, added_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_unlock_base ON unlocks (base, unlock_ts_ms);
"""
RECIPIENTS = ("team", "investor", "cliff", "ecosystem", "community", "other")
BEARISH_RECIPIENTS = {"team", "investor", "cliff"}


class EventStore:
    def __init__(self, db_path: Path | str):
        self.db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def add_unlock(self, base: str, date: str, pct_supply: float | None, recipient: str, source: str,
                   notes: str = "") -> int:
        if recipient not in RECIPIENTS:
            raise ValueError(f"recipient must be one of {RECIPIENTS}")
        if not source.strip():
            raise ValueError("a source (URL or document) is required for every unlock")
        dt = datetime.fromisoformat(date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        cur = self.db.execute(
            "INSERT INTO unlocks (base, unlock_ts_ms, pct_supply, recipient, source, notes, added_at)"
            " VALUES (?,?,?,?,?,?,?)", (base.upper().removeprefix("1000"), int(dt.timestamp() * 1000), pct_supply,
                                       recipient, source, notes, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        self.db.commit()
        return int(cur.lastrowid)

    def upcoming(self, base: str | None = None, days: float = 60, include_past_days: float = 7) -> list[dict[str, Any]]:
        now = time.time() * 1000
        q = "SELECT * FROM unlocks WHERE unlock_ts_ms BETWEEN ? AND ?"
        args: list[Any] = [now - include_past_days * 86_400_000, now + days * 86_400_000]
        if base:
            q += " AND base=?"
            args.append(base.upper().removeprefix("1000"))
        rows = [dict(r) for r in self.db.execute(q + " ORDER BY unlock_ts_ms", args).fetchall()]
        for r in rows:
            r["days_to_unlock"] = round((r["unlock_ts_ms"] - now) / 86_400_000, 2)
            r["date"] = datetime.fromtimestamp(r["unlock_ts_ms"] / 1000, timezone.utc).isoformat(timespec="minutes")
        return rows
