"""Live position management from the CURRENT state (docs/POSITION_MANAGEMENT.md §b).

`reassess()` is pure: given the trade, live position, fresh candles, funding and the current risk budget, it
re-plans the trade as if entering now and returns ranked, reasoned actions. The analyst decides; the
actions below execute the non-trivial ones safely:

- widen_stop: allowed ONCE per trade, only with a size cut so the $ loss at the new stop ≤ the initial 1R
- add_to_winner: only in profit; the stop is tightened FIRST so total worst case after the add ≤ 1R
(Tightening and partial exits already exist: Trader.move_stop / Trader.close(fraction).)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import clientid, killswitch
from . import indicators as ind
from .exchange import to_symbol


def _r_initial(trade: dict) -> float:
    """The trade's 1R in USDT, fixed at entry. `plan.r1_usdt` pins it once adds change the amount (or a split entry
    sized the budget for more than the first leg), so a bigger position never buys itself a bigger risk budget."""
    pinned = (trade.get("plan") or {}).get("r1_usdt")
    if pinned:
        return float(pinned)
    return max(0.0, ((trade.get("initial_stop") or 0) - (trade.get("entry_price") or 0)) * (trade.get("amount") or 0))


def chandelier_stop(c1h: list[list[float]], n: int = 22, k: float = 3.0) -> float | None:
    """Short-side chandelier exit: lowest low of n bars + k·ATR(n) (LeBeau)."""
    if len(c1h) <= n:
        return None
    atr = ind.atr(c1h, n)
    return min(c[3] for c in c1h[-n:]) + k * atr if atr else None


def reassess(trade: dict, pos: dict, c1h: list[list[float]], funding_8h: float | None,
             budget_usd: float, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    plan = trade.get("plan") or {}
    entry, mark, qty = trade["entry_price"], pos["mark"], pos["contracts"]
    stop = pos.get("exchange_stop")
    r1 = _r_initial(trade) or 1e-9
    atr = ind.atr(c1h) or 0.0
    risk_now = (stop - mark) * qty if stop else float("inf")
    pnl = (entry - mark) * qty
    age_h = (now - datetime.fromisoformat(trade["opened_at"])).total_seconds() / 3600
    hit = set(trade.get("targets_hit") or [])
    targets = sorted([t["price"] for t in plan.get("targets") or [] if t["price"] not in hit and t["price"] < mark],
                     reverse=True)
    target_now = targets[0] if targets else None
    rr_now = (mark - target_now) / (stop - mark) if target_now and stop and stop > mark else None
    chand = chandelier_stop(c1h)
    size_ideal = budget_usd / (stop - mark) if stop and stop > mark else None
    acts: list[dict[str, Any]] = []

    def act(kind: str, why: str, **kw: Any) -> None:
        acts.append({"action": kind, "why": why, **kw})

    tsh = plan.get("time_stop_hours")
    if tsh and age_h >= tsh / 2 and trade.get("mfe_pct", 0) * entry / 100 * qty < 0.5 * r1:
        act("exit_or_shorten", f"age {age_h:.1f}h ≥ half the {tsh}h time stop and MFE < 0.5R: not working")
    if funding_8h is not None and funding_8h < 0 and tsh:
        cost = abs(funding_8h) * max(tsh - age_h, 0) / 8 * mark * qty
        if cost > 0.1 * r1:
            act("shorten", f"negative funding costs ~{cost:.2f} USDT over the remaining hold (> 0.1R)")
    if rr_now is not None and rr_now < 1:
        act("tighten_or_take_profit", f"reward:risk from here is {rr_now:.2f} (< 1) to target {target_now}")
    if size_ideal is not None and qty > size_ideal * 1.25:
        act("reduce", f"size {qty} > ideal {size_ideal:.4g} for today's budget {budget_usd:.2f} USDT",
            reduce_fraction=round(1 - size_ideal / qty, 3))
    if chand and stop and mark < chand < stop:
        act("tighten", f"chandelier stop {chand:.6g} is below the current stop {stop}", new_stop=round(chand, 8))
    if stop and atr and (stop - mark) < 0.75 * atr and "widened" not in (trade.get("notes") or ""):
        act("consider_widen_with_cut", f"stop only {(stop - mark) / atr:.2f} ATR away: inside normal noise; "
            "widen beyond the sweep level with a size cut (≤ 1R) or accept the exit")
    if pnl > 0 and atr and (entry - mark) >= 0.5 * atr and budget_usd > 0:
        act("consider_add", "in profit by ≥ 0.5 ATR: pyramid possible if the stop moves so total worst case ≤ 1R")
    if not acts:
        act("hold", "plan intact from the current state")
    return {"symbol": trade["symbol"], "price": mark, "entry": entry, "stop": stop, "contracts": qty,
            "open_pnl_usdt": round(pnl, 2), "open_pnl_R": round(pnl / r1, 2), "risk_to_stop_usdt": round(risk_now, 2),
            "risk_to_stop_R": round(risk_now / r1, 2) if stop else None, "initial_R_usdt": round(r1, 2),
            "atr_1h": atr, "stop_in_atr": round((stop - mark) / atr, 2) if stop and atr else None,
            "chandelier_stop": chand, "target_now": target_now, "reward_risk_now": rr_now,
            "age_h": round(age_h, 2), "budget_now_usdt": round(budget_usd, 2),
            "size_ideal": size_ideal, "funding_8h": funding_8h, "actions": acts}


class PositionOps:
    def __init__(self, trader):
        self.tr = trader

    async def reassess(self, symbol: str) -> dict[str, Any]:
        tr = self.tr
        symbol = to_symbol(symbol)
        pos = next((p for p in await tr.positions() if p["symbol"] == symbol), None)
        trade = tr.j.active_trade(symbol)
        if not pos or not trade or trade["status"] != "open":
            return {"ok": False, "error": f"no open journal-tracked position in {symbol}"}
        c1h = await tr.ex.public.fetch_ohlcv(symbol, "1h", limit=60)
        fr = await tr.ex.public.fetch_funding_rate(symbol)
        from .scanner import market_regime
        budget = await tr.budget(tr, symbol, (await market_regime(tr.ex))["verdict_for_shorts"])
        f8 = fr.get("fundingRate")
        res = reassess(trade, pos, c1h, f8, budget["risk_usd"])
        res["budget"] = budget
        if res.get("stop") and res.get("target_now"):
            from .probability import ProbabilityEngine
            plan = trade.get("plan") or {}
            left = max(1.0, float(plan.get("time_stop_hours") or 24) - res["age_h"])
            pe = await ProbabilityEngine(tr.ex).short_bracket(symbol, res["stop"], [res["target_now"]], left,
                                                              entry=res["price"])
            if pe.get("ok"):
                t = pe["targets"][0]
                res["probability_from_here"] = {
                    "label": pe["label"], "hours_left": round(left, 1), "p_target_first": t["p_target"],
                    "p_stop_first": t["p_stop"], "p_neither": t["p_time"], "mc_p_target": t.get("mc_p_target"),
                    "reward_R_from_here": t["reward_R"], "note": "our thesis must beat these odds to have edge"}
        return res

    async def widen_stop(self, symbol: str, new_stop: float, reason: str) -> dict[str, Any]:
        tr = self.tr
        symbol = to_symbol(symbol)
        if blocked := await killswitch.gate(tr, "widen_stop"):   # can raise the loss at stop back up to 1R
            return blocked
        async with tr.symbol_lock(symbol, f"widen stop to {new_stop:g}: {reason}"):
            if blocked := killswitch.state_refusal(tr.j, "widen_stop"):   # tripped while we waited for the lock
                return blocked
            await tr.ex.trade_markets()
            pos, trade = await tr.position(symbol), tr.j.active_trade(symbol)
            if not pos or not trade:
                return {"ok": False, "error": "no journal-tracked position"}
            if "widened" in (trade.get("notes") or ""):
                return {"ok": False, "error": "stop already widened once on this trade (rule: at most once)"}
            stops = await tr.stop_orders(symbol)
            cur = min((s["trigger"] for s in stops), default=None)
            entry, qty = float(pos["entryPrice"]), float(pos["contracts"])
            if cur is None or new_stop <= cur:
                return {"ok": False, "error": "not a widening (use move_stop to tighten)"}
            liq = float(pos.get("liquidationPrice") or 0)
            if liq and new_stop >= liq * (1 - tr.ex.limits.liq_buffer_pct / 100):
                return {"ok": False, "error": f"new stop {new_stop} too close to liquidation {liq}"}
            r1 = _r_initial(trade)
            allowed = r1 / (new_stop - entry) if new_stop > entry else qty
            cut = qty - allowed
            if cut > 0:
                cut = float(tr.t.amount_to_precision(symbol, cut + float(tr.t.market(symbol)["precision"]["amount"] or 0)))
                await tr._market_close(symbol, min(cut, qty), whole=cut >= qty)
            for s in stops:
                await tr.t.cancel_order(s["id"], symbol, params={"trigger": True})
            try:
                await tr._place_stop(symbol, new_stop, tries=3)
            except Exception as e:  # noqa: BLE001 - restore the old stop, never leave it naked
                await tr._place_stop(symbol, cur, tries=3)
                return {"ok": False, "error": f"new stop rejected ({e}); old stop restored", "reduced": max(cut, 0)}
            tr.j.update_trade(trade["id"], stop_price=new_stop,
                              notes=f"{trade.get('notes') or ''} widened {cur}->{new_stop}".strip())
            tr.j.log_decision("widen_stop", reason, symbol, snapshot={"from": cur, "to": new_stop, "cut": max(cut, 0),
                                                                      "r_initial": r1})
            await tr.alert("action_widen", f"stop {cur} -> {new_stop}, size cut {max(cut, 0)}: {reason}", symbol, False)
            return {"ok": True, "old_stop": cur, "new_stop": new_stop, "size_cut": max(cut, 0),
                    "remaining": round(qty - max(cut, 0), 8), "max_loss_usdt": round(r1, 2)}

    async def add_to_winner(self, symbol: str, notional: float, new_stop: float, reason: str) -> dict[str, Any]:
        tr = self.tr
        symbol = to_symbol(symbol)
        if blocked := await killswitch.gate(tr, "add_to_winner"):   # before the pre-add stop tighten
            return blocked
        async with tr.symbol_lock(symbol, f"add {notional:g} USDT to a winner: {reason}"):   # one view: decide, act
            return await self._add(symbol, notional, new_stop, reason)

    async def _add(self, symbol: str, notional: float, new_stop: float, reason: str) -> dict[str, Any]:
        tr = self.tr
        pos, trade = await tr.position(symbol), tr.j.active_trade(symbol)
        if not pos or not trade:
            return {"ok": False, "error": "no journal-tracked position"}
        entry, qty, mark = float(pos["entryPrice"]), float(pos["contracts"]), float(pos["markPrice"])
        if mark >= entry:
            return {"ok": False, "error": "only add to winners (price must be below entry); never average up"}
        from .tranches import pending_legs
        if pending_legs(tr.j, symbol):   # the resting leg IS the planned add; both would break the 1R budget
            return {"ok": False, "error": "a split-entry leg is still resting for this symbol: that is the add"}
        add_qty = float(tr.t.amount_to_precision(symbol, notional / mark))
        new_avg = (entry * qty + mark * add_qty) / (qty + add_qty)
        worst = (new_stop - new_avg) * (qty + add_qty)
        r1 = _r_initial(trade)
        cap = r1 * max(1.0, tr.ex.limits.max_add_r_mult)     # > 1R only via set_risk_params (experiment tag)
        if worst > cap + 1e-9:
            return {"ok": False, "error": f"after the add, loss at stop {new_stop} would be {worst:.2f} > "
                                          f"{cap / r1 if r1 else 1:g}R ({cap:.2f}); tighten the stop or add less"}
        if worst > r1 + 1e-9:   # extra risk must fit today's governor room (daily cap), never around a halt
            from .risk_budget import governor_status
            gov = await governor_status(tr, "unknown")
            room = ((gov.get("daily") or {}).get("room_usd"))
            if room is not None:   # the daily room must also carry the risk already open on the OTHER positions
                room -= sum(max(0.0, (p["exchange_stop"] - p["entry"]) * p["contracts"])
                            for p in await tr.positions() if p["symbol"] != symbol and p.get("exchange_stop"))
            if gov.get("halt") or (room is not None and worst - r1 > room):
                return {"ok": False, "error": f"extra risk {worst - r1:.2f} over 1R does not fit the governor "
                                              f"(halt={gov.get('halt')}, daily room={room})"}
        cur_stops = [s["trigger"] for s in await tr.stop_orders(symbol, close_position_only=True)]
        if not cur_stops or new_stop < min(cur_stops) * 0.9999:   # tighten first; an already-fitting stop stays
            mv = await tr.move_stop(symbol, new_stop, f"pre-add tighten: {reason}")  # protect first
            if not mv.get("ok"):
                return {"ok": False, "error": f"could not tighten the stop first: {mv.get('error')}"}
        if blocked := killswitch.state_refusal(tr.j, "add_to_winner"):   # tripped meanwhile: the tightened stop stays
            return blocked
        cid = clientid.new("add", trade["id"])
        try:
            o = await tr._market_sell(symbol, add_qty, cid)
        except clientid.Unknown as e:   # it may have run: never re-sent (#9); logged as a size change
            msg = f"add of {add_qty}: {e}. Not re-sent: re-read the position before adding again"
            tr.j.log_decision("add", f"UNKNOWN: {reason}", symbol, snapshot={"add_qty": add_qty, "client_id": cid,
                                                                             "unknown": True, "new_stop": new_stop})
            await tr.alert("add_unknown", msg, symbol, True)
            return {"ok": False, "unknown": True, "client_id": cid, "error": msg}
        except Exception as e:  # noqa: BLE001 - a definite refusal: nothing ran; the tightened stop stays
            return {"ok": False, "error": f"add refused: {e}"}
        if o.get("filled") is not None and not float(o["filled"]):
            return {"ok": False, "error": f"add order {o.get('status')}: nothing filled"}
        add_qty = float(o.get("filled") or add_qty)   # what filled
        mark = float(o.get("average") or 0) or mark
        plan = dict(trade.get("plan") or {})
        plan.setdefault("r1_usdt", r1)                               # pin 1R before the amount grows
        plan.setdefault("full_size", trade.get("amount") or qty)     # ladder exposure base (x = contracts/full_size)
        tr.j.update_trade(trade["id"], plan=plan,   # amount = peak size: targets are its shares
                          amount=max(float(trade.get("amount") or 0), qty + add_qty),
                          notes=f"{trade.get('notes') or ''} added {add_qty}@{mark}".strip())
        tr.j.log_decision("add", reason, symbol, snapshot={"add_qty": add_qty, "price": mark, "new_stop": new_stop,
                                                           "worst_case": worst, "r_initial": r1,
                                                           "worst_case_R": round(worst / r1, 2) if r1 else None})
        await tr.alert("action_add", f"added {add_qty} @ {mark}; stop {new_stop}; worst case {worst:.2f}", symbol, False)
        return {"ok": True, "added": add_qty, "price": mark, "new_stop": new_stop, "worst_case_usdt": round(worst, 2)}
