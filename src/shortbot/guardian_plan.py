"""Guardian plan execution (protective lane, inside the 60 s positions pass): each open trade's plan decided by
manager.plan_actions on the live position, applied under the symbol lock."""

from __future__ import annotations

from .manager import excursions, plan_actions


class PlanChecks:
    """Guardian mixin: needs app, j, n."""

    async def _run_plan(self, trade: dict, p: dict) -> None:
        ex = excursions(trade, p["mark"])
        self.j.update_trade(trade["id"], mfe_pct=round(ex["mfe_pct"], 3), mae_pct=round(ex["mae_pct"], 3),
                            low_since_entry=ex["low_since_entry"])
        trade = {**trade, **ex}
        tr, sym = self.app.trader, trade["symbol"]
        acts = plan_actions(trade, p)
        if all(a.kind == "alert" for a in acts):
            return await self._apply_plan(trade, p, acts)
        async with tr.symbol_lock(sym, "guardian plan"):
            # decide again on what is true NOW: a session may have closed, added or moved the stop since the snapshot
            pos = await tr.position(sym)
            fresh = self.j.get_trade(trade["id"])
            if not pos or not fresh or fresh["status"] != "open":
                return
            stop = min((s["trigger"] for s in await tr.stop_orders(sym)), default=None)
            p = {"mark": float(pos["markPrice"]), "contracts": float(pos["contracts"]), "exchange_stop": stop}
            trade = {**fresh, **excursions(fresh, p["mark"])}
            await self._apply_plan(trade, p, plan_actions(trade, p))

    async def _apply_plan(self, trade: dict, p: dict, acts: list) -> None:
        tr, sym = self.app.trader, trade["symbol"]
        hits, cur = list(trade.get("targets_hit") or []), p["contracts"]
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
                hits.append(a.target_price)                 # one running list: two actions in one cycle both stick
                self.j.update_trade(trade["id"], targets_hit=list(hits))
                await self.n.send("plan_target_done", a.reason, sym, cooldown_s=0)
            elif a.kind == "close":
                r = await tr.close(sym, f"plan/{a.reason}", fraction=a.fraction or 1.0, expect_contracts=cur)
                cur = r.get("remaining", cur)   # T1 and T2 in one pass: the second sees what the first left
                if a.target_price is not None and r.get("ok"):
                    hits.append(a.target_price)
                    self.j.update_trade(trade["id"], targets_hit=list(hits))
                await self.n.send("plan_close", f"{a.reason}: {r}", sym, cooldown_s=0)
                if (a.fraction or 1.0) >= 1.0:
                    return
