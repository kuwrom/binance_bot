"""Laggard catch-up study (docs/LADDER.md A): when a sector leader shows ACCEPTANCE below its recent 4h low, do its
lagging siblings catch down over 1-12h (short the laggard), or keep lagging? Shadow only. Every event logs the
laggard AND a random non-laggard control from the same sector; the verdict needs +40 bp net over the control
across >= 100 events on >= 30 days (effective N = days, events cluster)."""

from __future__ import annotations

import json
import random
import statistics
import time
from datetime import datetime, timezone
from typing import Any


SCHEMA = """CREATE TABLE IF NOT EXISTS laggard_events (id INTEGER PRIMARY KEY, ts TEXT, ts_ms INTEGER, sector TEXT,
    leader TEXT, leader_ret4h REAL, laggard TEXT, laggard_ret4h REAL, laggard_px REAL, control TEXT,
    control_ret4h REAL, control_px REAL, breadth REAL, outcome TEXT, labelled_at TEXT)"""
MIN_MEMBERS, MIN_QVOL, COOLDOWN_MS = 4, 20e6, 4 * 3_600_000
HORIZONS_H = (1, 2, 4, 8, 12)


def accepted_breakdown(c15: list[list[float]]) -> tuple[bool, float | None]:
    """Last 2 CLOSED 15m bars both close below the low of the 16 bars (4h) before them."""
    closed = c15[:-1]
    if len(closed) < 18:
        return False, None
    level = min(b[3] for b in closed[-18:-2])
    return all(b[4] < level for b in closed[-2:]), level


def pick_laggards(members: list[dict], leader: str, leader_ret: float) -> tuple[list[dict], list[dict]]:
    """Laggard = fell less than half as much as the leader over 4h (same sector); the rest are control candidates."""
    others = [m for m in members if m["symbol"] != leader and m.get("r4h") is not None]
    lag = [m for m in others if m["r4h"] > 0.5 * leader_ret]
    return lag, [m for m in others if m not in lag]


async def detect(ex, journal) -> int:
    from .market.market_map import cached
    m = cached(journal, 1800)
    if not m:
        return 0
    journal.db.execute(SCHEMA)
    tk = await ex.public.fetch_tickers()
    sectors: dict[str, list[dict]] = {}
    for sym, c in m["coins"].items():
        if c.get("sector") and (tk.get(sym, {}).get("quoteVolume") or 0) >= MIN_QVOL:
            sectors.setdefault(c["sector"], []).append({"symbol": sym, **c})
    now_ms, n = int(time.time() * 1000), 0
    for sec, mem in sectors.items():
        if len(mem) < MIN_MEMBERS:
            continue
        leader = max(mem, key=lambda x: tk[x["symbol"]].get("quoteVolume") or 0)
        last = journal.db.execute("SELECT MAX(ts_ms) FROM laggard_events WHERE leader=?", (leader["symbol"],)).fetchone()[0]
        if last and now_ms - last < COOLDOWN_MS:
            continue
        c15 = await ex.public.fetch_ohlcv(leader["symbol"], "15m", limit=20)
        ok, _ = accepted_breakdown(c15)
        if not ok or (leader.get("r4h") or 0) >= 0:
            continue
        lag, rest = pick_laggards(mem, leader["symbol"], leader["r4h"])
        for g in lag:
            ctl = random.choice(rest) if rest else None
            journal.db.execute(
                "INSERT INTO laggard_events (ts, ts_ms, sector, leader, leader_ret4h, laggard, laggard_ret4h, "
                "laggard_px, control, control_ret4h, control_px, breadth) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), now_ms, sec, leader["symbol"],
                 leader["r4h"], g["symbol"], g["r4h"], tk[g["symbol"]]["last"], ctl and ctl["symbol"],
                 ctl and ctl["r4h"], ctl and tk[ctl["symbol"]]["last"], m["header"]["breadth_pct"]))
            n += 1
    journal.db.commit()
    return n


def outcome(p0: float, c15: list[list[float]], gap_pct: float | None = None) -> dict[str, Any]:
    """Short returns (%) at each horizon, MFE/MAE over 12h, and whether/when the catch-up target was hit."""
    out: dict[str, Any] = {}
    for h in HORIZONS_H:
        k = h * 4
        out[f"r{h}h"] = round((p0 - c15[k - 1][4]) / p0 * 100, 3) if len(c15) >= k else None
    seg = c15[:48]
    if seg:
        out["mfe"] = round((p0 - min(b[3] for b in seg)) / p0 * 100, 3)
        out["mae"] = round((max(b[2] for b in seg) - p0) / p0 * 100, 3)
    if gap_pct and gap_pct > 0:
        target = p0 * (1 - gap_pct / 100)
        hit = next((i for i, b in enumerate(seg) if b[3] <= target), None)
        out["catchup_hit"], out["catchup_minutes"] = hit is not None, None if hit is None else (hit + 1) * 15
    return out


async def label(ex, journal, limit: int = 20) -> int:
    journal.db.execute(SCHEMA)
    cutoff = int(time.time() * 1000) - 12 * 3_600_000 - 900_000
    rows = journal.db.execute("SELECT id, ts_ms, leader_ret4h, laggard, laggard_ret4h, laggard_px, control, control_px"
                              " FROM laggard_events WHERE labelled_at IS NULL AND ts_ms < ? LIMIT ?",
                              (cutoff, limit)).fetchall()
    for rid, ts, lr, lag, lagr, lagp, ctl, ctlp in rows:
        o: dict[str, Any] = {}
        try:
            c = await ex.public.fetch_ohlcv(lag, "15m", since=ts, limit=50)
            o["laggard"] = outcome(lagp, c, (lagr - lr) if lr is not None and lagr is not None else None)
            if ctl:
                o["control"] = outcome(ctlp, await ex.public.fetch_ohlcv(ctl, "15m", since=ts, limit=50))
        except Exception:  # noqa: BLE001
            pass
        journal.db.execute("UPDATE laggard_events SET outcome=?, labelled_at=? WHERE id=?",
                           (json.dumps(o), datetime.now(timezone.utc).isoformat(timespec="seconds"), rid))
    journal.db.commit()
    return len(rows)


def report(journal) -> dict[str, Any]:
    journal.db.execute(SCHEMA)
    rows = journal.db.execute("SELECT ts, outcome FROM laggard_events WHERE outcome IS NOT NULL").fetchall()
    pending = journal.db.execute("SELECT COUNT(*) FROM laggard_events WHERE outcome IS NULL").fetchone()[0]
    res: dict[str, Any] = {"labelled_events": len(rows), "pending": pending}
    for h in HORIZONS_H:
        by_day: dict[str, list[float]] = {}
        for ts, o in rows:
            o = json.loads(o)
            a, b = (o.get("laggard") or {}).get(f"r{h}h"), (o.get("control") or {}).get(f"r{h}h")
            if a is not None and b is not None:
                by_day.setdefault(ts[:10], []).append(a - b)
        days = [statistics.fmean(v) for v in by_day.values()]
        if days:
            m = statistics.fmean(days)
            se = statistics.stdev(days) / len(days) ** 0.5 if len(days) > 1 else None
            res[f"{h}h"] = {"days": len(days), "events": sum(len(v) for v in by_day.values()),
                            "laggard_minus_control_short_ret_pct": round(m, 3), "t_by_day": round(m / se, 2) if se else None}
    res["go_rule"] = "+0.40% net over control, >= 100 events on >= 30 days, t >= 3 (clustered by day); test both signs"
    return res
