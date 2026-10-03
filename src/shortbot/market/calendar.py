"""Macro calendar (docs/SESSIONS.md): scheduled US releases that move BTC, and alts harder via beta.
Sources (all free, public):
- BLS iCal: exact release times (CPI, Employment Situation = NFP, PPI).
- Federal Reserve FOMC calendar: statement at 14:00 ET on the second meeting day (entered with its source).
- ForexFactory weekly JSON (unofficial; backup, and the only free source with PCE/GDP times): USD high-impact.
Entry windows: tier-1 (CPI/NFP/PCE) -15/+30 min, FOMC -30/+60 min, maintenance ±30 min."""

from __future__ import annotations

import re
import sqlite3
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import truststore

from .. import clock
from ..journal_writer import Writer, connect

NY = ZoneInfo("America/New_York")
BLS_ICS = "https://www.bls.gov/schedule/news_release/bls.ics"
FF_WEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
UA = {"User-Agent": "shortbot-research/0.1 (personal research desk)"}
# kind -> (minutes before, minutes after) during which NEW entries are vetoed
WINDOWS = {"tier1": (15, 30), "fomc": (30, 60), "maintenance": (30, 30), "tier2": (5, 15)}
BLS_KINDS = {"Consumer Price Index": "tier1", "Employment Situation": "tier1", "Producer Price Index": "tier2"}
FF_TIER1 = ("CPI", "Non-Farm", "PCE", "Unemployment Rate")
FOMC_2026 = ("2026-10-28", "2026-12-09")  # second meeting day, 14:00 ET (Fed calendar, fetched 2026-09-28)

SCHEMA = """
CREATE TABLE IF NOT EXISTS macro_events (
    id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
    UNIQUE (ts_ms, name)
);
"""


def _ny_ms(date: str, hh: int = 14, mm: int = 0) -> int:
    d = datetime.fromisoformat(date).replace(hour=hh, minute=mm, tzinfo=NY)
    return int(d.timestamp() * 1000)


def parse_bls_ics(text: str) -> list[tuple[int, str, str]]:
    out = []
    for stamp, summary in re.findall(r"DTSTART;TZID=US-Eastern:(\d{8}T\d{6}).*?SUMMARY:(.*?)\r?\n", text, re.S):
        kind = BLS_KINDS.get(summary.strip())
        if kind:
            dt = datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=NY)
            out.append((int(dt.timestamp() * 1000), summary.strip(), kind))
    return out


def parse_ff(rows: list[dict[str, Any]]) -> list[tuple[int, str, str]]:
    out = []
    for r in rows:
        if r.get("country") != "USD" or r.get("impact") != "High" or not r.get("date"):
            continue
        title = r.get("title", "")
        kind = "fomc" if "FOMC Statement" in title or "Federal Funds Rate" in title else \
            "tier1" if any(k in title for k in FF_TIER1) else "tier2"
        out.append((int(datetime.fromisoformat(r["date"]).timestamp() * 1000), title, kind))
    return out


class MacroCalendar:
    def __init__(self, db_path: Path | str):
        self.db = connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.w = Writer(self.db)

    def add(self, ts_ms: int, name: str, kind: str, source: str) -> None:
        if kind not in WINDOWS:
            raise ValueError(f"kind must be one of {tuple(WINDOWS)}")
        self.db.execute("INSERT OR IGNORE INTO macro_events (ts_ms, name, kind, source) VALUES (?,?,?,?)",
                        (ts_ms, name, kind, source))
        self.db.commit()

    async def refresh(self) -> dict[str, Any]:
        """Pull BLS + ForexFactory (+ the known FOMC dates). Each source is optional."""
        got: dict[str, Any] = {}
        events = [(_ny_ms(d), "FOMC statement", "fomc", FED_URL) for d in FOMC_2026]
        async with httpx.AsyncClient(timeout=20, headers=UA,
                                     verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)) as c:
            for name, url, parse in (("bls", BLS_ICS, lambda r: parse_bls_ics(r.text)),
                                     ("forexfactory", FF_WEEK, lambda r: parse_ff(r.json()))):
                try:
                    r = await c.get(url)
                    r.raise_for_status()
                    rows = parse(r)
                    events += [(ts, n, k, url) for ts, n, k in rows]
                    got[name] = len(rows)
                except Exception as e:  # noqa: BLE001 - calendar is best-effort; missing = no veto
                    got[name] = f"error: {type(e).__name__}"
        await self.w.write(self._add_all, events)   # one unit (#79)
        return got

    def _add_all(self, events: list[tuple[int, str, str, str]]) -> None:
        for e in events:
            self.add(*e)

    def around(self, now_ms: int | None = None, hours: float = 48) -> list[dict[str, Any]]:
        now_ms = now_ms or clock.now_ms()
        rows = [dict(r) for r in self.db.execute(
            "SELECT * FROM macro_events WHERE ts_ms BETWEEN ? AND ? ORDER BY ts_ms",
            (now_ms - 2 * 3_600_000, now_ms + int(hours * 3_600_000))).fetchall()]
        for r in rows:
            r["minutes_to"] = round((r["ts_ms"] - now_ms) / 60_000, 1)
            r["utc"] = datetime.fromtimestamp(r["ts_ms"] / 1000, timezone.utc).isoformat(timespec="minutes")
        return rows


def blocking(events: list[dict[str, Any]]) -> list[str]:
    """Events whose entry window covers now (events carry minutes_to)."""
    out = []
    for e in events:
        before, after = WINDOWS.get(e["kind"], (0, 0))
        if -after <= e["minutes_to"] <= before:
            out.append(f"{e['name']} ({e['kind']}) at {e['utc']}: no new entries from -{before} to +{after} min")
    return out
