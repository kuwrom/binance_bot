"""Guardian plan execution (protective lane, inside the 60 s positions pass): each open trade's plan decided by
manager.plan_actions on the live position, applied under the symbol lock. Resting targets (#65, targets_sync.py):
their fills are settled before the plan decides (a filled target is hit, not crossed), and the book is synced after
it acts. The lock is taken only when an action is due or the targets need it (targets_sync.due)."""

from __future__ import annotations

import logging

from . import targets, targets_sync
from .guardian_loop import send_safe
from .manager import excursions, plan_actions
from .symlock import SymbolBusy

log = logging.getLogger("shortbot.guardian")


class PlanChecks:
    """Guardian mixin: needs app, j, n."""

    async def _targets(self, coro, sym: str | None = None, default=None):
        """A target step never costs the pass's stop and plan checks: a failure is alerted (SymbolBusy: next pass)."""
        try:
            return await coro
        except SymbolBusy:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("targets %s failed", sym)
            await send_safe(self.n, "target_sync_error", f"{type(e).__name__}: {e}", sym, cooldown_s=1800)
            return default

    async def _run_plan(self, trade: dict, p: dict) -> None:
        ex = excursions(trade, p["mark"])
        self.j.update_trade(trade["id"], mfe_pct=round(ex["mfe_pct"], 3), mae_pct=round(ex["mae_pct"], 3),
                            low_since_entry=ex["low_since_entry"])
        trade = {**trade, **ex}
        tr, sym = self.app.trader, trade["symbol"]
        acts = plan_actions(trade, p)
        due = await self._targets(targets_sync.due(tr, trade, p), sym, default=True)
        if all(a.kind == "alert" for a in acts) and not due:
            return await self._apply_plan(trade, p, acts)
        async with tr.symbol_lock(sym, "guardian plan"):
            # decide again on what is true NOW: a session may have closed, added or moved the stop since the snapshot
            pos = await tr.position(sym)
            if not pos:
                return
            await self._targets(targets_sync.settle(tr, sym), sym)   # a filled target is hit, not crossed
            fresh = self.j.get_trade(trade["id"])
            if not fresh or fresh["status"] != "open":
                return
            stop = min((s["trigger"] for s in await tr.stop_orders(sym)), default=None)
            p = {"mark": float(pos["markPrice"]), "contracts": float(pos["contracts"]), "exchange_stop": stop}
            trade = {**fresh, **excursions(fresh, p["mark"])}
            await self._apply_plan(trade, p, plan_actions(trade, p))
            await self._targets(targets_sync.sync(tr, sym, "guardian pass"), sym)

    async def _apply_plan(self, trade: dict, p: dict, acts: list) -> None:
        tr, sym = self.app.trader, trade["symbol"]
        cur = p["contracts"]
        for a in acts:
            if a.kind == "alert":
                await self.n.send(f"plan:{a.key or a.kind}", a.reason, sym, cooldown_s=a.cooldown_s, urgent=a.urgent)
            elif a.kind == "move_stop":
                r = await tr.move_stop(sym, a.price, f"plan/{a.reason}")
                if r.get("noop"):          # rounds to the stop already in place
                    continue
                await self.n.send("plan_stop_moved" if r.get("ok") else "plan_stop_move_failed",
                                  f"{a.reason} -> {a.price:.6g}: {r}", sym, cooldown_s=0)
            elif a.kind == "target_done":
                targets.mark_hit(self.j, trade["id"], a.target_price)   # re-read: a fill may have added one
                await self.n.send("plan_target_done", a.reason, sym, cooldown_s=0)
            elif a.kind == "close":
                if a.cum is not None:   # a target's market fallback: idempotent, whatever its limit filled meanwhile
                    r = await tr.close(sym, f"plan/{a.reason}", fraction=a.cum, of_original=True)
                else:
                    r = await tr.close(sym, f"plan/{a.reason}", fraction=a.fraction or 1.0, expect_contracts=cur)
                cur = r.get("remaining", cur)   # T1 and T2 in one pass: the second sees what the first left
                if a.target_price is not None and (r.get("ok") or r.get("noop")):
                    targets.mark_hit(self.j, trade["id"], a.target_price)
                await self.n.send("plan_close", f"{a.reason}: {r}", sym, cooldown_s=0)
                if (a.fraction or 1.0) >= 1.0:
                    return
