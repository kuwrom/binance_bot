"""Host-wide weight ledger: the IP's request-weight budget shared by every shortbot process on the host (#70 follow-up).

Why: Binance limits request weight per IP ("The limits on the API are based on the IPs, not the API keys",
https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info). weight.py alone is per process: it
trusts `x-mbx-used-weight-1m` from its own responses, so the guardian, the MCP servers, the CLI, lab and ad-hoc scripts
each saw only their own share, and a 429/418 seen by one was news to the others. On 2026-10-07 15:35 UTC a raw-urllib
script (472 klines limit=1000 + ticker/24hr, 8 threads, ~2,400 weight in under a minute) got the IP banned 14 min.

One small SQLite file (WAL, synchronous=OFF: it holds seconds-old state, nothing to keep) under data/:
- `hosts`: per host the freshest used-weight reading (wall time), the learned exchangeInfo limit, and until when a
  429/418/-1003 says the host is spent (`full_until`) and research cools down (`cooldown_until`).
- `calls`: one row per admitted non-protective request: its up-front cost (in flight until its response, at most
  INFLIGHT_TTL_S) and its pacing lane. A paced check and its reservation are one IMMEDIATE transaction, so threads and
  processes can't pass the same gap together.

Times are wall-clock seconds (time.time), the only clock processes share. Path (`path()`): $SHORTBOT_WEIGHT_LEDGER
("off" disables it); else, for code running from a linked git worktree with the default (relative) journal path, the
main checkout's data/ (git rev-parse --git-common-dir), so ad-hoc scripts from ~/wt-* share the desk's ledger; else
<data dir>/weight_ledger.db (the journal's directory: Docker's ./data volume is the host's data/).

Never blocks a caller: the process lock and SQLite's busy wait are each bounded by BUSY_MS, and contention raises
Busy, so the caller (weight_share.py) drops the write or falls back to its own readings.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("shortbot.weight")

ENV = "SHORTBOT_WEIGHT_LEDGER"
FILENAME = "weight_ledger.db"
INFLIGHT_TTL_S = 30.0    # a reservation without an answer stops counting after this (a crashed or hung caller)
KEEP_S = 600.0           # rows older than this are pruned
BUSY_MS = 20             # the longest a caller (the guardian's event loop included) waits on a contended ledger
CODE_ROOT = Path(__file__).resolve().parents[2]   # the checkout this code runs from (src/shortbot/ -> repo)

SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (host TEXT PRIMARY KEY, used INTEGER, seen REAL, lim INTEGER,
    full_until REAL NOT NULL DEFAULT 0, cooldown_until REAL NOT NULL DEFAULT 0, last_limit TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, host TEXT NOT NULL, cost REAL NOT NULL, at REAL NOT NULL,
    lane TEXT, lane_cost REAL NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0, pid INTEGER);
CREATE INDEX IF NOT EXISTS calls_host_at ON calls (host, at);
"""


@dataclass(frozen=True)
class Shared:
    """A host's IP-wide state as of `now` (wall seconds)."""
    used: int | None          # the freshest reading, any process
    seen: float | None        # when it was read
    limit: int | None         # REQUEST_WEIGHT per minute, once some process read exchangeInfo
    full_until: float
    cooldown_until: float
    last_limit: str
    inflight: float           # weight admitted but not yet answered, every process


@dataclass(frozen=True)
class Pace:
    """A pacing lane: at most `cap` of lane cost per `window_s` per host, across every process."""
    lane: str
    cost: float
    cap: float
    window_s: float

    def full(self, spent: float) -> str:
        return f"pacing: {spent:.0f}/{self.cap:.0f} {self.lane} in the last {self.window_s:.0f}s on this host"


class Busy(Exception):
    """The ledger is contended right now: the caller doesn't wait for it."""


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()   # one connection per process, shared by its threads
        self._conn: sqlite3.Connection | None = None
        self._pid = 0

    def _db(self) -> sqlite3.Connection:
        if self._conn is None or self._pid != os.getpid():   # never reuse a connection across fork
            self.path.parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(self.path, timeout=BUSY_MS / 1000, isolation_level=None, check_same_thread=False)
            try:
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA synchronous=OFF")
                c.executescript(SCHEMA)
            except BaseException:
                c.close()
                raise
            self._conn, self._pid = c, os.getpid()
        return self._conn

    @contextmanager
    def _session(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        """The connection, held within BUSY_MS (the process lock, then SQLite's busy wait), or Busy. `write`: one
        IMMEDIATE transaction, committed at the end, rolled back on any error."""
        if not self._lock.acquire(timeout=BUSY_MS / 1000):
            raise Busy("weight ledger: another thread of this process holds it")
        try:
            c = self._db()
            if write:
                c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                if write:
                    c.execute("COMMIT")
            except BaseException:
                if write and c.in_transaction:
                    c.execute("ROLLBACK")
                raise
        except sqlite3.OperationalError as e:
            if "locked" in str(e) or "busy" in str(e):
                raise Busy(str(e)) from e
            raise
        finally:
            self._lock.release()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None and self._pid == os.getpid():
                self._conn.close()
            self._conn = None

    # ------------------------------------------------------------ writes from responses
    def note(self, host: str, now: float, used: int | None = None, limit: int | None = None,
             full_until: float | None = None, cooldown_until: float | None = None, last_limit: str | None = None
             ) -> None:
        """One response's news: a reading (kept if it is the freshest), a learned limit, a limit answer."""
        with self._session(write=True) as c:
            c.execute("INSERT OR IGNORE INTO hosts (host) VALUES (?)", (host,))
            if used is not None:
                c.execute("UPDATE hosts SET used = ?, seen = ? WHERE host = ? AND (seen IS NULL OR seen <= ?)",
                          (used, now, host, now))
            if limit is not None:
                c.execute("UPDATE hosts SET lim = ? WHERE host = ?", (limit, host))
            if full_until is not None and cooldown_until is not None:
                c.execute("UPDATE hosts SET full_until = MAX(full_until, ?), cooldown_until = MAX(cooldown_until, ?), "
                          "last_limit = ? WHERE host = ?", (full_until, cooldown_until, last_limit or "", host))

    # ------------------------------------------------------------ reads before a request
    def state(self, host: str, now: float) -> Shared:
        with self._session() as c:
            row = c.execute("SELECT used, seen, lim, full_until, cooldown_until, last_limit FROM hosts WHERE host = ?",
                            (host,)).fetchone()
            inflight = c.execute("SELECT COALESCE(SUM(cost), 0) FROM calls WHERE host = ? AND done = 0 AND at > ?",
                                 (host, now - INFLIGHT_TTL_S)).fetchone()[0]
        if row is None:
            return Shared(None, None, None, 0.0, 0.0, "", float(inflight))
        return Shared(row[0], row[1], row[2], float(row[3]), float(row[4]), str(row[5]), float(inflight))

    def reserve(self, host: str, cost: float, now: float, pace: Pace | None = None) -> tuple[int | None, str | None]:
        """Admit one request: (its reservation id, None), or (None, why) when its pacing lane is full right now."""
        with self._session(write=True) as c:
            if pace is not None:
                spent = c.execute("SELECT COALESCE(SUM(lane_cost), 0) FROM calls WHERE host = ? AND lane = ? AND at > ?",
                                  (host, pace.lane, now - pace.window_s)).fetchone()[0]
                if spent and spent + pace.cost > pace.cap:
                    return None, pace.full(spent)
            cur = c.execute("INSERT INTO calls (host, cost, at, lane, lane_cost, pid) VALUES (?, ?, ?, ?, ?, ?)",
                            (host, cost, now, pace.lane if pace else None, pace.cost if pace else 0, os.getpid()))
            c.execute("DELETE FROM calls WHERE at < ?", (now - KEEP_S,))
            return cur.lastrowid, None

    def done(self, rid: int) -> None:
        """The request was answered (or failed): its cost is no longer in flight."""
        with self._session() as c:
            c.execute("UPDATE calls SET done = 1 WHERE id = ?", (rid,))


# ------------------------------------------------------------------ where the ledger lives
_ledgers: dict[Path, Ledger] = {}
_ledgers_lock = threading.Lock()
_paths: dict[tuple[str, str | None, str], Path | None] = {}


def main_checkout(code_root: Path) -> Path | None:
    """The main checkout when `code_root` is a linked git worktree of it (its .git is a file), else None: a main
    checkout, or an installed copy (Docker's /app) has no worktree."""
    if not (code_root / ".git").is_file():
        return None
    try:
        r = subprocess.run(["git", "-C", str(code_root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    common = Path(r.stdout.strip())
    return common.parent if r.returncode == 0 and common.name == ".git" else None


def path() -> Path | None:
    """The ledger's file for the current environment (resolved once per $SHORTBOT_WEIGHT_LEDGER, $SHORTBOT_DB, cwd)."""
    env = os.getenv(ENV, "").strip()
    key = (env, os.getenv("SHORTBOT_DB"), os.getcwd())
    if key not in _paths:
        _paths[key] = _resolve(env)
    return _paths[key]


def _resolve(env: str) -> Path | None:
    if env.lower() in ("off", "0", "none"):
        return None
    if env:
        return Path(env).expanduser().resolve()
    # lazy: loads config (.env), as every SHORTBOT_DB reader does
    from .provenance import data_dir
    here = data_dir()
    if os.getenv("SHORTBOT_DB") is None:   # the default, relative data/: a worktree's own, not the desk's
        main = main_checkout(CODE_ROOT)
        if main is not None and (main / "data").is_dir():
            return main / "data" / FILENAME
    return here / FILENAME


def get() -> Ledger | None:
    """This process's ledger for the current path (tests change the path per test)."""
    p = path()
    if p is None:
        return None
    with _ledgers_lock:
        led = _ledgers.get(p)
        if led is None:
            led = _ledgers[p] = Ledger(p)
        return led


def close_all() -> None:
    with _ledgers_lock:
        for led in _ledgers.values():
            led.close()
        _ledgers.clear()
