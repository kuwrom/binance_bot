"""The kill-switch drill (#20, #46): `shortbot drill kill-switch [--symbol S] [--second S2]`, then, once the owner has
cleared the switch, `shortbot drill kill-switch --verify`.

It needs a flat desk (drill.refusal). Steps, each the real command's intent (drill.py):
1. `open` a drill short on S: exactly one closePosition stop rests; `propose` a far limit entry on S2: it rests.
2. `kill-switch REDUCING`: an `open` is refused (its intent `refused`, nothing sent, the position unchanged), the entry
   still rests, a `move-stop` tightening runs (one closePosition stop, at the new price), a `widen-stop` is refused.
3. `kill-switch HALTED`: the entry is withdrawn on the exchange, the protective stop stays.
4. `flatten`: flat on the exchange, the drill trade closed with exchange-truth P&L and the drill's lesson, still HALTED.
It stops there: clearing the switch is the owner's alone (`shortbot-admin clear`, from a real terminal), so the run's
drill_result is `awaiting_clear` and the answer prints the owner's command. --verify then checks the switch is ACTIVE
again by a shortbot-admin clear journalled after the run, and journals the `pass` (or `fail`). A step the drill can't
go on without stops it: its entry is cancelled and its short closed (both allowed in any state), the switch stays where
the drill left it, and the result is `fail`."""

from __future__ import annotations

import json
from typing import Any

from . import audit, drill_log
from . import drill as D
from . import killswitch as ks
from .drill_log import OVERRIDE, check
from .entry_intents import ON_BOOK
from .exchange import to_raw

NAME = "kill-switch"
WINDOW_S = 1800.0
ON_BOOK_STATUSES = ("pending", "placing", "unknown", "external")
assert all(s in ON_BOOK for s in ON_BOOK_STATUSES)
LESSON = (f"{OVERRIDE} (kill-switch): REDUCING refused the open and the widening and kept the entry, a tightening ran; "
          "HALTED withdrew the entry and kept the stop; flatten closed at market. A drill, not a market read: no "
          "evidence about the coin")
ABORT = f"{OVERRIDE} (kill-switch) stopped early: closed by the drill's cleanup. No evidence about the coin"
CLEAR = ".venv/bin/shortbot-admin clear --reason 'kill-switch drill #46 done: REDUCING, HALTED and flatten as designed'"
VERIFY = ".venv/bin/shortbot drill kill-switch --verify"


async def run(app: Any, sym: str, sym2: str) -> dict[str, Any]:
    tr = app.trader
    head = {"drill": NAME, "symbols": [sym, sym2]}
    if why := await D.refusal(tr, [sym, sym2], NAME, flat=True):
        return {"ok": False, "refused": True, **head, "error": why}
    d, ctx = D.Drill(app, NAME), dict[str, Any]()
    async with d.window(WINDOW_S):
        try:
            await _steps(d, sym, sym2, ctx)
        except Exception as e:  # noqa: BLE001 - a DrillStop or a bug: recorded, then the cleanup
            d.check("completed", False, f"{type(e).__name__}: {e}"[:500])
        if not all(c["ok"] for c in d.checks.values()):
            await d.cleanup([sym, sym2], ctx.get("group"), ABORT)
        left = await d.left([sym, sym2])
    d.check("nothing_left", not any(v["position"] or v["orders"] for v in left.values()), left)
    passed = all(c["ok"] for c in d.checks.values())
    state = d.state()
    res = d.finish("awaiting_clear" if passed else "fail", **head, trade_id=ctx.get("trade_id"), state=state,
                   vetoes_overridden=ctx.get("vetoes_overridden"))
    nxt = ([f"the owner clears the switch from a real terminal: {CLEAR}", f"then: {VERIFY}"] if passed else
           ["see the failed checks; the switch is " + state + (f": the owner clears it ({CLEAR})"
                                                                if state != ks.ACTIVE else "")])
    return {**res, "next": nxt}


async def _steps(d: D.Drill, sym: str, sym2: str, ctx: dict[str, Any]) -> None:
    tr = d.tr
    res, price = await d.open_short(sym)
    if not d.check("opened", res.get("ok") is True and isinstance(res.get("trade_id"), int), D.brief(res)):
        raise D.DrillStop("the drill short did not open")
    tid = ctx["trade_id"] = res["trade_id"]
    ctx["vetoes_overridden"] = ((d.j.get_trade(tid) or {}).get("entry_features") or {}).get("vetoes_overridden")
    d.check("one_close_stop", len(await tr.stop_orders(sym, True)) == 1, await tr.stop_orders(sym, True))
    res = await d.rest_entry(sym2, 1.0)
    placed = res.get("placed") or []
    eid = placed[0]["entry_id"] if len(placed) == 1 else None
    ctx["group"] = res.get("group_id")
    oid = d.entry_row(eid).get("order_id")
    if not d.check("entry_rests", bool(oid) and await d.order_open(sym2, oid), D.brief(res)):
        raise D.DrillStop("the far entry did not rest")

    # REDUCING: risk refused, reductions allowed, resting entries stay
    res = await d.submit("trip REDUCING", "kill-switch", state=ks.REDUCING, reason=f"{OVERRIDE}: REDUCING phase")
    if not d.check("reducing", d.state() == ks.REDUCING, D.brief(res)):
        raise D.DrillStop("the switch did not go REDUCING")
    size = (await tr.position(sym) or {}).get("contracts")
    res, _ = await d.open_short(sym, "open while REDUCING (must be refused)")
    st = await d.intent_status(d.steps[-1]["intent_id"])
    after = (await tr.position(sym) or {}).get("contracts")
    d.check("reducing_open_refused", res.get("ok") is False and res.get("kill_switch") == ks.REDUCING
            and st["status"] == "refused" and not st["sent"] and after == size,
            {"answer": res.get("error"), "intent": st, "contracts": [size, after]})
    row = d.entry_row(eid)
    d.check("reducing_entry_rests", row.get("status") == "pending" and await d.order_open(sym2, oid),
            {"status": row.get("status")})
    tight = d.px(sym, price, D.STOP_PCT - 2)
    res = await d.submit("tighten the stop while REDUCING", "move-stop", symbol=to_raw(sym), new_stop=tight,
                         reason=f"{OVERRIDE}: a tightening is allowed while REDUCING")
    stops = await tr.stop_orders(sym, True)
    d.check("reducing_tighten_ok", res.get("ok") is True and [s["trigger"] for s in stops] == [tight],
            {"answer": D.brief(res), "stops": stops})
    res = await d.submit("widen the stop while REDUCING (must be refused)", "widen-stop", symbol=to_raw(sym),
                         new_stop=d.px(sym, price, D.STOP_PCT + 2), reason=f"{OVERRIDE}: a widening must be refused")
    st = await d.intent_status(d.steps[-1]["intent_id"])
    stops = await tr.stop_orders(sym, True)
    d.check("reducing_widen_refused", res.get("ok") is False and res.get("kill_switch") == ks.REDUCING
            and not st["sent"] and [s["trigger"] for s in stops] == [tight], {"answer": res.get("error"), "intent": st})

    # HALTED: resting entries withdrawn, protective stops kept
    res = await d.submit("trip HALTED", "kill-switch", state=ks.HALTED, reason=f"{OVERRIDE}: HALTED phase")
    if not d.check("halted", d.state() == ks.HALTED, D.brief(res)):
        raise D.DrillStop("the switch did not go HALTED")
    row = d.entry_row(eid)
    d.check("halted_entry_withdrawn", row.get("status") not in ON_BOOK_STATUSES and not await d.order_open(sym2, oid),
            {"status": row.get("status"), "resting": res.get("resting")})
    d.check("halted_stop_kept", len(await tr.stop_orders(sym, True)) == 1, await tr.stop_orders(sym, True))

    # flatten: flat, the trade closed from the exchange's truth, still HALTED
    res = await d.submit("flatten", "flatten", reason=f"{OVERRIDE}: flatten phase", lesson=LESSON)
    t = await d.closed_trade(tid)
    d.check("flatten_flat", res.get("ok") is True and not await tr.raw_positions(), D.brief(res))
    d.check("flatten_trade_closed", t.get("status") == "closed" and t.get("pnl") is not None
            and t.get("lesson") == LESSON, {k: t.get(k) for k in ("status", "pnl", "fees", "exit_reason")})
    d.check("still_halted", d.state() == ks.HALTED, d.state())
    d.check("completed", True)


async def verify(app: Any) -> dict[str, Any]:
    """After the owner's clear: ACTIVE again, by a shortbot-admin clear journalled after the run (module doc)."""
    j = app.journal
    last = drill_log.latest_results(j).get(NAME)
    if last is None:
        return {"ok": False, "drill": NAME, "error": "no kill-switch drill on record: shortbot drill kill-switch"}
    if last.get("verdict") == "pass":
        return {"ok": True, "drill": NAME, "already_verified": True, "decision_id": last["decision_id"]}
    if last.get("verdict") != "awaiting_clear":
        return {"ok": False, "drill": NAME, "error": f"the last run ended {last.get('verdict')}: run the drill again",
                "failed": [k for k, c in (last.get("checks") or {}).items() if not c.get("ok")]}
    st = ks.read(j)
    if st["state"] != ks.ACTIVE:
        return {"ok": False, "drill": NAME, "state": st["state"],
                "error": f"the switch is {st['state']}: only the owner clears it, from a real terminal: {CLEAR}"}
    clears = []
    for r in j._rows("SELECT id, ts, actor, snapshot FROM decisions WHERE action='kill_switch_clear' AND ts >= ?"
                     " ORDER BY id", (last["ts"],)):
        snap = json.loads(r["snapshot"] or "{}")
        if str(r["actor"] or "").startswith("admin:") and str(snap.get("by") or "").startswith("human:"):
            clears.append({"decision_id": r["id"], "ts": r["ts"], "by": snap.get("by"), "reason": snap.get("reason")})
    checks = {**{f"run:{k}": c for k, c in (last.get("checks") or {}).items()},
              "cleared_active": check(True, {k: st.get(k) for k in ("state", "by", "at")}),
              "admin_clear": check(bool(clears), clears or "no shortbot-admin clear journalled after the run")}
    with audit.acting(f"cli:drill:{NAME}"):
        return drill_log.result(j, NAME, drill_log.verdict(checks), checks, run=last.get("run"),
                                started=last.get("started"), verifies=last.get("decision_id"),
                                symbols=last.get("symbols"), trade_id=last.get("trade_id"), steps=last.get("steps"),
                                intents=last.get("intents"), override=last.get("override"))
