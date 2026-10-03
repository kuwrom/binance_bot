"""Day / week P&L and an equity peak that survive midnight carry-overs and transfers (task #16).

The old definition was realized income since 00:00 UTC + ALL unrealized P&L, so a loss carried in
an open position trips the daily cap at midnight with no new loss, and deposits/withdrawals move `equity_peak` (a 12%
withdrawal looks like a 12% drawdown). Here:
- marks: the first equity seen in each UTC day and ISO week is stored in kv (`eq_mark:day:<date>`,
  `eq_mark:week:<year-Www>`); period P&L = equity now - mark - net TRANSFER income since the mark.
- a LATE mark (first seen > 15 min after the period began, e.g. the guardian slept across midnight) cannot see the
  loss before it, so the period P&L is the worse of the new and the old definition: the caps never miss a loss.
- peak: `equity_peak` moves with transfers (a deposit raises it, a withdrawal lowers it), so only trading P&L is
  drawdown; `reset_peak` is the human reset (shortbot-admin).
Every key here belongs to one account: read and written through `journal.mode_key` (demo keeps the bare key, live
gets '@live'), so a switch to live never starts from demo's peak or marks (#32).
Values are computed from what is stored and returned at once; what they store is posted (#79, journal_writer.py), so
the guardian's pass never waits for another process's write lock. Merged (Journal.merge, #82): each landing unit merges
with what is stored then (the first mark wins; the peak is recomputed), and until it lands this process's reads merge
its own pending value the same way, so a hold never makes drawdown or the period's loss read smaller.
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


def _mark(journal, kind: str, key: str, equity: float, now_ms: int, unrealized: float | None = None) -> dict[str, Any]:
    k = journal.mode_key(f"eq_mark:{kind}:{key}")
    raw = journal.get(k) or journal.pending(k)   # the first stored mark, else this process's own, still queued
    if raw:
        return json.loads(raw)
    m = {"equity": equity, "ts": now_ms, **({"unrealized": unrealized} if unrealized is not None else {})}
    journal.merge(k, json.dumps(m), _first_mark, k, json.dumps(m))
    return m


def _first_mark(journal, k: str, v: str) -> None:
    """The period's first mark stays: another process's, if it got there first."""
    journal.db.execute("INSERT OR IGNORE INTO kv (k, v) VALUES (?, ?)", (k, v))


def trading_income(rows: list[dict[str, Any]], since_ms: int) -> float:
    """Realized P&L + fees + funding + liquidation fees since since_ms (reconcile.PNL_TYPES; never transfers)."""
    return sum(float(r.get("income") or 0) for r in rows
               if r.get("incomeType") in reconcile.PNL_TYPES and int(r.get("time") or 0) >= since_ms)


def transfers(rows: list[dict[str, Any]], since_ms: int, until_ms: int | None = None) -> float:
    """Net TRANSFER income with since_ms <= time < until_ms (open-ended without until_ms)."""
    return sum(float(r.get("income") or 0) for r in rows
               if r.get("incomeType") == "TRANSFER" and int(r.get("time") or 0) >= since_ms
               and (until_ms is None or int(r.get("time") or 0) < until_ms))


PENDING_MS = 120_000     # how long an unexplained equity move is treated as income not yet reported


def period_pnl(journal, equity: float, transfer_rows: list[dict[str, Any]], old_day: float, old_week: float,
               now: datetime | None = None, unrealized: float | None = None) -> dict[str, Any]:
    """{'day_pnl', 'week_pnl', 'late': {...}, 'pending': {...}, 'degraded': bool} from the marks.

    transfer_rows are the raw /fapi/v1/income rows since (at least) the week mark. With `unrealized` (and a mark
    that stored it) the equity view (A = equity - mark - transfers) is cross-checked against the income view
    (B = trading income since the mark + change in unrealized). They agree unless an income row lags the balance:
    a withdrawal not yet reported would read as a loss in A, a deposit as a gain. A disagreement younger than
    PENDING_MS uses B (pending); one that persists uses min(A, B) (conservative for the caps). Income rows in the
    mark's own second count on whichever side of it B agrees with A. Never raises: any failure returns the old
    definitions with degraded=True (the kill switch calls this inside guardian hooks)."""
    try:
        now = now or datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        out: dict[str, Any] = {"late": {}, "pending": {}, "degraded": False}
        for kind, old in (("day", old_day), ("week", old_week)):
            key, start_ms = period_starts(now)[kind]
            m = _mark(journal, kind, key, equity, now_ms, unrealized)
            ts = int(m["ts"])
            pnl = equity - float(m["equity"]) - transfers(transfer_rows, ts)
            out["pending"][kind] = False
            if unrealized is not None and m.get("unrealized") is not None:
                du = unrealized - float(m["unrealized"])
                # income rows are stamped to the second (probe_result 395): one in the mark's own second may be from
                # before the mark (the guardian closes, then marks) or after it. B counts them on the side that agrees
                b = min((trading_income(transfer_rows, t0) + du for t0 in (ts, ts // 1000 * 1000)),
                        key=lambda x: abs(pnl - x))
                pk = journal.mode_key(f"eq_pending:{kind}")
                if abs(pnl - b) > max(1.0, 0.001 * abs(equity)):
                    first = int(journal.get(pk) or 0) or now_ms
                    if first == now_ms:
                        journal.mark(pk, str(now_ms))   # its age must grow during a held lock too (#81)
                    young = now_ms - first < PENDING_MS
                    out["pending"][kind] = young
                    pnl = b if young else min(pnl, b)
                elif journal.get(pk) not in (None, "0"):
                    journal.mark(pk, "0")
            late = ts - start_ms > LATE_MARK_S * 1000
            out["late"][kind] = late
            out[f"{kind}_pnl"] = round(min(pnl, old) if late else pnl, 4)
        return out
    except Exception:  # noqa: BLE001
        return {"day_pnl": old_day, "week_pnl": old_week, "late": {}, "pending": {}, "degraded": True}


CURSOR_LAG_MS = 60_000   # income can be reported a few seconds late: count transfers only up to now - lag


def adjusted_peak(journal, equity: float, transfer_rows: list[dict[str, Any]], now_ms: int | None = None) -> float:
    """equity_peak shifted by transfers, never below current equity. Several processes call this (guardian lanes,
    MCP, CLI), so the stored peak and cursor move in one unit that recomputes from what it reads, and the cursor only
    moves forward: each transfer in [cursor, now - lag) is counted exactly once by whichever caller advances the
    cursor over it. Merged: the peak returned is computed from what is stored now and from this process's own
    (peak, cursor) still queued, each moved by the transfers since its cursor (a withdrawal lowers both); the unit
    recomputes from what is stored when it lands."""
    now_ms = now_ms or int(time.time() * 1000)
    k = journal.mode_key(KV_PEAK)
    peak, cursor = _peak(journal, equity, transfer_rows, now_ms, journal.pending(k))
    journal.merge(k, json.dumps([peak, cursor]), _advance_peak, equity, transfer_rows, now_ms)
    return peak


def _peak(journal, equity: float, transfer_rows: list[dict[str, Any]], now_ms: int,
          pending: str | None = None) -> tuple[float, int]:
    """(peak, cursor) from what is stored, and from `pending` ([peak, cursor] json) when given."""
    upto = now_ms - CURSOR_LAG_MS
    raw = journal.get(journal.mode_key(KV_CURSOR))
    views = [(float(journal.get(journal.mode_key(KV_PEAK)) or 0.0), int(raw) if raw else upto)]
    if pending:
        views.append(tuple(json.loads(pending)))
    peak = max(p + (transfers(transfer_rows, c, upto) if upto > c else 0.0) for p, c in views)
    cursor = max(max(int(c), upto) for _, c in views)
    # transfers inside the lag window are already in equity but not yet in the peak: leave them out of the
    # high-water mark, or the next pass adds them a second time (phantom drawdown, #16 review)
    return max(peak, equity - transfers(transfer_rows, cursor)), cursor


def _advance_peak(journal, equity: float, transfer_rows: list[dict[str, Any]], now_ms: int) -> None:
    peak, cursor = _peak(journal, equity, transfer_rows, now_ms)
    journal.set(journal.mode_key(KV_PEAK), str(peak))
    journal.set(journal.mode_key(KV_CURSOR), str(cursor))


def reset_peak(journal, equity: float, reason: str) -> dict[str, Any]:
    """Human reset (shortbot-admin): the drawdown is measured from today's equity from now on."""
    old = journal.get(journal.mode_key(KV_PEAK))
    journal.set(journal.mode_key(KV_PEAK), str(equity))
    journal.set(journal.mode_key(KV_CURSOR), str(int(time.time() * 1000)))
    journal.log_decision("equity_peak_reset", f"{old} -> {equity}: {reason}", None,
                         snapshot={"old": old, "new": equity, "reason": reason})
    return {"ok": True, "old_peak": float(old) if old else None, "new_peak": equity}
