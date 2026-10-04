"""Guardian uptime (#46): when the desk's protection went unverified, as rows a go-live report can read.

- The heartbeat (killswitch.HEARTBEAT_KEY) is stamped by a positions pass that verified every held position (#61).
  Guardian._heartbeat hands each stamp here with the one before it: this process's last, or, at its first stamp, the
  previous process's (the kv value it left). A gap over GAP_S (3 positions passes, killswitch.VERIFIED_S) is a row in
  `guardian_gaps` (journal_tables.py) from the last verified stamp to this one, so a guardian that was down, hung, or
  ran with its heartbeat withheld (a failing fill poll) counts the same. A guardian that is down now has no row yet:
  summary() reports that open gap from the heartbeat's age.
- A gap overlapping a drill window (kv drill:window:<name>, which drill.py holds while a drill runs: the restart
  drill's planned outage) is tagged `drill`; any other gap is untagged, i.e. unexplained downtime.
- kv uptime_since (mode_key): the first verified stamp since this code first ran on this journal. Gap rows cover the
  time from there.
- Writes on the guardian loop are posted (#79, journal_writer.py): nothing here makes it wait for the write lock.
Readers (the go-live report): uptime_since(), gaps(), summary(), windows()."""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("shortbot.uptime")
GAP_S = 180.0                    # 3 missed positions passes: protection unverified (killswitch.VERIFIED_S)
SINCE_KEY = "uptime_since"
WINDOW_KEY = "drill:window:"     # + drill name (mode_key): {"from": epoch s, "until": epoch s}
TAG_DRILL = "drill"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def stamp_of(raw: str | None) -> float | None:
    """A heartbeat kv value as epoch seconds; None when absent or unreadable."""
    with suppress(TypeError, ValueError):
        v = float(raw) if raw is not None else None
        return v if v and v > 0 else None
    return None


# ------------------------------------------------------------ the guardian's side
def beat(j: Any, prev: float | None, at: float, first: bool) -> None:
    """A verified stamp `at` after `prev` (None: none before it). Posted only when it records something: the first
    stamp of a process (uptime_since) or a gap."""
    if first or (prev is not None and at - prev > GAP_S):
        j.post(record, prev, at, os.getpid())


def record(j: Any, prev: float | None, at: float, pid: int) -> None:
    """One unit: uptime_since once, then the gap's row if there is one."""
    j.db.execute("INSERT OR IGNORE INTO kv (k, v) VALUES (?, ?)", (j.mode_key(SINCE_KEY), iso(at)))
    if prev is not None and at - prev > GAP_S:
        tag = TAG_DRILL if in_window(j, prev, at) else None
        j.db.execute("INSERT INTO guardian_gaps (start_at, end_at, secs, mode, pid, tag) VALUES (?,?,?,?,?,?)",
                     (iso(prev), iso(at), round(at - prev, 1), j.mode, pid, tag))
        log.warning("guardian gap: protection unverified for %.0f s (%s -> %s)%s", at - prev, iso(prev), iso(at),
                    f", tagged {tag}" if tag else "")
    j.db.commit()


# ------------------------------------------------------------ drill windows (drill.py)
def _key(j: Any, name: str) -> str:
    return str(j.mode_key(WINDOW_KEY + name))


def open_window(j: Any, name: str, seconds: float, now: float | None = None) -> dict[str, float]:
    """A unit (await j.write(open_window, ...)): a gap overlapping the next `seconds` is the drill's."""
    now = time.time() if now is None else now
    w = {"from": now, "until": now + seconds}
    j.set(_key(j, name), json.dumps(w))
    return w


def close_window(j: Any, name: str, now: float | None = None) -> None:
    """A unit: the window ends now (kept: a gap the guardian posts late still finds it)."""
    now = time.time() if now is None else now
    w = windows(j).get(name)
    if w is not None:
        j.set(_key(j, name), json.dumps({"from": w["from"], "until": min(w["until"], now)}))


def windows(j: Any) -> dict[str, dict[str, float]]:
    """This mode's drill windows by drill name; an unreadable row is skipped (its gap stays untagged)."""
    suffix = "" if j.mode == "demo" else f"@{j.mode}"
    out = {}
    for r in j._rows("SELECT k, v FROM kv WHERE k LIKE ?", (WINDOW_KEY + "%",)):
        name = r["k"][len(WINDOW_KEY):]
        if (suffix and not name.endswith(suffix)) or (not suffix and "@" in name):
            continue
        with suppress(TypeError, ValueError, KeyError):
            w = json.loads(r["v"])
            out[name[:len(name) - len(suffix)]] = {"from": float(w["from"]), "until": float(w["until"])}
    return out


def in_window(j: Any, start: float, end: float) -> bool:
    return any(w["from"] < end and w["until"] > start for w in windows(j).values())


# ------------------------------------------------------------ readers
def uptime_since(j: Any) -> str | None:
    v = j.get(j.mode_key(SINCE_KEY))
    return str(v) if v is not None else None


def gaps(j: Any, since: str | None = None) -> list[dict[str, Any]]:
    """This mode's gaps that end after `since` (ISO UTC; None: all), oldest first."""
    rows: list[dict[str, Any]] = j._rows("SELECT * FROM guardian_gaps WHERE mode=? AND end_at > ? ORDER BY end_at",
                                         (j.mode, since or ""))
    return rows


def summary(j: Any, since: str | None = None, now: float | None = None) -> dict[str, Any]:
    """The gaps from max(since, uptime_since), the longest untagged one, and the gap still open now (the heartbeat's
    age past GAP_S: a guardian down at this moment). covered: uptime_since is set, so rows exist for the window."""
    from .killswitch import HEARTBEAT_KEY
    now = time.time() if now is None else now
    first = uptime_since(j)
    start = max(x for x in (since, first, "") if x is not None) or None
    rows = gaps(j, start)
    hb = stamp_of(j.get(HEARTBEAT_KEY))
    age = None if hb is None else max(0.0, now - hb)
    untagged = [r["secs"] for r in rows if not r["tag"]]
    return {"since": start, "uptime_since": first, "covered": first is not None, "gaps": rows,
            "downtime_s": round(sum(r["secs"] for r in rows), 1), "untagged_max_s": max(untagged, default=0.0),
            "heartbeat_age_s": None if age is None else round(age, 1),
            "open_gap_s": round(age, 1) if age is not None and age > GAP_S else None}
