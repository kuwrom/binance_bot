"""Tables other modules keep in the journal file, created when a Journal opens (#82, as #81 did for the registry).

Created on first use instead, a module's CREATE TABLE needed the write lock whenever the file didn't have the table
yet (a fresh journal, or the first run of the code that added it), and waited for it up to the busy timeout (15 s) on
whatever event loop read the table first: the entry path (warm-up gate), the guardian's target sync, its ladder,
laggard and sweep lanes. A CREATE TABLE IF NOT EXISTS for a table that exists needs no lock; one that creates it does.
The DDL lives here because the Journal can't import those modules (targets imports journal, ladder pulls in numpy,
warmup the exchange layer). A table grows by adding columns to COLUMNS; the Journal migrates old files when it opens.

- target_orders: resting plan targets, write-ahead (targets.py, #65). history: every status change [{at, status, why,
  ex}] (ex: the exchange's own status when it decided)
- ladder_shadow, trial_ledger: the ladder's shadow log and its variants (ladder.py)
- warmups: cold-start warm-ups (warmup.py)
- laggard_events: the sector-laggard study (laggard.py)
- level_sweeps: the level-sweep study (sweeps.py)
"""

from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS target_orders (
    id INTEGER PRIMARY KEY, trade_id INTEGER NOT NULL, symbol TEXT NOT NULL, target REAL NOT NULL, price REAL NOT NULL,
    qty REAL NOT NULL, client_id TEXT NOT NULL UNIQUE, order_id TEXT, status TEXT NOT NULL,
    filled REAL NOT NULL DEFAULT 0, avg REAL, fill_ms INTEGER, created_at TEXT NOT NULL, updated_at TEXT, why TEXT,
    history TEXT NOT NULL DEFAULT '[]', mode TEXT NOT NULL DEFAULT 'demo'
);
CREATE TABLE IF NOT EXISTS ladder_shadow (id INTEGER PRIMARY KEY, ts TEXT, trade_id INTEGER, symbol TEXT,
    price REAL, score REAL, parts TEXT, levels TEXT, x_star REAL, x_actual REAL, x_virtual REAL, action TEXT);
CREATE TABLE IF NOT EXISTS trial_ledger (variant TEXT PRIMARY KEY, family TEXT, params TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS warmups (
    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, t0_ms INTEGER NOT NULL, symbols TEXT NOT NULL,
    snap0 TEXT NOT NULL, snap1 TEXT, verdicts TEXT, checked_at TEXT, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS laggard_events (id INTEGER PRIMARY KEY, ts TEXT, ts_ms INTEGER, sector TEXT,
    leader TEXT, leader_ret4h REAL, laggard TEXT, laggard_ret4h REAL, laggard_px REAL, control TEXT,
    control_ret4h REAL, control_px REAL, breadth REAL, outcome TEXT, labelled_at TEXT);
CREATE TABLE IF NOT EXISTS level_sweeps (symbol TEXT, ts INTEGER, kind TEXT, tier INTEGER, level REAL,
    data TEXT, PRIMARY KEY (symbol, ts, kind, level));
"""
COLUMNS: dict[str, dict[str, str]] = {
    "ladder_shadow": {"variant": "TEXT DEFAULT 'base'", "stop_v": "REAL", "entry": "REAL", "init_r": "REAL"},
}
