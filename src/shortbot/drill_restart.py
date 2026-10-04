"""The restart drill (#46, GATEWAY.md §8 step 6 on the real desk): the guardian stopped past the stale-heartbeat limit
and started again, with a drill position and a resting entry on the book. Two phases around steps done by hand:

    shortbot drill restart --phase before     # a drill short with 2 resting targets on S, a far entry on S2
    (by hand, BY_HAND; deploy/DEPLOY.md §7a) stop the guardian >= 11 min; meanwhile run the watchdog (`shortbot
    kill-switch`: HALTED, stale heartbeat); start the guardian
    shortbot drill restart --phase after      # waits up to 15 min for the guardian, checks, closes the drill trade

before: drill.refusal (clean symbols, no resting entry anywhere: the stale halt withdraws every one), the drill window
opened for WINDOW_S (the outage's gap is tagged `drill`: uptime.py), the drill short (targets auto: both rest as sb-t
orders, exactly one closePosition stop) and the far entry, then a snapshot in kv drill:restart:state. Its steps and
checks go into the run's drill_result at `after`; a failed `before` cleans up and journals a `fail` at once.
after (demo only; any kill-switch state): waits until the evidence is in (EVIDENCE) or `wait_min` passes. Still
missing while the window is open: it answers what is missing and journals nothing, so it can run again. Then the
checks: the stale-heartbeat HALTED trip and its auto-lift after 2 verified passes, guardian_started, exactly one
closePosition stop, the plan's targets resting, no untracked / NAKED / stop alert since the snapshot, the user stream
connected since the restart, the gap logged and tagged, the entry withdrawn by the halt. Then the drill trade is closed
with its lesson, the window closed, the snapshot dropped, and the drill_result journalled."""

from __future__ import annotations

import json
import time
from typing import Any

from . import drill as D
from . import killswitch as ks
from . import probes, targets, uptime
from .drill_log import OVERRIDE
from .exchange import to_raw
from .journal import Journal, now_iso
from .user_stream import STATS_KEY

NAME = "restart"
WINDOW_S = 2 * 3600.0
STATE_KEY = "drill:restart:state"
POLL_S = 15.0
TARGETS = 2
BAD_ALERTS = ("NAKED_POSITION", "untracked_position", "stop_missing", "stop_restored", "stop_restore_failed",
              "stop_failed", "unexpected_long", "position_flip")
EVIDENCE = ("stale_trip", "auto_clear", "guardian_started", "user_stream", "gap_logged")
LESSON = (f"{OVERRIDE} (restart): the guardian was stopped past the stale limit and started again; the exchange stop "
          "and targets held, the stale halt tripped, withdrew the entry and lifted itself. A drill, not a market read: "
          "no evidence about the coin")
ABORT = f"{OVERRIDE} (restart) stopped early: closed by the drill's cleanup. No evidence about the coin"
BY_HAND = ["on the desk host, in ~/binance_bot: `docker compose stop guardian`, and keep it stopped at least 11 min "
           "(no push meanwhile: a deploy starts it)",
           "after 10 min: `.venv/bin/shortbot kill-switch` (the watchdog: expect HALTED, cause stale_heartbeat)",
           "`docker compose start guardian`",
           "then: `.venv/bin/shortbot drill restart --phase after` (waits up to 15 min; re-run it if it answers "
           "pending)"]


def _state(j: Any) -> dict[str, Any] | None:
    try:
        st = json.loads(j.get(j.mode_key(STATE_KEY)) or "null")
    except ValueError:
        return None
    return st if isinstance(st, dict) else None


def _drop(j: Any) -> None:
    j.db.execute("DELETE FROM kv WHERE k=?", (j.mode_key(STATE_KEY),))
    j.db.commit()


async def _targets(d: D.Drill, sym: str, tid: int) -> int:
    return sum(targets.trade_ref(o.get("clientOrderId")) == tid for o in await d.tr.t.fetch_open_orders(sym))


# ------------------------------------------------------------ before
async def before(app: Any, sym: str, sym2: str) -> dict[str, Any]:
    tr, j = app.trader, app.journal
    head = {"drill": NAME, "phase": "before", "symbols": [sym, sym2]}
    if (st := _state(j)) and (j.get_trade(st.get("trade_id") or 0) or {}).get("status") in ("opening", "open"):
        return {"ok": False, "refused": True, **head, "error": f"a restart drill is under way (trade "
                f"#{st['trade_id']}): run --phase after"}
    if why := await D.refusal(tr, [sym, sym2], NAME, no_entries=True):
        return {"ok": False, "refused": True, **head, "error": why}
    d, group, tid, eid, oid = D.Drill(app, NAME), None, None, None, None
    await j.write(uptime.open_window, NAME, WINDOW_S)
    try:
        res, _ = await d.open_short(sym, targets_auto=True, min_steps=TARGETS)
        if not d.check("opened", res.get("ok") is True and isinstance(res.get("trade_id"), int), D.brief(res)):
            raise D.DrillStop("the drill short did not open")
        tid = res["trade_id"]
        d.check("one_close_stop", len(await tr.stop_orders(sym, True)) == 1, await tr.stop_orders(sym, True))
        d.check("targets_resting", await _targets(d, sym, tid) == TARGETS, await _targets(d, sym, tid))
        res = await d.rest_entry(sym2, 2.0)
        group, placed = res.get("group_id"), res.get("placed") or []
        eid = placed[0]["entry_id"] if len(placed) == 1 else None
        oid = d.entry_row(eid).get("order_id")
        d.check("entry_rests", bool(oid) and await d.order_open(sym2, oid), D.brief(res))
        if not all(c["ok"] for c in d.checks.values()):
            raise D.DrillStop("before: " + ", ".join(k for k, c in d.checks.items() if not c["ok"]))
    except Exception as e:  # noqa: BLE001 - a DrillStop or a bug: recorded, cleaned up, journalled
        d.check("completed", False, f"{type(e).__name__}: {e}"[:500])
        await d.cleanup([sym, sym2], group, ABORT)
        await j.write(uptime.close_window, NAME)
        return {**d.finish("fail", **head, trade_id=tid), "left": await d.left([sym, sym2])}
    snap = {"run": d.run, "started": d.started, "at": time.time(), "at_iso": now_iso(), "trade_id": tid,
            "symbol": sym, "second": sym2, "group_id": group, "entry_id": eid, "order_id": oid,
            "heartbeat": j.get(ks.heartbeat_key(j)), "checks": d.checks, "steps": d.steps}
    await j.write(Journal.set, j.mode_key(STATE_KEY), json.dumps(snap, default=str))
    d.note("before: snapshot", {k: v for k, v in snap.items() if k not in ("checks", "steps")}, sym)
    return {"ok": True, **head, "run": d.run, "trade_id": tid, "entry_id": eid, "checks": d.checks,
            "override": f"the drill trades carry veto_override={OVERRIDE!r}", "next": BY_HAND}


# ------------------------------------------------------------ after
def _decisions(j: Any, action: str, since: str) -> list[dict[str, Any]]:
    rows = j._rows("SELECT id, ts, snapshot FROM decisions WHERE action=? AND ts >= ? AND mode=? ORDER BY id",
                   (action, since, j.mode))
    return [{**r, "snapshot": json.loads(r["snapshot"] or "{}")} for r in rows]


def evidence(j: Any, st: dict[str, Any]) -> dict[str, Any]:
    """What the after phase waits for: each a truthy detail once seen."""
    since = st["at_iso"]
    trips = [r for r in _decisions(j, "kill_switch", since) if r["snapshot"].get("cause") == ks.STALE]
    lifts = [r for r in _decisions(j, "kill_switch_auto_clear", since) if trips and r["id"] > trips[0]["id"]]
    state = ks.read(j)
    started = j._rows("SELECT id, ts FROM alerts WHERE kind='guardian_started' AND ts >= ? AND mode=? ORDER BY id",
                      (since, j.mode))
    try:
        us = json.loads(j.get(j.mode_key(STATS_KEY)) or "{}")
    except ValueError:
        us = {}
    stream = (str(us.get("since") or "") >= since and int(us.get("connects") or 0) >= 1 and us.get("up") is True
              and bool(us.get("last_connect_at")))
    return {"stale_trip": [r["ts"] for r in trips[:1]],
            "auto_clear": [r["ts"] for r in lifts[:1]] if state.get("cause") != ks.STALE else [],
            "guardian_started": [r["ts"] for r in started[:1]],
            "user_stream": {k: us.get(k) for k in ("since", "connects", "up", "last_connect_at")} if stream else None,
            "gap_logged": uptime.gaps(j, since)}


async def after(app: Any, wait_min: float = 15.0) -> dict[str, Any]:
    tr, j = app.trader, app.journal
    head: dict[str, Any] = {"drill": NAME, "phase": "after"}
    if why := probes.demo_refusal(tr, "drill"):
        return {"ok": False, "refused": True, **head, "error": why}
    if (st := _state(j)) is None:
        return {"ok": False, "refused": True, **head, "error": "no restart drill under way: run --phase before first"}
    sym, sym2, tid = st["symbol"], st["second"], st["trade_id"]
    d = D.Drill(app, NAME, run=st["run"], started=st["started"])
    d.steps, d.checks = list(st.get("steps") or []), {f"before:{k}": c for k, c in (st.get("checks") or {}).items()}
    end = time.monotonic() + wait_min * 60
    while True:
        ev = evidence(j, st)
        missing = [k for k, v in ev.items() if not v]
        if not missing or time.monotonic() >= end:
            break
        await D._sleep(POLL_S)
    if missing and time.time() - st["at"] < WINDOW_S:
        d.note("after: still waiting", {"missing": missing}, sym)
        return {"ok": False, "pending": missing, **head, "trade_id": tid,
                "hint": "not all evidence is in (the guardian back, its stale halt lifted, its stream up, the gap "
                        "logged): re-run --phase after. The drill trade stays open meanwhile"}
    for k in EVIDENCE:
        d.check(k, bool(ev[k]), ev[k])
    gaps = [g for g in ev["gap_logged"] if g["tag"] == uptime.TAG_DRILL and g["secs"] >= ks.STALE_HEARTBEAT_S]
    d.check("gap_tagged", bool(gaps), gaps or ev["gap_logged"])
    d.check("one_close_stop", len(await tr.stop_orders(sym, True)) == 1, await tr.stop_orders(sym, True))
    d.check("targets_resting", await _targets(d, sym, tid) == TARGETS, await _targets(d, sym, tid))
    bad = j._rows(f"SELECT id, ts, kind, symbol FROM alerts WHERE ts >= ? AND mode=? AND kind IN "
                  f"({','.join('?' * len(BAD_ALERTS))}) ORDER BY id", (st["at_iso"], j.mode, *BAD_ALERTS))
    d.check("no_bad_alerts", not bad, bad)
    row = d.entry_row(st.get("entry_id"))
    d.check("entry_withdrawn", row.get("status") not in ("pending", "placing", "unknown", "external")
            and not await d.order_open(sym2, st.get("order_id")), {"status": row.get("status")})
    age = ks.heartbeat_age(j)
    d.check("heartbeat_verified", age is not None and age <= ks.VERIFIED_S, age)
    res = await d.submit("close the drill short", "close", symbol=to_raw(sym), reason=f"{OVERRIDE}: restart drill done",
                         lesson=LESSON)
    t = await d.closed_trade(tid)
    d.check("closed", res.get("ok") is True and t.get("status") == "closed" and t.get("pnl") is not None,
            {k: t.get(k) for k in ("status", "pnl", "fees", "exit_reason")})
    await d.cleanup([sym, sym2], st.get("group_id"), ABORT)   # anything still resting or open: nothing, normally
    left = await d.left([sym, sym2])
    d.check("nothing_left", not any(v["position"] or v["orders"] for v in left.values()), left)
    await j.write(uptime.close_window, NAME)
    await j.write(_drop)
    return d.finish(**head, symbols=[sym, sym2], trade_id=tid, waited_s=round(wait_min * 60 - max(0.0, end -
                                                                                                    time.monotonic())))

