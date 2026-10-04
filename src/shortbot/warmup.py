"""Cold-start warm-up (docs/SESSIONS.md): when the desk has been idle (no trade activity for 6h, or none yet
today), entries wait for a 15-minute study: snapshot the candidates at t0 and t+15 and drop the ones whose
picture changed. Every warm-up is stored, so the delay's value is MEASURED (persisted vs dropped) rather than
assumed. Thresholds are starting assumptions, to be tuned by that study. A warm desk trades without it. The `warmups`
table is created when the journal opens (journal_tables.py, #82)."""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import statistics
import time
from datetime import UTC, datetime
from typing import Any, TypeGuard

from .exchange import to_symbol
from .journal_tables import NOT_DRILL

COLD_AFTER_H = 6.0
WARMUP_MIN = 15.0


def _ts(iso: str | None) -> datetime | None:
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def last_activity(j) -> datetime | None:
    """Latest trade open/close or completed warm-up of this desk. A plumbing drill's trade is no activity (#46)."""
    row = j.db.execute(f"SELECT MAX(COALESCE(closed_at, opened_at)) FROM trades WHERE {NOT_DRILL} AND mode=?",
                       (j.mode,)).fetchone()
    wu = j.db.execute("SELECT MAX(checked_at) FROM warmups WHERE status='done' AND mode=?", (j.mode,)).fetchone()
    stamps = [t for t in (_ts(row[0]), _ts(wu[0])) if t]
    return max(stamps) if stamps else None


def desk_state(j, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    if j.open_trades(include_opening=True):
        return {"cold": False, "why": "positions open: the desk is live"}
    last = last_activity(j)
    if last is None:
        return {"cold": True, "why": "no trade history yet"}
    gap_h = (now - last).total_seconds() / 3600
    cold = gap_h >= COLD_AFTER_H or last.date() < now.date()
    return {"cold": cold, "last_activity": last.isoformat(timespec="minutes"), "idle_hours": round(gap_h, 1),
            "why": (f"idle {gap_h:.1f}h" if gap_h >= COLD_AFTER_H else "first session of the UTC day") if cold
            else f"active {gap_h:.1f}h ago"}


def gate(j, now: datetime | None = None) -> str | None:
    """Blocker text for a new entry when the desk is cold and no warm-up has completed since."""
    st = desk_state(j, now)
    if not st["cold"]:
        return None
    return (f"desk is cold ({st['why']}): run the 15-min warm-up first (`shortbot warmup start`, then "
            "`warmup check` after 15 min)")


def drop_reasons(s0: dict[str, Any], s1: dict[str, Any], btc_ret_15m: float | None) -> list[str]:
    """Did the picture change enough in 15 minutes to skip this candidate? (pure)"""
    out = []
    if s1.get("last") and s0.get("high_6h") and s1["last"] > s0["high_6h"]:
        out.append(f"new local high {s1['last']} > 6h high {s0['high_6h']} at t0: the push is still on")
    if s0.get("oi") and s1.get("oi") and s0.get("last") and s1.get("last"):
        d_oi = (s1["oi"] / s0["oi"] - 1) * 100
        if d_oi > 3 and s1["last"] > s0["last"]:
            out.append(f"OI +{d_oi:.1f}% with price rising: fresh longs are still coming in")
    if (s0.get("funding_rate") or 0) >= 0 > (s1.get("funding_rate") or 0):
        out.append("funding flipped negative: shorts are now the crowd")
    if s1.get("funding_interval_h") == 1 and s0.get("funding_interval_h") != 1:
        out.append("funding switched to 1h (cap hit)")
    if s0.get("spread_bp") and s1.get("spread_bp") and s1["spread_bp"] > 2 * s0["spread_bp"] and s1["spread_bp"] > 5:
        out.append(f"spread widened {s0['spread_bp']:.1f} -> {s1['spread_bp']:.1f} bp")
    if s0.get("depth_1pct_usd") and s1.get("depth_1pct_usd") and s1["depth_1pct_usd"] < 0.5 * s0["depth_1pct_usd"]:
        out.append("book depth within 1% halved")
    if btc_ret_15m is not None and btc_ret_15m > 1.0:
        out.append(f"BTC +{btc_ret_15m:.2f}% in 15 min: alts get dragged up")
    return out


def _ok(v: object) -> TypeGuard[Any]:
    """A gather(return_exceptions=True) answer that is not an exception (ccxt's answers are Any)."""
    return not isinstance(v, BaseException)


async def snapshot(ex, symbol: str) -> dict[str, Any]:
    pub = ex.public
    tk, fr, oi, ob, c1m, c1h, intervals = await asyncio.gather(
        pub.fetch_ticker(symbol), pub.fetch_funding_rate(symbol), pub.fetch_open_interest(symbol),
        pub.fetch_order_book(symbol, limit=100), pub.fetch_ohlcv(symbol, "1m", limit=16),
        pub.fetch_ohlcv(symbol, "1h", limit=6), ex.funding_intervals(), return_exceptions=True)
    s: dict[str, Any] = {"symbol": symbol, "ts": int(time.time() * 1000)}
    if _ok(tk):
        s["last"] = tk.get("last")
    if _ok(ob) and ob["bids"] and ob["asks"] and s.get("last"):   # futures 24h ticker carries no bid/ask
        s["spread_bp"] = round((ob["asks"][0][0] - ob["bids"][0][0]) / s["last"] * 1e4, 2)
    if _ok(fr):
        s.update(mark=fr.get("markPrice"), index=fr.get("indexPrice"), funding_rate=fr.get("fundingRate"))
        if fr.get("markPrice") and fr.get("indexPrice"):
            s["basis_bp"] = round((fr["markPrice"] / fr["indexPrice"] - 1) * 1e4, 2)
    if _ok(oi):
        s["oi"] = oi.get("openInterestAmount")
    if _ok(ob) and s.get("last"):
        lo, hi = s["last"] * 0.99, s["last"] * 1.01
        s["depth_1pct_usd"] = round(sum(p * q for p, q, *_ in ob["bids"] if p >= lo)
                                    + sum(p * q for p, q, *_ in ob["asks"] if p <= hi))
    if _ok(c1m) and len(c1m) > 2:
        rets = [math.log(b[4] / a[4]) for a, b in itertools.pairwise(c1m) if a[4] and b[4]]
        s["rv_1m_pct"] = round(statistics.pstdev(rets) * 100, 4) if rets else None
    if _ok(c1h) and c1h:
        s["high_6h"] = max(c[2] for c in c1h)
    if _ok(intervals):
        from .exchange import to_raw
        s["funding_interval_h"] = (intervals.get(to_raw(symbol)) or {}).get("interval_h") or 8
    return s


async def btc_15m_return(ex) -> float | None:
    try:
        c = await ex.public.fetch_ohlcv("BTC/USDT:USDT", "1m", limit=16)
        return round((c[-1][4] / c[0][1] - 1) * 100, 3)
    except Exception:  # noqa: BLE001
        return None


class WarmUp:
    def __init__(self, ex, journal):
        self.ex, self.j = ex, journal

    async def default_symbols(self) -> list[str]:
        pending = [r[0] for r in self.j.db.execute("SELECT DISTINCT symbol FROM entries WHERE status='pending' "
                                                           "AND mode=?", (self.j.mode,))] \
            if self.j.db.execute("SELECT name FROM sqlite_master WHERE name='entries'").fetchone() else []
        perps = await self.ex.coin_perps()
        tickers = await self.ex.public.fetch_tickers()
        movers = sorted((s for s in tickers if s in perps), key=lambda s: tickers[s].get("percentage") or 0,
                        reverse=True)[:5]
        return list(dict.fromkeys(pending + movers))

    async def start(self, symbols: list[str] | None = None) -> dict[str, Any]:
        syms = [to_symbol(s) for s in symbols] if symbols else await self.default_symbols()
        snaps = await asyncio.gather(*(snapshot(self.ex, s) for s in syms))
        btc = await btc_15m_return(self.ex)
        now = datetime.now(UTC)
        cur = self.j.db.execute(
            "INSERT INTO warmups (started_at, t0_ms, symbols, snap0, status, mode) VALUES (?,?,?,?,?,?)",
            (now.isoformat(timespec="seconds"), int(now.timestamp() * 1000), json.dumps(syms),
             json.dumps({"coins": snaps, "btc_ret_15m": btc}), "running", self.j.mode))
        self.j.db.commit()
        note = "snapshot taken on a burst minute (:00-:01/:15/:30/:45); fine, but prefer :02-:13" \
            if now.minute % 15 < 2 else None
        return {"warmup_id": cur.lastrowid, "symbols": syms, "check_after": datetime.fromtimestamp(
            now.timestamp() + WARMUP_MIN * 60, UTC).isoformat(timespec="minutes"), "note": note,
            "study": "read the tape meanwhile: regime, governor, clock/macro, charts of these coins"}

    async def check(self, warmup_id: int | None = None) -> dict[str, Any]:
        row = self.j.db.execute("SELECT * FROM warmups WHERE status='running' AND mode=? " + (
            "AND id=? " if warmup_id else "") + "ORDER BY id DESC LIMIT 1",
            (self.j.mode, *((warmup_id,) if warmup_id else ()))).fetchone()
        if not row:
            return {"ok": False, "error": "no running warm-up; start one with `warmup start`"}
        waited = (time.time() * 1000 - row["t0_ms"]) / 60_000
        if waited < WARMUP_MIN - 0.5:
            return {"ok": False, "error": f"only {waited:.1f} of {WARMUP_MIN:.0f} minutes elapsed; keep studying"}
        s0 = json.loads(row["snap0"])
        syms = json.loads(row["symbols"])
        snaps1 = await asyncio.gather(*(snapshot(self.ex, s) for s in syms))
        btc = await btc_15m_return(self.ex)
        by0 = {s["symbol"]: s for s in s0["coins"]}
        verdicts: dict[str, dict[str, Any]] = {}
        for s1 in snaps1:
            reasons = drop_reasons(by0.get(s1["symbol"], {}), s1, btc)
            verdicts[s1["symbol"]] = {"persisted": not reasons, "drop_reasons": reasons,
                                      "move_pct": round((s1["last"] / by0[s1["symbol"]]["last"] - 1) * 100, 2)
                                      if s1.get("last") and by0.get(s1["symbol"], {}).get("last") else None}
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self.j.db.execute("UPDATE warmups SET snap1=?, verdicts=?, checked_at=?, status='done' WHERE id=?",
                          (json.dumps({"coins": snaps1, "btc_ret_15m": btc}), json.dumps(verdicts), now, row["id"]))
        self.j.db.commit()
        self.j.log_decision("warmup_done", f"warm-up #{row['id']} after {waited:.0f} min: "
                            f"{sum(v['persisted'] for v in verdicts.values())}/{len(verdicts)} persisted", None,
                            snapshot=verdicts)
        return {"ok": True, "warmup_id": row["id"], "minutes": round(waited, 1), "btc_ret_15m": btc,
                "verdicts": verdicts, "desk": "warm: entries allowed (governor and vetoes still apply)"}


def desk_clock(settings, j) -> dict[str, Any]:
    """Clock + macro calendar (next 7 days) + cold/warm state: what time it is for the desk."""
    from .market.calendar import MacroCalendar, blocking
    from .sessions import clock
    events = MacroCalendar(settings.db_path).around(hours=24 * 7)
    running = j.db.execute("SELECT id, started_at FROM warmups WHERE status='running' AND mode=? ORDER BY id DESC",
                           (j.mode,)).fetchone()
    return {"clock": clock(), "desk": desk_state(j), "macro_blocking_now": blocking(events),
            "macro_week": [{k: e[k] for k in ("utc", "name", "kind", "minutes_to")} for e in events],
            "warmup_running": dict(running) if running else None}
