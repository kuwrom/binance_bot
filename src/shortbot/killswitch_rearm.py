"""A human clear that sticks (task #63). Without memory, the loss trips (daily loss, drawdown; killswitch.py) re-fire on
the very loss the human just reviewed, within a minute of `shortbot-admin clear`. So:
- every guardian evaluation stores its reading (`kill_switch_obs:<condition>`: loss %, limit, time);
- the clear stores a HOLD (`kill_switch_clearance`) in the same transaction as the state swap: for each condition at or
  over its limit with a reading at most OBS_FRESH_S old, that reading is the baseline the human accepted;
- that condition then trips only once the loss is `margin_pp` points worse than the baseline. The hold lapses when its
  period ends (daily loss: the UTC day; drawdown: the ISO week, a weekly review) or once the loss recovers below
  limit - margin, so a NEW loss trips at the plain limit again.
Fail closed: no fresh reading, a malformed hold or any error means nothing is held. Every clear replaces the hold.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .equity_marks import period_starts
from .journal import Journal

DAILY, DRAWDOWN = "daily_loss", "drawdown"
LAPSE = {DAILY: "day", DRAWDOWN: "week"}   # the equity_marks period whose end lapses a hold
# account-level: read and written through journal.mode_key, like trading_state itself (killswitch.state_key)
KV_OBS = "kill_switch_obs:"                # + condition: the guardian's latest reading
KV_HOLD = "kill_switch_clearance"
REARM_MARGIN_PP = 1.0
MAX_MARGIN_PP = 5.0
OBS_FRESH_S = 900                          # drawdown is read every 300 s, daily loss every 60 s


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _obj(raw: str | None) -> dict[str, Any] | None:
    try:
        v = json.loads(raw or "null")
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


def observe(j: Journal, cond: str, loss_pct: float, limit_pct: float, now: datetime | None = None) -> None:
    """Posted (#79): the guardian's pass never waits for it."""
    now = _now(now)
    j.post(Journal.set, j.mode_key(KV_OBS + cond), json.dumps({"loss_pct": loss_pct, "limit_pct": limit_pct,
                                                               "ts": now.timestamp(),
                                                               "at": now.isoformat(timespec="seconds")}))


def preview(j: Journal, margin_pp: float = REARM_MARGIN_PP, now: datetime | None = None) -> dict[str, Any]:
    """The hold a clear would store now (shown to the human before they type the code, then stored as shown)."""
    if not 0 <= margin_pp <= MAX_MARGIN_PP:
        raise ValueError(f"re-arm margin {margin_pp} pp: must be 0..{MAX_MARGIN_PP} percentage points")
    now = _now(now)
    out: dict[str, Any] = {"margin_pp": margin_pp, "held": {}, "not_held": {}}
    for cond in (DAILY, DRAWDOWN):
        o = _obj(j.get(j.mode_key(KV_OBS + cond))) or {}   # none yet: KeyError below, as None's TypeError was
        try:
            loss, limit, age = float(o["loss_pct"]), float(o["limit_pct"]), now.timestamp() - float(o["ts"])
        except (TypeError, KeyError, ValueError):
            loss, limit, age = 0.0, 0.0, None
        if age is None or age > OBS_FRESH_S:
            out["not_held"][cond] = (f"no guardian reading in the last {OBS_FRESH_S // 60} min: it re-trips at its "
                                     "limit if still over it")
        elif loss >= limit:
            out["held"][cond] = {"baseline_pct": loss, "rearm_at_pct": round(loss + margin_pp, 4), "limit_pct": limit,
                                 "period": period_starts(now)[LAPSE[cond]][0], "observed_at": o.get("at")}
    return out


def describe(p: dict[str, Any]) -> str:
    """The lines the human confirms."""
    name = {DAILY: "daily loss", DRAWDOWN: "drawdown"}
    lines = [f"- {name[c]} {h['baseline_pct']:.2f}% (read {h['observed_at']}): held until {h['rearm_at_pct']:.2f}% or "
             f"the end of {h['period']}, or a recovery under {h['limit_pct'] - p['margin_pp']:.2f}%"
             for c, h in p["held"].items()]
    lines += [f"- {name[c]}: {why}" for c, why in p["not_held"].items()]
    return "\n".join(lines or ["- daily loss and drawdown are under their limits: nothing to hold"])


def encode(p: dict[str, Any] | None, by: str, at: str) -> str:
    return json.dumps({"margin_pp": (p or {}).get("margin_pp", REARM_MARGIN_PP), "held": (p or {}).get("held", {}),
                       "by": by, "at": at})


def holding(j: Journal) -> dict[str, Any] | None:
    """The live hold per condition, for status(); None when nothing is held."""
    held = (_obj(j.get(j.mode_key(KV_HOLD))) or {}).get("held")
    return held if isinstance(held, dict) and held else None


def trip_at(j: Journal, cond: str, loss_pct: float, limit_pct: float, now: datetime | None = None
            ) -> tuple[float, dict[str, Any] | None]:
    """(the loss % at which `cond` trips now, the hold behind it or None). Drops a hold that lapsed."""
    key = j.mode_key(KV_HOLD)
    raw = j.get(key)
    hold = _obj(raw) or {}
    held = hold.get("held")
    h = held.get(cond) if isinstance(held, dict) else None
    if not isinstance(h, dict):
        return limit_pct, None
    try:
        margin, rearm, period = float(hold["margin_pp"]), float(h["rearm_at_pct"]), str(h["period"])
        baseline = float(h["baseline_pct"])
    except (TypeError, KeyError, ValueError):
        return limit_pct, None
    if not 0 <= margin <= MAX_MARGIN_PP or rearm > baseline + MAX_MARGIN_PP + 1e-9:
        return limit_pct, None   # not a hold a clear writes: ignore it (fail closed)
    if period != period_starts(_now(now))[LAPSE[cond]][0]:
        why = f"{period} ended"
    elif loss_pct < limit_pct - margin:
        why = f"recovered to {loss_pct:.2f}% (under {limit_pct - margin:.2f}%)"
    else:
        return max(limit_pct, rearm), {**h, "margin_pp": margin, "baseline_pct": baseline, "cleared_by": hold.get("by"),
                                       "cleared_at": hold.get("at")}
    rest = {k: v for k, v in hold["held"].items() if k != cond}
    j.post(_drop, key, raw, json.dumps({**hold, "held": rest}), f"{cond} hold from the clear at {hold.get('at')} "
           f"lapsed: {why}", {"condition": cond, "hold": h, "loss_pct": loss_pct, "limit_pct": limit_pct})
    return limit_pct, None


def _drop(j: Journal, key: str, raw: str, new: str, why: str, snapshot: dict[str, Any]) -> None:
    """A lapsed hold goes (posted: the pass that saw it lapse never waits); a clear that landed meanwhile wins."""
    if j.db.execute("UPDATE kv SET v=? WHERE k=? AND v=?", (new, key, raw)).rowcount:
        j.log_decision("kill_switch_rearmed", why, None, snapshot=snapshot)
