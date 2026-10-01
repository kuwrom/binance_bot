"""Day / week P&L and an equity peak that survive midnight carry-overs and transfers (task #16).

The old definition (reconcile.day_pnl) is realized income since 00:00 UTC + ALL unrealized P&L, so a loss carried in
an open position trips the daily cap at midnight with no new loss, and deposits/withdrawals move `equity_peak` (a 12%
withdrawal looks like a 12% drawdown). Here:
- marks: the first equity seen in each UTC day and ISO week is stored in kv (`eq_mark:day:<date>`,
  `eq_mark:week:<year-Www>`); period P&L = equity now - mark - net TRANSFER income since the mark.
- a LATE mark (first seen > 15 min after the period began, e.g. the guardian slept across midnight) cannot see the
  loss before it, so the period P&L is the worse of the new and the old definition: the caps never miss a loss.
- peak: `equity_peak` moves with transfers (a deposit raises it, a withdrawal lowers it), so only trading P&L is
  drawdown; `reset_peak` is the human reset (shortbot-admin).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import reconcile

LATE_MARK_S = 15 * 60
KV_PEAK = "equity_peak"
KV_CURSOR = "eq_transfer_cursor_ms"


def period_starts(now: datetime) -> dict[str, tuple[str, int]]:
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week0 = day0 - timedelta(days=day0.weekday())
    iso = now.isocalendar()
    return {"day": (day0.date().isoformat(), int(day0.timestamp() * 1000)),
            "week": (f"{iso[0]}-W{iso[1]:02d}", int(week0.timestamp() * 1000))}


def _mark(journal, kind: str, key: str, equity: float, now_ms: int) -> dict[str, Any]:
    k = f"eq_mark:{kind}:{key}"
    raw = journal.get(k)
    if raw:
        return json.loads(raw)
    m = {"equity": equity, "ts": now_ms}
    journal.set(k, json.dumps(m))
    return m


def transfers(rows: list[dict[str, Any]], since_ms: int) -> float:
    return sum(float(r.get("income") or 0) for r in rows
               if r.get("incomeType") == "TRANSFER" and int(r.get("time") or 0) >= since_ms)


def period_pnl(journal, equity: float, transfer_rows: list[dict[str, Any]], old_day: float, old_week: float,
               now: datetime | None = None) -> dict[str, Any]:
    """{'day_pnl', 'week_pnl', 'late': {...}, 'degraded': bool} from the marks. Never raises: any failure (kv lock,
    bad mark) returns the old definitions with degraded=True (the kill switch calls this inside guardian hooks)."""
    try:
        now = now or datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        out: dict[str, Any] = {"late": {}, "degraded": False}
        for kind, old in (("day", old_day), ("week", old_week)):
            key, start_ms = period_starts(now)[kind]
            m = _mark(journal, kind, key, equity, now_ms)
            pnl = equity - float(m["equity"]) - transfers(transfer_rows, int(m["ts"]))
            late = int(m["ts"]) - start_ms > LATE_MARK_S * 1000
            out["late"][kind] = late
            out[f"{kind}_pnl"] = round(min(pnl, old) if late else pnl, 4)
        return out
    except Exception:  # noqa: BLE001
        return {"day_pnl": old_day, "week_pnl": old_week, "late": {}, "degraded": True}


def adjusted_peak(journal, equity: float, transfer_rows: list[dict[str, Any]], now_ms: int | None = None) -> float:
    """equity_peak shifted by transfers since the last call, never below current equity."""
    now_ms = now_ms or int(time.time() * 1000)
    cursor = int(journal.get(KV_CURSOR) or now_ms)
    peak = float(journal.get(KV_PEAK) or 0.0) + transfers(transfer_rows, cursor)
    peak = max(peak, equity)
    journal.set(KV_PEAK, str(peak))
    journal.set(KV_CURSOR, str(now_ms))
    return peak


def reset_peak(journal, equity: float, reason: str) -> dict[str, Any]:
    """Human reset (shortbot-admin): the drawdown is measured from today's equity from now on."""
    old = journal.get(KV_PEAK)
    journal.set(KV_PEAK, str(equity))
    journal.set(KV_CURSOR, str(int(time.time() * 1000)))
    journal.log_decision("equity_peak_reset", f"{old} -> {equity}: {reason}", None,
                         snapshot={"old": old, "new": equity, "reason": reason})
    return {"ok": True, "old_peak": float(old) if old else None, "new_peak": equity}


async def gather(t, journal, equity: float, old_day: float, old_week: float) -> dict[str, Any]:
    """One income read covering the week mark and the peak cursor; returns day/week P&L and the adjusted peak.
    Income unreachable -> old definitions and the stored peak (never blocks the budget)."""
    now = datetime.now(timezone.utc)
    try:
        starts = [period_starts(now)["week"][1], int(journal.get(KV_CURSOR) or time.time() * 1000)]
        rows = await reconcile.income_since(t, min(starts))
        out = period_pnl(journal, equity, rows, old_day, old_week, now)
        out["peak"] = adjusted_peak(journal, equity, rows, int(now.timestamp() * 1000))
        return out
    except Exception:  # noqa: BLE001
        try:
            peak = max(float(journal.get(KV_PEAK) or 0), equity)
        except Exception:  # noqa: BLE001
            peak = equity
        return {"day_pnl": old_day, "week_pnl": old_week, "peak": peak, "late": {}, "degraded": True}
