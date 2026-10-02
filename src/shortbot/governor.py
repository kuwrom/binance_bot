"""Risk governor (docs/RISK_GOVERNANCE.md): account-level circuit breakers ABOVE the per-trade budget.

A breaker either HALTS new entries (until a time, or until a human review) or scales every size (m_gov).
Open positions are not force-closed here: their stops and plans still apply, and the analyst decides.

- daily cap   = start-of-day equity x daily_loss_limit_pct x f,  f = max(0.5, min(1, vol_target/vol_now) x regime)
                a new trade may only risk what is left under the cap (a stop-out can never breach it)
- weekly cap  = weekly_loss_limit_pct of start-of-week equity -> halt until Monday 00:00 UTC
- drawdown    >= dd_halve_pct -> 0.5x;  >= dd_stop_pct -> halt until a human review
- loss streak >= loss_streak_halve -> 0.5x for the rest of the UTC day, or until 2 wins;
              >= loss_streak_pause -> 24h cool-off after the last loss
- give-back   once the day peaked at >= giveback_start_pct, halt if day P&L falls under giveback_keep_frac of the
              peak (and under +giveback_start_pct once the peak is >= 2x that)
Pure and unit-tested; `risk_budget.gather_inputs` supplies the live numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import RiskLimits

REGIME_MULT = {"favorable": 1.0, "caution": 0.75, "hostile": 0.5, "unknown": 0.5}


@dataclass
class GovInputs:
    equity: float
    peak_equity: float
    day_pnl: float                                   # Trader.account(): equity vs the 00:00 UTC mark, net of transfers
    day_peak_pnl: float = 0.0                        # highest day_pnl seen today
    week_pnl: float = 0.0                            # the same against Monday 00:00 UTC
    results: list[tuple[str, float]] = field(default_factory=list)  # (closed_at iso, net pnl), oldest first
    regime: str = "unknown"
    btc_vol_now: float | None = None
    btc_vol_target: float | None = None
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def next_utc_midnight(now: datetime) -> datetime:
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def next_monday(now: datetime) -> datetime:
    return next_utc_midnight(now + timedelta(days=(6 - now.weekday())))


def _ts(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def loss_streak(results: list[tuple[str, float]], run: int = 3) -> dict[str, Any]:
    """Trailing consecutive losses, plus the last run of >= `run` losses and the wins since it ended."""
    n = 0
    for _, pnl in reversed(results):
        if pnl >= 0:
            break
        n += 1
    run_end, wins_after, i = None, 0, len(results) - 1
    while i >= 0:
        j = i
        while j >= 0 and results[j][1] < 0:
            j -= 1
        if i - j >= run:
            run_end = results[i][0]
            break
        if j == i:
            wins_after += 1
            i -= 1
        else:
            i = j
    return {"current": n, "last_loss_at": results[-1][0] if n else None,
            "run3_ended_at": run_end, "wins_since_run3": wins_after}


def daily_cap(limits: RiskLimits, inp: GovInputs) -> dict[str, float]:
    start_eq = max(inp.equity - inp.day_pnl, 0.0)
    m_vol = min(1.0, inp.btc_vol_target / inp.btc_vol_now) if inp.btc_vol_now and inp.btc_vol_target else 1.0
    f = max(0.5, m_vol * REGIME_MULT.get(inp.regime, 0.5))
    cap = start_eq * limits.daily_loss_limit_pct / 100 * f
    return {"start_equity": round(start_eq, 2), "f": round(f, 3), "cap_usd": round(cap, 2),
            "room_usd": round(max(0.0, cap + min(inp.day_pnl, 0.0)), 2)}


def govern(limits: RiskLimits, inp: GovInputs) -> dict[str, Any]:
    now, reasons = inp.now, []
    halts: list[tuple[datetime | None, str]] = []   # (until, why); None = until a human review
    m = 1.0
    dc = daily_cap(limits, inp)
    start_eq = dc["start_equity"] or inp.equity
    if dc["room_usd"] <= 0:
        halts.append((next_utc_midnight(now), f"daily loss cap hit ({inp.day_pnl:+.2f} vs -{dc['cap_usd']:.2f} USDT, "
                                              f"f={dc['f']})"))
    week_start_eq = max(inp.equity - inp.week_pnl, 0.0)
    if week_start_eq and inp.week_pnl <= -week_start_eq * limits.weekly_loss_limit_pct / 100:
        halts.append((next_monday(now), f"weekly loss {inp.week_pnl:+.2f} USDT >= {limits.weekly_loss_limit_pct}%: "
                                        "halt and review the week"))
    dd = (inp.peak_equity - inp.equity) / inp.peak_equity * 100 if inp.peak_equity > 0 else 0.0
    if dd >= limits.dd_stop_pct:
        halts.append((None, f"drawdown {dd:.1f}% >= {limits.dd_stop_pct}%: stop and review before any new trade"))
    elif dd >= limits.dd_halve_pct:
        m *= 0.5
        reasons.append(f"drawdown {dd:.1f}% >= {limits.dd_halve_pct}%: half size")
    st = loss_streak(inp.results, limits.loss_streak_halve)
    if st["current"] >= limits.loss_streak_pause and st["last_loss_at"]:
        until = _ts(st["last_loss_at"]) + timedelta(hours=24)
        if until > now:
            halts.append((until, f"{st['current']} losses in a row: 24h cool-off"))
    if st["run3_ended_at"] and st["wins_since_run3"] < 2 and _ts(st["run3_ended_at"]).date() == now.date():
        m *= 0.5
        reasons.append(f"loss streak >= {limits.loss_streak_halve} today: half size until 2 wins or 00:00 UTC")
    peak_pct = inp.day_peak_pnl / start_eq * 100 if start_eq else 0.0
    if peak_pct >= limits.giveback_start_pct:
        floor_pct = peak_pct * limits.giveback_keep_frac
        if peak_pct >= 2 * limits.giveback_start_pct:
            floor_pct = max(floor_pct, limits.giveback_start_pct)
        day_pct = inp.day_pnl / start_eq * 100
        if day_pct < floor_pct:
            halts.append((next_utc_midnight(now), f"give-back stop: day peaked +{peak_pct:.2f}%, now {day_pct:+.2f}% "
                                                  f"(< {floor_pct:.2f}% floor). Bank the day"))
    reasons += [w for _, w in halts]
    timed = [u for u, _ in halts if u is not None]
    return {"halt": bool(halts), "until": None if any(u is None for u, _ in halts) or not timed
            else max(timed).isoformat(timespec="minutes"), "m_gov": m, "drawdown_pct": round(dd, 2),
            "daily": dc, "day_peak_pct": round(peak_pct, 2), "streak": st, "reasons": reasons}
