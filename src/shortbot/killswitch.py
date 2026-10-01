"""Kill switch: one persisted desk state that every opening path checks (FIA 2024 automated-trading risk controls;
NautilusTrader's ACTIVE / REDUCING / HALTED trading states).

- ACTIVE    normal trading.
- REDUCING  only risk-reducing actions: closes, partial closes, stop tightening, cancelling entries. No new entries,
            adds, risk-raising amends, stop widening or ladder opt-ins. Resting limit entries stay; a fill is
            protected and journalled like any other, then alerted.
- HALTED    REDUCING + every resting limit entry is cancelled (by whoever trips it, then every cycle by the
            guardian). A pre-armed stop goes only with a limit confirmed unfilled. Protective stops on open
            positions are NEVER touched: for a short book a generic cancel-all would strip them.
Flatten-all is a command, not a state (killswitch_ops.flatten): HALTED + market-close every short reduce-only.

Anyone may tighten the state (the agent via `halt_trading` / `shortbot kill-switch`, guardian auto-trips, the session
watchdog). Loosening it is human-only (`shortbot-admin clear`, from a real terminal), with one exception: a HALTED
caused only by a stale guardian heartbeat (laptop sleep, an outage) clears itself back to what it replaced once the
heartbeat is fresh again; every other cause is sticky. Each change writes a decision row and an alert. The state is
one journal kv row shared by guardian, MCP server and CLI, changed only by compare-and-swap, so two processes
tripping at once can never lower it or lose a cause.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

ACTIVE, REDUCING, HALTED = "ACTIVE", "REDUCING", "HALTED"
LEVEL = {ACTIVE: 0, REDUCING: 1, HALTED: 2}
KV_KEY = "trading_state"
FRESH_KEY = "trading_state_fresh_checks"
HEARTBEAT_KEY = "guardian_heartbeat"   # "protection last worked": written after a good protective tick
STALE_HEARTBEAT_S = 600                # 240 s job timeout + retry stays under it; Mac sleep counts as downtime
SLOW_HEARTBEAT_S = 180                 # brief warning only
FRESH_HEARTBEAT_S = 60
AUTO_CLEAR_CHECKS = 2                  # distinct fresh heartbeats (guardian passes) before a stale halt lifts
STALE = "stale_heartbeat"
ALLOWED = "close, partial close, move_stop (tighten), cancel entries, flatten"
CLEAR_HINT = "only a human clears it: `.venv/bin/shortbot-admin clear --reason '...'` from a real terminal"
_KEEP = ("state", "by", "reason", "at", "cause")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------ state
def _parse(raw: str | None) -> dict[str, Any] | None:
    try:
        st = json.loads(raw) if raw is not None else None
        return st if isinstance(st, dict) and st.get("state") in LEVEL else None
    except ValueError:
        return None


def read(j) -> dict[str, Any]:
    """The current state; no row = ACTIVE. An unreadable row fails closed (REDUCING)."""
    raw = j.get(KV_KEY)
    st = _parse(raw)
    if st:
        return {**st, "level": LEVEL[st["state"]]}
    if raw is None:
        return {"state": ACTIVE, "level": 0, "by": None, "reason": None, "at": None, "cause": None}
    return {"state": REDUCING, "level": 1, "by": "kill switch", "at": None, "cause": "unreadable",
            "reason": f"state row unreadable ({str(raw)[:60]!r}): failing closed"}


def _cas(j, old: str | None, new: dict[str, Any]) -> bool:
    """Write `new` only if the row still holds `old` (one statement: atomic across processes)."""
    if old is None:
        n = j.db.execute("INSERT OR IGNORE INTO kv (k, v) VALUES (?, ?)", (KV_KEY, json.dumps(new))).rowcount
    else:
        n = j.db.execute("UPDATE kv SET v=? WHERE k=? AND v=?", (json.dumps(new), KV_KEY, old)).rowcount
    j.db.commit()
    return n > 0


async def notify(j, settings, kind: str, msg: str, urgent: bool) -> None:
    from .notify import Notifier   # Telegram when configured, always an alerts row
    await Notifier(settings, j).send(kind, msg, None, cooldown_s=0, urgent=urgent)


async def trip(j, settings, state: str, by: str, reason: str, evidence: dict | None = None,
               cause: str = "manual") -> dict[str, Any]:
    """Escalate to `state` (REDUCING or HALTED). Already there or tighter: a silent no-op that keeps the first
    cause, except during a stale-heartbeat halt, where the new cause is remembered so it outlives the auto-clear."""
    if state not in (REDUCING, HALTED):
        raise ValueError(f"trip to REDUCING or HALTED, not {state!r} (loosening is human-only: shortbot-admin)")
    if not (reason or "").strip():
        raise ValueError("a kill-switch trip needs a reason")
    row = {"state": state, "level": LEVEL[state], "by": by, "reason": reason.strip(), "at": _now_iso(),
           "cause": cause}
    for _ in range(5):
        raw = j.get(KV_KEY)
        parsed = _parse(raw)   # an unreadable row is overwritten by any trip
        cur = parsed or {"state": ACTIVE}
        if parsed and LEVEL[cur["state"]] >= LEVEL[state]:
            if cur.get("cause") != STALE or cause == STALE:
                return {"ok": True, "changed": False, **read(j)}
            if _cas(j, raw, {**cur, "since": [*cur.get("since", []), {k: row[k] for k in _KEEP}]}):
                j.log_decision("kill_switch_noted", f"{state} by {by} during a stale-heartbeat halt: {reason}",
                               None, snapshot={**row, "evidence": evidence})
                return {"ok": True, "changed": False, **read(j)}
            continue
        new = {**row, "prev": cur["state"]}
        if cause == STALE:   # what the auto-clear restores
            new["prev_row"] = {k: cur.get(k) for k in _KEEP}
        if _cas(j, raw, new):
            break
    else:
        raise RuntimeError("kill switch state kept changing under us; trip again")
    if cause == STALE:
        j.set(FRESH_KEY, "{}")
    j.log_decision("kill_switch", f"{cur['state']} -> {state} by {by}: {reason}", None,
                   snapshot={**new, "evidence": evidence})
    await notify(j, settings, "kill_switch", f"{state} (was {cur['state']}) by {by}: {reason}. Allowed now: "
                 f"{ALLOWED}. {how_to_clear(new)}", urgent=True)
    return {"ok": True, "changed": True, **new}


async def clear(j, settings, state: str, by: str, reason: str, shown: str | None) -> dict[str, Any]:
    """Loosen (or set) the state. Human-only: called by shortbot-admin after its terminal confirmation, never by an
    MCP tool or a `shortbot` subcommand (the agent's allowlist covers those). `shown` is the raw row the human
    confirmed: if a trip landed meanwhile, nothing changes and they re-run against the new state."""
    if state not in LEVEL or not (reason or "").strip():
        raise ValueError("clear needs a state (ACTIVE/REDUCING/HALTED) and a reason")
    prev = read(j)
    row = {"state": state, "level": LEVEL[state], "by": by, "reason": reason.strip(), "at": _now_iso(),
           "cause": "human", "prev": prev["state"]}
    if not _cas(j, shown, row):
        return {"ok": False, "error": f"the kill-switch state changed while you were confirming (now {prev['state']} "
                                      f"by {prev.get('by')}: {prev.get('reason')}): nothing changed, re-run"}
    j.log_decision("kill_switch_clear", f"{prev['state']} -> {state} by {by}: {reason}", None,
                   snapshot={**row, "cleared": {k: prev.get(k) for k in (*_KEEP, "since")}})
    await notify(j, settings, "kill_switch_clear", f"{prev['state']} -> {state} by {by}: {reason}", urgent=False)
    return {"ok": True, **row}


def _fresh_passes(j, now: float | None) -> int:
    """Consecutive DISTINCT fresh heartbeats seen (each one = a guardian protective pass that worked); a stale or
    missing one resets the count. Re-reading the same heartbeat from another process doesn't count again."""
    hb, age = j.get(HEARTBEAT_KEY), heartbeat_age(j, now)
    try:
        seen = json.loads(j.get(FRESH_KEY) or "{}")
        n, last = (int(seen.get("n", 0)), seen.get("hb")) if isinstance(seen, dict) else (0, None)
    except ValueError:
        n, last = 0, None
    if age is None or age >= FRESH_HEARTBEAT_S:
        n, last = 0, None
    elif hb != last:
        n, last = n + 1, hb
    else:
        return n
    j.set(FRESH_KEY, json.dumps({"n": n, "hb": last}))
    return n


async def auto_clear_stale(j, settings, now: float | None = None) -> dict[str, Any] | None:
    """Lift a stale-heartbeat halt after AUTO_CLEAR_CHECKS distinct fresh heartbeats (guardian passes): back to the row
    it replaced, or to a sticky trip that landed meanwhile (the most severe, latest first). Withdrawn entries stay
    withdrawn."""
    raw = j.get(KV_KEY)
    st = _parse(raw)
    if not st or st.get("cause") != STALE:
        return None
    if _fresh_passes(j, now) < AUTO_CLEAR_CHECKS:
        return None
    age = heartbeat_age(j, now) or 0.0
    back = max(reversed([st.get("prev_row") or {"state": ACTIVE}, *st.get("since", [])]),
               key=lambda r: LEVEL.get(r.get("state"), 0))
    if back["state"] == ACTIVE:
        new = {"state": ACTIVE, "by": "guardian watchdog", "at": _now_iso(), "cause": "auto_clear",
               "reason": f"guardian heartbeat fresh again ({age:.0f} s) after the stale-heartbeat halt"}
    else:
        new = {**back, "restored_at": _now_iso()}
    new = {**new, "level": LEVEL[new["state"]], "prev": HALTED}
    if not _cas(j, raw, new):
        return None
    j.set(FRESH_KEY, "{}")
    j.log_decision("kill_switch_auto_clear", f"HALTED (stale heartbeat) -> {new['state']}", None,
                   snapshot={"restored": new, "cleared": {k: st.get(k) for k in _KEEP}})
    await notify(j, settings, "kill_switch_clear", f"stale-heartbeat halt lifted: guardian is back. Now {new['state']}"
                 + (f" ({new['by']}: {new['reason']})" if new["state"] != ACTIVE else "")
                 + ". Entries it withdrew stay withdrawn: re-propose if still valid.", urgent=False)
    return new


# ------------------------------------------------------------ guardian watchdog
def heartbeat_age(j, now: float | None = None) -> float | None:
    hb = j.get(HEARTBEAT_KEY)
    try:
        return (now or time.time()) - float(hb) if hb is not None else None
    except ValueError:
        return None


async def watch_guardian(tr, now: float | None = None) -> str | None:
    """The guardian can't see its own death, so sessions check its heartbeat wherever they read the state (the
    opening gate, the brief, the status tool). Stale: trip HALTED and withdraw the resting entries now, since no one
    would protect their fills. Never seen: block without tripping (start the guardian first)."""
    age = heartbeat_age(tr.j, now)
    if age is None:
        return "the guardian has never run against this journal: start shortbot-guardian before opening risk"
    if age <= STALE_HEARTBEAT_S:
        await auto_clear_stale(tr.j, tr.ex.settings, now)
        return None
    why = (f"guardian heartbeat is {age / 60:.0f} min old (> {STALE_HEARTBEAT_S / 60:.0f} min): fills, stops "
           "and plans are unguarded")
    from .killswitch_ops import halt
    await halt(tr, HALTED, "session watchdog", why, {"heartbeat_age_s": round(age)}, cause=STALE)
    return why


# ------------------------------------------------------------ the gate
def how_to_clear(st: dict[str, Any]) -> str | None:
    if st["state"] == ACTIVE:
        return None
    return ("it lifts itself once the guardian heartbeat is fresh again" if st.get("cause") == STALE
            else f"{CLEAR_HINT} (not via Claude)")


def state_refusal(j, action: str) -> dict[str, Any] | None:
    """The gate's re-check INSIDE a symbol lock: the persisted state only. No heartbeat watchdog (its halt cancels
    entries on every symbol, and a task holding one symbol lock never takes another)."""
    st = read(j)
    return refusal(st, action) if st["state"] != ACTIVE else None


def refusal(st: dict[str, Any], action: str) -> dict[str, Any]:
    return {"ok": False, "kill_switch": st["state"],
            "error": f"kill switch {st['state']} since {st.get('at')} ({st.get('by')}: {st.get('reason')}): "
                     f"{action} refused. Allowed: {ALLOWED}. {how_to_clear(st)}"}


async def gate(tr, action: str) -> dict[str, Any] | None:
    """None when `action` (anything that opens or adds risk) may proceed; else the refusal the caller returns.
    Called at the top of each opening path and again right before its order is sent, so a trip from another
    process during the slow pre-checks still stops it. Exits and stop tightening never call this."""
    stale = await watch_guardian(tr)
    st = read(tr.j)
    if st["state"] != ACTIVE:
        return refusal(st, action)
    if stale:
        return {"ok": False, "kill_switch": ACTIVE, "error": f"{action} refused: {stale}"}
    return None


# ------------------------------------------------------------ auto-trips (guardian)
def effective_limits(settings, j):
    """(limits, source): the research override when one is active, else the .env baseline. The guardian runs
    without research params, but its kill-switch thresholds follow the experiment (BOUND_* caps it)."""
    from .params import ResearchParams
    p = ResearchParams(settings, j)
    src = f"research override ({p.experiment() or 'untagged'})" if p.active and p.overrides() else ".env baseline"
    return p.effective(), src


async def trip_on_daily_loss(j, settings, equity: float, day_pnl: float) -> dict[str, Any] | None:
    """Day P&L (realized since 00:00 UTC + all unrealized) against START-of-day equity, as governor.daily_cap."""
    lim, src = effective_limits(settings, j)
    start = equity - day_pnl
    day_pct = day_pnl / start * 100 if start > 0 else 0.0
    if not equity or day_pct > -lim.daily_loss_limit_pct:
        return None
    return await trip(j, settings, REDUCING, "guardian", f"daily loss {day_pct:+.2f}% hit the "
                      f"-{lim.daily_loss_limit_pct}% limit ({src})", {"equity": equity, "day_pnl": day_pnl,
                      "limit_pct": lim.daily_loss_limit_pct, "limit_source": src}, cause="daily_loss")


async def trip_on_drawdown(j, settings, drawdown_pct: float | None) -> dict[str, Any] | None:
    """The governor's dd_stop ('halt until a human review') made sticky: the clear is the review."""
    lim, src = effective_limits(settings, j)
    if drawdown_pct is None or drawdown_pct < lim.dd_stop_pct:
        return None
    return await trip(j, settings, REDUCING, "guardian", f"drawdown {drawdown_pct:.1f}% >= dd_stop "
                      f"{lim.dd_stop_pct}% ({src}): review before any new trade",
                      {"drawdown_pct": drawdown_pct, "limit_pct": lim.dd_stop_pct, "limit_source": src},
                      cause="drawdown")


# ------------------------------------------------------------ visibility
def status(j, now: float | None = None) -> dict[str, Any]:
    st = read(j)
    age = heartbeat_age(j, now)
    return {**{k: v for k, v in st.items() if k != "prev_row"},
            "guardian_heartbeat_age_s": None if age is None else round(age),
            "guardian_alive": age is not None and age <= STALE_HEARTBEAT_S,
            "allowed": "everything (risk checks still apply)" if st["state"] == ACTIVE else ALLOWED,
            "clear": how_to_clear(st)}


def brief_line(j, now: float | None = None) -> str:
    s = status(j, now)
    age = s["guardian_heartbeat_age_s"]
    hb = "never seen" if age is None else f"{age} s ago" + (
        " (STALE)" if not s["guardian_alive"] else " (slow: protection not confirmed for 3+ min)"
        if age > SLOW_HEARTBEAT_S else "")
    if s["state"] == ACTIVE:
        return f"### kill switch: ACTIVE; guardian heartbeat {hb}"
    return (f"### ⛔ kill switch {s['state']} since {s['at']}, tripped by {s['by']} ({s.get('cause')}): {s['reason']}\n"
            f"- allowed: {ALLOWED}. No new entries, adds, widening or ladder opt-ins.\n"
            f"- {s['clear']}. Guardian heartbeat {hb}.")
