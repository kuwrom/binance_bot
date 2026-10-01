"""Live ladder (experiment ladder_live_v1, docs/LADDER.md): turn the shadow ladder's decision for ONE position into
actions through the existing guarded tools. Run by the analyst at a review (`shortbot ladder SYM --apply`); the
guardian never runs it (no autopilot).

- exposure x = contracts / full_size; neutral score = 1.0 (the normal entry), a sweet score scales up to
  max_add_r_mult (e.g. 2 = double), a sour one down to 0 (ladder.live_exposure_target)
- cut: the base variant's hysteresis says reduce -> partial close down to x_virtual x full_size
- add: the base variant says add -> add_to_winner (winners only, stop tightened first, total loss at stop <=
  max_add_r_mult x the pinned 1R, and any risk above 1R must fit the governor's daily room);
  if the stop the budget needs would sit too close to price, the add shrinks (halves twice) or is skipped
- stop: the adaptive variant's event re-level (confirmed lower 1h swing high + 0.5 ATR), tighten-only
Every step is logged as a `ladder_apply` decision with the ladder snapshot, so live-managed trades can be compared
with the ghost of their original plan."""

from __future__ import annotations

from typing import Any

from . import killswitch
from .exchange import to_symbol
from .ladder import build_ladder
from .positioning import PositionOps, _r_initial

TAG = "v1"
MIN_TRIM = 0.1        # ignore cuts smaller than 10% of the position (fees for nothing)


def plan_add(entry: float, cur: float, mark: float, stop: float, add_qty: float, r1: float,
             min_stop_pct: float) -> tuple[float, float] | None:
    """(qty, stop) for an add that keeps the whole position's loss at the stop <= r1, halving the add up to twice
    when the required stop would be closer than min_stop_pct above price. None if nothing fits."""
    for _ in range(3):
        if add_qty <= 0:
            return None
        avg = (entry * cur + mark * add_qty) / (cur + add_qty)
        max_stop = avg + r1 / (cur + add_qty)
        new_stop = min(stop, max_stop)
        if new_stop >= mark * (1 + min_stop_pct / 100):
            return add_qty, new_stop
        add_qty /= 2
    return None


def _pending_legs(trader) -> list[dict[str, Any]]:
    try:
        return trader.j._rows("SELECT symbol FROM entries WHERE status='pending' AND tranche IS NOT NULL")
    except Exception:  # noqa: BLE001 - no entries table yet
        return []


async def enable(trader, symbol: str) -> dict[str, Any]:
    """Opt a trade into the live ladder: pins 1R and the exposure base before anything changes size."""
    sym = to_symbol(symbol)
    if blocked := await killswitch.gate(trader, "ladder enable"):   # opting in arms future adds
        return blocked
    trade = trader.j.active_trade(sym)
    if not trade:
        return {"ok": False, "error": "no journal-tracked trade"}
    plan = dict(trade.get("plan") or {})
    plan.setdefault("r1_usdt", _r_initial(trade))
    plan.setdefault("full_size", trade.get("amount"))
    plan["ladder_live"] = TAG
    trader.j.update_trade(trade["id"], plan=plan)
    trader.j.log_decision("ladder_enable", f"live ladder {TAG} enabled", sym, snapshot={
        "trade_id": trade["id"], "r1_usdt": plan["r1_usdt"], "full_size": plan["full_size"]})
    return {"ok": True, "trade_id": trade["id"], "r1_usdt": plan["r1_usdt"], "full_size": plan["full_size"]}


async def apply(trader, symbol: str, dry_run: bool = False) -> dict[str, Any]:
    sym = to_symbol(symbol)
    pos = next((p for p in await trader.positions() if p["symbol"] == sym), None)
    trade = trader.j.active_trade(sym)
    if not pos or not trade:
        return {"ok": False, "error": "no journal-tracked position"}
    plan = trade.get("plan") or {}
    if plan.get("ladder_live") != TAG and not dry_run:   # a dry run may preview any trade
        return {"ok": False, "error": f"trade #{trade['id']} is not in ladder_live_{TAG}; run `shortbot ladder "
                                      f"{symbol} --enable` first (a deliberate choice, logged)"}
    lad = await build_ladder(trader.ex, trader.j, pos, log=not dry_run)
    base, adaptive = lad["variants"]["base"], lad["variants"]["adaptive"]
    full, cur, mark = plan.get("full_size") or trade["amount"], pos["contracts"], pos["mark"]
    stop = pos["exchange_stop"]
    acts: list[dict[str, Any]] = []
    if adaptive.get("relevel") and stop and adaptive["stop_v"] < stop:
        acts.append({"kind": "move_stop", "price": adaptive["stop_v"], "why": adaptive["relevel"]})
        stop = adaptive["stop_v"]
    if base["action"] == "reduce":
        frac = 1 - base["x_virtual"] * full / cur if cur else 0
        if frac >= MIN_TRIM:
            acts.append({"kind": "close", "fraction": round(min(1.0, frac), 3),
                         "why": f"score {base['score']:+g}: exposure -> {base['x_virtual']:.0%} of full size"})
    elif base["action"] == "add" and any(e["symbol"] == sym for e in _pending_legs(trader)):
        acts.append({"kind": "skip_add", "why": "a split-entry leg is still resting: that IS the planned add"})
    elif base["action"] == "add" and (blocked := await killswitch.gate(trader, "ladder add")):
        acts.append({"kind": "skip_add", "why": blocked["error"]})   # cuts and stop re-levels still run
    elif base["action"] == "add":
        r_cap = _r_initial(trade) * max(1.0, trader.ex.limits.max_add_r_mult)
        fit = plan_add(pos["entry"], cur, mark, stop, base["x_virtual"] * full - cur, r_cap,
                       trader.ex.limits.min_stop_distance_pct)
        if fit:
            acts.append({"kind": "add", "notional": round(fit[0] * mark, 2), "stop": fit[1],
                         "why": f"score {base['score']:+g}: exposure -> {base['x_virtual']:.0%}; loss at stop <= 1R"})
        else:
            acts.append({"kind": "skip_add", "why": "no add fits the 1R budget with a sane stop distance"})
    results = []
    if not dry_run:
        for a in acts:
            reason = f"ladder_live_{TAG}: {a['why']}"
            if a["kind"] == "move_stop":
                results.append(await trader.move_stop(sym, a["price"], reason))
            elif a["kind"] == "close":
                results.append(await trader.close(sym, reason, "", a["fraction"]))
            elif a["kind"] == "add":
                results.append(await PositionOps(trader).add_to_winner(sym, a["notional"], a["stop"], reason))
        trader.j.log_decision("ladder_apply", f"{base['action']} (score {base['score']:+g})", sym, snapshot={
            "trade_id": trade["id"], "actions": acts, "results": results, "x_actual": lad["x_actual"],
            "x_star": lad["x_star"], "levels": lad["levels"], "parts": lad["parts"]})
    return {"ok": True, "dry_run": dry_run, "trade_id": trade["id"], "score": lad["score"], "parts": lad["parts"],
            "x_actual": lad["x_actual"], "x_star": lad["x_star"], "decision": base["action"], "actions": acts,
            "results": results, "levels": lad["levels"]}
