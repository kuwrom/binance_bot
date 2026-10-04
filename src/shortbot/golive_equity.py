"""The go-live gate's equity drawdown (golive.py's `equity`, #87): the account's worst drawdown in the window, net of
transfers and demo balance resets, from what the journal keeps. Read-only, no exchange call.

The series (this mode's kv, journal.mode_key: demo keeps the bare key):
- `eq_mark:day:<date>` (equity_marks.py, #16): the first equity the guardian saw each UTC day, its ms (`ts`) and the
  unrealized P&L in it (stored since 2026-10-02: every mark of the window has it);
- `day_low:<date>` / `day_peak:<date>` (risk_budget.track_day_low / track_day_peak, each guardian positions pass,
  about once a minute): the lowest and highest day P&L seen that day. Day P&L is equity vs the day's mark net of
  TRANSFER income (equity_marks.period_pnl), so mark + low and mark + peak are the day's intraday low and high.
Nothing finer is kept: `equity_peak` and `kill_switch_obs:*` (#63) hold only their latest value, a kill-switch trip's
evidence only the moment it tripped. day_low is recorded from #87 on; a day without it has its mark (and high) only.
Within a day the points are the mark, the low, then the high. Which of the low and the high came first is unknown, so
the drawdown into the low is measured from the peak before the day (the low came after the mark) and none is invented
from the day's high down to its low; the high certainly came before the next day's mark.

Transfers and demo balance resets. The journal keeps no income rows, so a jump is what the P&L booked between two
consecutive marks doesn't explain: each trade of this mode (drills too: they trade the same account) books every exit
fill's gross, (entry price - fill price) x qty, when it filled (fill_refs, #35), and the rest of its journalled net pnl
(fees, funding) at its close; plus the change in unrealized P&L. A residual past equity_marks.tolerance (max(1 USDT,
0.1% of equity)) is a jump: a TRANSFER or another non-trading income type, a demo balance reset (no income row at all),
or P&L the journal doesn't hold (a position the desk didn't open: the incidents criterion's untracked_position). It is
netted as equity_marks.adjusted_peak nets a transfer: the peak moves by it (a deposit raises it, a withdrawal or a
reset down lowers it), so only trading P&L is drawdown. Each jump is listed, and that day's intraday range is left
out (a reset has no TRANSFER row, so its day P&L reads it as a loss). Two marks of which one lacks unrealized can't be
checked: their move counts as trading (a withdrawal then reads as a loss: conservative)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from . import equity_marks as em


def _num(raw: Any) -> float | None:
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _kv(j: Any, prefix: str, since_day: str) -> dict[str, str]:
    """This mode's kv `<prefix><date>` from since_day on, {date: raw value}."""
    tail, out = j.mode_key(""), dict[str, str]()
    for k, v in j.db.execute("SELECT k, v FROM kv WHERE k >= ? AND k < ?", (prefix + since_day, prefix[:-1] + ";")):
        day = k[len(prefix):]
        if (tail and not day.endswith(tail)) or (not tail and "@" in day):
            continue
        out[day[:len(day) - len(tail)]] = v
    return out


def marks(j: Any, since_day: str) -> list[dict[str, Any]]:
    out = []
    for day, raw in sorted(_kv(j, "eq_mark:day:", since_day).items()):
        try:
            m = json.loads(raw)
            out.append({"day": day, "equity": float(m["equity"]), "ts": int(m.get("ts") or 0),
                        "unrealized": _num(m.get("unrealized"))})
        except (ValueError, KeyError, TypeError):
            continue
    return out


def booked(j: Any, start: str) -> list[tuple[int, float]]:
    """(ms, USDT): this mode's trading P&L as it reached the wallet, for the trades open or closed since `start`."""
    m, args = j._in_mode()
    out: list[tuple[int, float]] = []
    for r in j._rows(f"SELECT entry_price, pnl, closed_at, fill_refs FROM trades WHERE {m} AND (closed_at IS NULL OR "
                     "closed_at >= ?)", (*args, start)):
        try:
            refs = json.loads(r["fill_refs"] or "{}")
        except ValueError:
            refs = {}
        gross = 0.0
        for f in (refs.get("fills") or []) if isinstance(refs, dict) else []:
            if not isinstance(f, dict) or f.get("leg") != "exit":
                continue
            try:
                g, t = (float(r["entry_price"]) - float(f["px"])) * float(f["qty"]), int(f["ms"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append((t, g))
            gross += g
        if r["pnl"] is not None and r["closed_at"]:
            out.append((int(datetime.fromisoformat(r["closed_at"]).timestamp() * 1000), float(r["pnl"]) - gross))
    return out


def _max_dd(points: list[tuple[float, str, str, float]]) -> tuple[float, tuple[str, str] | None]:
    """Worst (peak - x) / peak over (shift, day, kind, equity) points in time order; a shift moves the peak first."""
    peak: float | None = None
    worst, at = 0.0, None
    for shift, day, kind, x in points:
        peak = x if peak is None else max(peak + shift, x)
        if peak > 0 and (dd := (peak - x) / peak * 100) > worst:
            worst, at = dd, (day, kind)
    return round(worst, 2), at


def drawdown(j: Any, start: str) -> dict[str, Any]:
    """The window's worst drawdown in % (max_dd_pct; None without marks), where (at, kind), the netted jumps, and the
    daily marks' own figure (marks_only_pct, what the gate read before #87) for comparison."""
    ms = marks(j, start[:10])
    out: dict[str, Any] = {"max_dd_pct": None, "at": None, "kind": None, "marks": len(ms), "ranges": 0,
                           "ranges_skipped": [], "jumps": [], "unchecked": 0, "marks_only_pct": None}
    if not ms:
        return out
    book, shift = booked(j, start), [0.0] * len(ms)
    for i in range(1, len(ms)):
        a, b = ms[i - 1], ms[i]
        if a["unrealized"] is None or b["unrealized"] is None:
            out["unchecked"] += 1
            continue
        move = b["equity"] - a["equity"]
        explained = sum(x for t, x in book if a["ts"] < t <= b["ts"]) + b["unrealized"] - a["unrealized"]
        if abs(move - explained) > em.tolerance(b["equity"]):
            shift[i] = move - explained
            out["jumps"].append({"from": a["day"], "to": b["day"], "move": round(move, 2),
                                 "explained": round(explained, 2), "jump": round(move - explained, 2)})
    lows, highs = _kv(j, "day_low:", start[:10]), _kv(j, "day_peak:", start[:10])
    skip, points = {x["from"] for x in out["jumps"]}, list[tuple[float, str, str, float]]()
    for i, m in enumerate(ms):
        points.append((shift[i], m["day"], "mark", m["equity"]))
        lo, hi = _num(lows.get(m["day"])), _num(highs.get(m["day"]))
        if lo is None and hi is None:
            continue
        if m["day"] in skip:
            out["ranges_skipped"].append(m["day"])
            continue
        out["ranges"] += 1
        if lo is not None:
            points.append((0.0, m["day"], "intraday low", m["equity"] + min(lo, 0.0)))
        if hi is not None:
            points.append((0.0, m["day"], "intraday high", m["equity"] + max(hi, 0.0)))
    worst, at = _max_dd(points)
    out.update(max_dd_pct=worst, at=at[0] if at else None, kind=at[1] if at else None,
               marks_only_pct=_max_dd([(0.0, m["day"], "mark", m["equity"]) for m in ms])[0])
    return out
