"""Live position management from the CURRENT state (docs/POSITION_MANAGEMENT.md §b).

`reassess()` is pure: given the trade, live position, fresh candles, funding and the current risk budget, it
re-plans the trade as if entering now and returns ranked, reasoned actions. The analyst decides; the
actions below execute the non-trivial ones safely:

- widen_stop: allowed ONCE per trade, only with a size cut so the $ loss at the new stop ≤ the initial 1R; the risk it
  adds at the stop (from the mark) must fit today's daily-loss room, as a new trade's budget must (#74)
- add_to_winner: only in profit; the stop is tightened FIRST so total worst case after the add ≤ 1R; inside open_short's
  caps and the pyramiding rules (risk.check_add: ≥ min_add_r, ≤ max_add_frac of the initial size, ≤ max_adds)
(Tightening and partial exits already exist: Trader.move_stop / Trader.close(fraction).)
Both add risk: each holds the account lock (symlock.py, #74), then the symbol's, from its checks through the send. The
guardian never widens or adds. Each journals its size change in a unit it awaits inside those locks (`_resized`, #82).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from . import clientid, errors, exposure, killswitch, stops, targets_sync
from . import indicators as ind
from .audit import refusals
from .exchange import to_symbol
from .journal import Journal
from .risk import AddRequest, RiskViolation, check_add
from .steps import floor_amount
from .symlock import busy_refusal

log = logging.getLogger("shortbot.positioning")


def _r_initial(trade: dict) -> float:
    """The trade's 1R in USDT, fixed at entry. `plan.r1_usdt` pins it once adds change the amount (or a split entry
    sized the budget for more than the first leg), so a bigger position never buys itself a bigger risk budget."""
    pinned = (trade.get("plan") or {}).get("r1_usdt")
    if pinned:
        return float(pinned)
    return max(0.0, ((trade.get("initial_stop") or 0) - (trade.get("entry_price") or 0)) * (trade.get("amount") or 0))


def adds_done(j, trade: dict) -> int:
    """Adds already made on this trade: its `add` decisions, filled or of unknown outcome (an ambiguous send may have
    filled, #9); a refused add logs `add_rejected`. Rows are matched by symbol from the trade's opening on and by the
    trade id in the snapshot, which is unique across both desks."""
    rows = j._rows("SELECT snapshot FROM decisions WHERE symbol=? AND action='add' AND ts >= ?",
                   (trade["symbol"], trade.get("opened_at") or ""))
    return sum((json.loads(r["snapshot"] or "{}") or {}).get("trade_id") == trade["id"] for r in rows)


def _resized(j, trade_id: int, fields: dict[str, Any], action: str, reason: str, symbol: str, snap: dict) -> None:
    """A size change's journal as one unit, awaited inside the symbol lock (#82): the trade row and the decision that
    adds_done and close_intent read. Posted, it could still wait in this process's queue when the next holder of the
    lock (maybe another process) reads them."""
    j.update_trade(trade_id, **fields)
    j.log_decision(action, reason, symbol, snapshot=snap)


def chandelier_stop(c1h: list[list[float]], n: int = 22, k: float = 3.0) -> float | None:
    """Short-side chandelier exit: lowest low of n bars + k·ATR(n) (LeBeau)."""
    if len(c1h) <= n:
        return None
    atr = ind.atr(c1h, n)
    return min(c[3] for c in c1h[-n:]) + k * atr if atr else None


def reassess(trade: dict, pos: dict, c1h: list[list[float]], funding_8h: float | None,
             budget_usd: float, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
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
        from .market.regime import market_regime
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

    @refusals("widen_stop")
    @busy_refusal
    async def widen_stop(self, symbol: str, new_stop: float, reason: str) -> dict[str, Any]:
        tr = self.tr
        symbol = to_symbol(symbol)
        if blocked := await killswitch.gate(tr, "widen_stop"):   # can raise the loss at stop back up to 1R
            return blocked
        async with (tr.account_lock(f"widen {symbol} stop to {new_stop:g}: {reason}"),   # it adds risk (#74)
                    tr.symbol_lock(symbol, f"widen stop to {new_stop:g}: {reason}")):
            if blocked := killswitch.state_refusal(tr.j, "widen_stop"):   # tripped while we waited for the lock
                return blocked
            await tr.ex.trade_markets()
            pos, trade = await tr.position(symbol), tr.j.active_trade(symbol)
            if not pos or not trade:
                return {"ok": False, "error": "no journal-tracked position"}
            if "widened" in (trade.get("notes") or ""):
                return {"ok": False, "error": "stop already widened once on this trade (rule: at most once)"}
            old = await tr.stop_orders(symbol, close_position_only=True)   # a resting entry's pre-arm isn't ours to move
            cur = min((s["trigger"] for s in old), default=None)
            entry, qty = float(pos["entryPrice"]), float(pos["contracts"])
            new_stop = float(tr.t.price_to_precision(symbol, new_stop))
            if cur is None or new_stop <= cur:
                return {"ok": False, "error": "not a widening (use move_stop to tighten)" if cur else
                        "no closePosition stop to widen: the guardian restores it first"}
            liq = float(pos.get("liquidationPrice") or 0)
            if liq and new_stop >= liq * (1 - tr.ex.limits.liq_buffer_pct / 100):
                return {"ok": False, "error": f"new stop {new_stop} too close to liquidation {liq}"}
            r1 = _r_initial(trade)
            allowed = r1 / (new_stop - entry) if new_stop > entry else qty
            cut = qty - allowed
            if cut > 0:
                cut = float(tr.t.amount_to_precision(symbol, cut + float(tr.t.market(symbol)["precision"]["amount"] or 0)))
            if cut >= qty:   # nothing would be left to protect at the wider stop
                return {"ok": False, "error": f"a widen to {new_stop} within 1R ({r1:.2f}) leaves no size: close instead"}
            if refused := await self._widen_room(pos, cur, new_stop, qty, max(cut, 0.0)):
                tr.j.log_decision("widen_rejected", reason, symbol, snapshot={"from": cur, "to": new_stop, **refused})
                return refused
            from .entries_partial import pull_for

            # a resting remainder refilling at the wider stop would take the loss past 1R (entries_partial.must_go)
            await pull_for(tr, symbol, f"stop widened to {new_stop:g}: {reason}")
            return await self._widen(trade, symbol, old, cur, new_stop, qty, max(cut, 0.0), r1, reason)

    async def _widen_room(self, pos: dict, cur: float, new_stop: float, qty: float, cut: float) -> dict | None:
        """A session's voluntary widen adds risk at the stop: loss from the mark at the new stop (after the cut) minus
        at the current one. It must fit today's daily-loss room after every open stop (risk_budget.room_left_today, the
        room a new trade is capped at), and never a governor halt. None = fits; else the refusal, with the numbers."""
        mark = float(pos.get("markPrice") or 0)
        added = max(0.0, (new_stop - mark) * (qty - cut)) - max(0.0, (cur - mark) * qty)
        if added <= 1e-9:
            return None
        from .risk_budget import room_today
        room = await room_today(self.tr, await self._regime())
        if room["room_usd"] >= added - 1e-9 and not room["halt"]:
            return None
        why = (f"widening to {new_stop:g} adds {added:.2f} USDT of risk at the stop (from the mark {mark:g}), more "
               f"than today's daily-loss room left: {room['room_usd']:.2f} USDT = daily room "
               f"{room['daily_room_usd']:.2f} - {room['open_risk_usd']:.2f} already at risk at the open stops"
               + (", and the governor halts new risk" if room["halt"] else ""))
        return {"ok": False, "error": why, "rejected_by_risk": [why], "added_risk_usdt": round(added, 2), "room": room,
                "hint": "widen less, close part first, or change the daily cap openly (set_risk_params)"}

    async def _regime(self) -> str:
        """The market regime for the governor's daily cap, as a new trade's budget reads it; "unknown" (the smallest
        cap) when it can't be read."""
        from .market.regime import market_regime
        try:
            return (await market_regime(self.tr.ex))["verdict_for_shorts"]
        except Exception as e:  # noqa: BLE001 - unread: "unknown" sizes the daily cap down, never up
            log.warning("market regime unread for the widen room (%s: %s): 'unknown'", type(e).__name__, e)
            return "unknown"

    async def _widen(self, trade: dict, symbol: str, old: list[dict], cur: float, new_stop: float, qty: float,
                     cut: float, r1: float, reason: str) -> dict[str, Any]:
        """Stop first, then the size cut (#14): the size never changes without its new stop resting, and a stop that
        can't be moved costs no size. Bounded residual: a process killed between the two leaves the uncut size at the
        wider stop (loss at stop up to qty x (new - entry), over 1R by the cut's share) and the journal still on the old
        stop, until a human or the next session sees it. A cut that fails moves the stop back (a tighten)."""
        tr = self.tr
        snap = {"from": cur, "to": new_stop, "cut": cut, "r_initial": r1}
        res = await stops.set_stop(tr, symbol, new_stop, qty, old)
        if res.get("closed") or not res.get("ok"):
            tr.j.log_decision("widen_stop_failed", reason, symbol, snapshot={**snap, "cut": 0, "result": res})
            return {**res, "size_cut": 0}
        left: float | None   # None: the cut's outcome unread
        left = qty
        if cut > 0:
            await targets_sync.pull_safe(tr, symbol, "widen: before its size cut")   # #65: none fills with the cut
            try:
                got = await tr.close_market(symbol, cut, whole=False)   # never re-sent blind (#9)
                err = None
            except Exception as e:  # noqa: BLE001 - incl. clientid.Unknown: the position decides below
                got, err = {}, e
            if got.get("rejected"):   # -2022 on a flat position: a stop or target closed it before the cut
                await stops.strip_flat(tr, symbol)   # the widened stop protects nothing now
                tr.j.log_decision("widen_stop_failed", reason, symbol, snapshot={**snap, "cut": 0, "result": "flat"})
                return {"ok": True, "closed": True, "size_cut": 0, "reason": "the position closed before the size cut "
                        "(a stop or target filled first); the guardian's reconcile journals that exit"}
            try:
                left = float((await tr.position(symbol) or {}).get("contracts") or 0)
            except Exception:  # noqa: BLE001 - unread: a failed cut counts as not done (stop moved back below)
                left = None if err else qty - cut
            if left is None or left > qty - cut + 1e-9:
                back = await tr.move_stop(symbol, cur, f"widen undone: the size cut failed ({err})")
                msg = (f"size cut of {cut:g} failed ({err}); stop moved back to {cur}" if back.get("ok") else
                       f"size cut of {cut:g} failed ({err}) AND moving the stop back to {cur} failed "
                       f"({back.get('error')}): the full size rests at the wider stop {new_stop}. Cut it now")
                tr.j.log_decision("widen_stop_failed", reason, symbol, snapshot={**snap, "cut": 0, "error": msg})
                await tr.alert("widen_undone", msg, symbol, not back.get("ok"))
                return {"ok": False, "error": msg, "size_cut": 0}
        await tr.j.write(_resized, trade["id"], {"stop_price": new_stop, "notes": f"{trade.get('notes') or ''} widened "
                         f"{cur}->{new_stop}".strip()}, "widen_stop", reason, symbol, {**snap, "remaining": left})
        await targets_sync.after(tr, symbol, "widen")
        await tr.alert("action_widen", f"stop {cur} -> {new_stop}, size cut {cut}: {reason}", symbol, False)
        return {"ok": True, "old_stop": cur, "new_stop": new_stop, "size_cut": cut, "remaining": round(left, 8),
                "max_loss_usdt": round(r1, 2), **({"warnings": res["warnings"]} if res.get("warnings") else {})}

    @refusals("add_to_winner")
    @busy_refusal
    async def add_to_winner(self, symbol: str, notional: float, new_stop: float, reason: str) -> dict[str, Any]:
        tr = self.tr
        symbol = to_symbol(symbol)
        if blocked := await killswitch.gate(tr, "add_to_winner"):   # before the pre-add stop tighten
            return blocked
        async with (tr.account_lock(f"add {notional:g} USDT to {symbol}: {reason}"),   # the caps through the send (#74)
                    tr.symbol_lock(symbol, f"add {notional:g} USDT to a winner: {reason}")):   # one view: decide, act
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
        add_qty = floor_amount(tr.t, symbol, notional / mark)   # 0.0 under one step: check_add refuses it (#71)
        new_avg = (entry * qty + mark * add_qty) / (qty + add_qty)
        worst = (new_stop - new_avg) * (qty + add_qty)
        r1 = _r_initial(trade)
        cur = min((s["trigger"] for s in await tr.stop_orders(symbol, close_position_only=True)), default=None)
        tighten = cur is None or new_stop < cur * 0.9999   # tighten first; an already-fitting stop stays
        stop_after = new_stop if tighten or cur is None else cur   # (no stop: tighten)
        if refused := await self._refusals(trade, pos, add_qty, new_stop, stop_after, worst):
            tr.j.log_decision("add_rejected", "; ".join(refused), symbol, snapshot={
                "trade_id": trade["id"], "add_qty": add_qty, "price": mark, "new_stop": new_stop, "worst_case": worst})
            return {"ok": False, "error": "; ".join(refused), "rejected_by_risk": refused,
                    "hint": "the caps and pyramiding rules are research params: change them openly with "
                            "set_risk_params under an experiment tag; never split the add"}
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
        if tighten:
            mv = await tr.move_stop(symbol, new_stop, f"pre-add tighten: {reason}")  # protect first
            if not mv.get("ok"):
                return {"ok": False, "error": f"could not tighten the stop first: {mv.get('error')}"}
        if blocked := killswitch.state_refusal(tr.j, "add_to_winner"):   # tripped meanwhile: the tightened stop stays
            return blocked
        cid = clientid.new("add", trade["id"])
        try:
            o = await tr.open_market(symbol, add_qty, cid)
        except clientid.Unknown as e:   # it may have run: never re-sent (#9); logged as a size change
            msg = f"add of {add_qty}: {e}. Not re-sent: re-read the position before adding again"
            await tr.j.write(Journal.log_decision, "add", f"UNKNOWN: {reason}", symbol, snapshot={   # an add (adds_done)
                "trade_id": trade["id"], "add_qty": add_qty, "client_id": cid, "unknown": True, "new_stop": new_stop})
            await tr.alert("add_unknown", msg, symbol, True)
            return {"ok": False, "unknown": True, "client_id": cid, "error": msg}
        except Exception as e:  # noqa: BLE001 - a definite refusal (open_market raises no other); tighter stop stays
            return {"ok": False, "error": f"add refused: {errors.describe(e)}"}
        if o.get("filled") is not None and not float(o["filled"]):
            return {"ok": False, "error": f"add order {o.get('status')}: nothing filled"}
        add_qty = float(o.get("filled") or add_qty)   # what filled
        mark = float(o.get("average") or 0) or mark
        plan = dict(trade.get("plan") or {})
        plan.setdefault("r1_usdt", r1)                               # pin 1R before the amount grows
        plan.setdefault("full_size", trade.get("amount") or qty)     # ladder exposure base (x = contracts/full_size)
        await tr.j.write(_resized, trade["id"], {   # amount = peak size: targets are its shares
            "plan": plan, "amount": max(float(trade.get("amount") or 0), qty + add_qty),
            "notes": f"{trade.get('notes') or ''} added {add_qty}@{mark}".strip()}, "add", reason, symbol, {
            "trade_id": trade["id"], "client_id": cid, "add_qty": add_qty, "price": mark, "new_stop": new_stop,
            "worst_case": worst, "r_initial": r1, "worst_case_R": round(worst / r1, 2) if r1 else None})
        await tr.alert("action_add", f"added {add_qty} @ {mark}; stop {new_stop}; worst case {worst:.2f}", symbol, False)
        await targets_sync.after(tr, symbol, "add")   # #65: the plan's shares of the grown size
        return {"ok": True, "added": add_qty, "price": mark, "new_stop": new_stop, "worst_case_usdt": round(worst, 2)}

    async def _refusals(self, trade: dict, pos: dict, add_qty: float, new_stop: float, stop_after: float,
                        worst: float) -> list[str]:
        """Every reason to refuse the add, before anything moves: the loss at new_stop vs the pinned 1R, open_short's
        caps and the pyramiding rules (risk.check_add, on the stop that rests after the add), the exchange minimum."""
        tr, lim = self.tr, self.tr.ex.limits
        symbol, mark, qty, r1 = trade["symbol"], float(pos["markPrice"]), float(pos["contracts"]), _r_initial(trade)
        cap = r1 * max(1.0, lim.max_add_r_mult)     # > 1R only via set_risk_params (experiment tag)
        out = [f"after the add, loss at stop {new_stop} would be {worst:.2f} > {cap / r1 if r1 else 1:g}R ({cap:.2f}); "
               "tighten the stop or add less"] if worst > cap + 1e-9 else []
        initial = float((trade.get("plan") or {}).get("full_size") or trade.get("amount") or qty)
        lev = int(float(pos.get("leverage") or trade.get("leverage") or 0))
        req = AddRequest(symbol, add_qty, mark, stop_after, qty, float(pos["entryPrice"]), lev, initial, r1,
                         adds_done(tr.j, trade), await tr.ex.maint_margin(symbol, (qty + add_qty) * mark))
        try:
            acct = await tr.account_state()
            acct = exposure.counted(tr.j, acct, lim.position_cap(acct.equity), symbol=symbol)
            check_add(req, acct, lim)   # resting entries and unknown entries count (#74)
        except RiskViolation as e:
            out += e.reasons
        min_cost = tr.t.market(symbol)["limits"]["cost"]["min"] or 0
        if 0 < add_qty * mark < min_cost:
            out.append(f"add of {add_qty * mark:.2f} USDT is below the exchange minimum {min_cost}")
        return out
