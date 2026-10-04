"""Amending a resting limit entry (mixed into EntryDesk, entries.py): move it ONCE (don't chase).

A lower sell limit fills sooner and, at the same size, widens the stop distance: more USDT at the stop than the budget
sized it for. So lowering re-runs the placement's risk check (#12): the kill switch, the CURRENT risk budget (not
blocked; risk at the stop within it) and the per-order hard caps (stop-distance range, liquidation buffer, risk per
trade). A split entry's leg is never lowered: its legs share one pinned 1R (tranches.finalize); cancel the group and
re-propose instead. Raising the limit toward the stop only cuts risk. Every amend and every refusal is a logged
decision. A lowering holds the account lock (symlock.py, #74) from its risk check through the edit.
"""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from . import killswitch
from .audit import refusals
from .entry_gates import stamps
from .risk import AccountState, RiskViolation, ShortRequest, check_open_short
from .symlock import busy_refusal

if TYPE_CHECKING:
    from .journal import Journal
    from .trading import Trader


class EntryAmend:
    """Uses EntryDesk's trader, journal, regime and entry-row storage."""
    tr: Trader
    j: Journal

    if TYPE_CHECKING:   # EntryDesk's
        async def _regime(self) -> str: ...

        def _rows(self, where: str = "1=1", args: tuple = ()) -> list[dict[str, Any]]: ...

        async def _update(self, eid: int, **f: Any) -> None: ...

    @refusals("amend")
    @busy_refusal
    async def amend(self, entry_id: int, new_limit: float, reason: str) -> dict[str, Any]:
        rows = self._rows("id=? AND status='pending'", (entry_id,))
        if not rows:
            return {"ok": False, "error": "no pending entry with that id"}
        e = rows[0]
        if e["replaced"]:
            return {"ok": False, "error": "already replaced once (rule: replace at most once, don't chase)"}
        if new_limit >= e["stop_price"]:
            return {"ok": False, "error": "new limit must stay below the stop"}
        t, sym = self.tr.t, e["symbol"]
        px = float(t.price_to_precision(sym, new_limit))
        lower = px < e["limit_price"]
        if lower and e.get("tranche"):
            return {"ok": False, "error": "a split entry's leg is never lowered: its legs share one pinned 1R. Cancel "
                                          "the group and re-propose it at the new levels"}
        if lower and (blocked := await killswitch.gate(self.tr, "amend (lower limit)")):
            return blocked
        snap = {"entry_id": entry_id, "old_limit": e["limit_price"], "new_limit": px, "amount": e["amount"],
                "old_risk_usdt": round(e["amount"] * (e["stop_price"] - e["limit_price"]), 4),
                "risk_usdt": round(e["amount"] * (e["stop_price"] - px), 4)}
        # lowering adds risk: its check through the edit holds the account lock (#74); raising only cuts risk
        async with self.tr.account_lock(f"lower entry #{entry_id} to {px:g}") if lower else nullcontext():
            if lower:
                refused, extra = await self._amend_risk(e, px, snap["risk_usdt"])
                if refused:
                    self.j.log_decision("rejected", f"amend entry #{entry_id} to {px}: " + "; ".join(
                        refused.get("rejected_by_budget") or refused["rejected_by_risk"]), sym, snapshot=snap)
                    return refused
                snap.update(extra)
            async with self.tr.symbol_lock(sym, f"amend entry #{entry_id}: {reason}"):
                if not self._rows("id=? AND status='pending' AND COALESCE(replaced, 0)=0 AND limit_price=?",
                                  (entry_id, e["limit_price"])):
                    return {"ok": False, "error": f"entry #{entry_id} changed since you looked (filled, cancelled or "
                                                  "amended meanwhile): re-read pending_entries"}
                if lower and (blocked := killswitch.state_refusal(self.j, "amend (lower limit)")):
                    return blocked
                await self.tr.edit_limit(sym, e["order_id"], e["amount"], px)
                await self._update(e["id"], limit_price=px, replaced=1, reason=f"amended: {reason}",
                                   **(stamps(snap.get("budget_multipliers")) if lower else {}))   # lowered: re-sized
        self.j.log_decision("amend_entry", reason, sym, snapshot=snap)
        return {"ok": True, "entry_id": entry_id, "new_limit": px, "risk_usdt": snap["risk_usdt"]}

    async def _amend_risk(self, e: dict, px: float, risk: float) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """(refusal or None, what the check saw). The risk budget for this symbol now, then the per-order caps. An
        amend changes no portfolio count (open symbols, total notional only shrinks), so those aren't re-checked."""
        tr, sym = self.tr, e["symbol"]
        budget = await tr.budget(tr, sym, await self._regime(), e.get("conviction") or "B", e["setup"])
        seen = {"budget_risk_usd": budget["risk_usd"], "budget_multipliers": budget.get("multipliers")}
        if budget["blocked"]:
            return {"ok": False, "rejected_by_budget": budget["reasons"] or ["no risk budget left"]}, seen
        if risk > budget["risk_usd"] * 1.001:
            return {"ok": False, "rejected_by_budget": [
                f"risk at stop {risk:.2f} USDT at {px} > current budget {budget['risk_usd']} USDT: cancel and "
                "re-propose to re-size at the new level"]}, seen
        acct = await tr.account_state()
        qv = float((await tr.ex.public.fetch_ticker(sym)).get("quoteVolume") or 0)
        maint = await tr.ex.maint_margin(sym, e["amount"] * px)   # the symbol's leverage bracket
        try:
            check_open_short(ShortRequest(sym, e["amount"] * px, px, e["stop_price"], int(e["leverage"]), qv, maint),
                             AccountState(acct.equity, (), 0.0, acct.day_pnl), tr.ex.limits)
        except RiskViolation as x:
            return {"ok": False, "rejected_by_risk": x.reasons}, seen
        return None, seen
