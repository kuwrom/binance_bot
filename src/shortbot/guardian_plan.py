"""Guardian plan execution (protective lane, inside the 60 s positions pass): each open trade's plan decided by
manager.plan_actions on the live position, applied under the symbol lock. Resting targets (#65, targets_sync.py):
their fills are settled before the plan decides (a filled target is hit, not crossed), and the book is synced after
it acts. The lock is taken only when an action is due or the targets need it (targets_sync.due): that step is a
`plan_step` intent (#36 step 2, gateway.py), recording what the pass saw and decided again under the lock. A flat
symbol's targets are pulled in a `pull_targets` intent (step 2b), before reconcile journals the trade."""

from __future__ import annotations

import logging

from . import targets, targets_sync
from .gateway import handler
from .guardian_loop import send_safe
from .intents import Intent
from .journal import Journal
from .manager import excursions, plan_actions
from .symlock import SymbolBusy

log = logging.getLogger("shortbot.guardian")


class PlanChecks:
    """Guardian mixin: needs app, j, n, gw."""

    async def _settle_flat(self, live: list[dict]) -> None:
        """Each symbol with live target rows and no position (targets_sync.flat_symbols): its targets pulled in an
        intent. A busy symbol waits for the next pass (a session's close pulls them too)."""
        for sym in targets_sync.flat_symbols(self.app.trader, live):
            try:
                await self.gw.submit(Intent("pull_targets", {"symbol": sym}, sym))
            except SymbolBusy:
                continue

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
        self.j.post(Journal.update_trade, trade["id"], mfe_pct=round(ex["mfe_pct"], 3),
                    mae_pct=round(ex["mae_pct"], 3), low_since_entry=ex["low_since_entry"])
        trade = {**trade, **ex}
        tr, sym = self.app.trader, trade["symbol"]
        acts = plan_actions(trade, p)
        due = await self._targets(targets_sync.due(tr, trade, p), sym, default=True)
        if all(a.kind == "alert" for a in acts) and not due:
            await self._apply_plan(trade, p, acts)
            return
        seen = {"contracts": p["contracts"], "stop": p.get("exchange_stop"), "targets_due": due,
                "actions": [a.kind for a in acts if a.kind != "alert"]}
        await self.gw.submit(Intent("plan_step", {"symbol": sym, "trade_id": trade["id"], "seen": seen}, sym))

    async def _apply_plan(self, trade: dict, p: dict, acts: list) -> list[dict]:
        """Run the plan's actions; returns what each order action did."""
        tr, sym = self.app.trader, trade["symbol"]
        cur, done = p["contracts"], []
        for a in acts:
            if a.kind == "alert":
                await self.n.send(f"plan:{a.key or a.kind}", a.reason, sym, cooldown_s=a.cooldown_s, urgent=a.urgent)
            elif a.kind == "move_stop":
                r = await tr.move_stop(sym, a.price, f"plan/{a.reason}")
                done.append({"kind": a.kind, "reason": a.reason, "result": r})
                if r.get("noop"):          # rounds to the stop already in place
                    continue
                await self.n.send("plan_stop_moved" if r.get("ok") else "plan_stop_move_failed",
                                  f"{a.reason} -> {a.price:.6g}: {r}", sym, cooldown_s=0)
            elif a.kind == "target_done":
                await targets.mark_hit(self.j, trade["id"], a.target_price)   # re-read: a fill may have added one
                done.append({"kind": a.kind, "reason": a.reason})
                await self.n.send("plan_target_done", a.reason, sym, cooldown_s=0)
            elif a.kind == "close":
                if a.cum is not None:   # a target's market fallback: idempotent, whatever its limit filled meanwhile
                    r = await tr.close(sym, f"plan/{a.reason}", fraction=a.cum, of_original=True)
                else:
                    r = await tr.close(sym, f"plan/{a.reason}", fraction=a.fraction or 1.0, expect_contracts=cur)
                done.append({"kind": a.kind, "reason": a.reason, "result": r})
                cur = r.get("remaining", cur)   # T1 and T2 in one pass: the second sees what the first left
                if a.target_price is not None and (r.get("ok") or r.get("noop")):
                    await targets.mark_hit(self.j, trade["id"], a.target_price)
                await self.n.send("plan_close", f"{a.reason}: {r}", sym, cooldown_s=0)
                if (a.fraction or 1.0) >= 1.0:
                    return done
        return done


@handler("pull_targets")
async def pull_targets(g, symbol: str) -> bool:
    """PullTargets (GATEWAY.md §4.2): a flat symbol's targets cancelled and their fills journalled, the position
    re-read under the lock (targets_sync.pull_flat). Strict (journal first): reduce-only orders can't fill at flat."""
    return await targets_sync.pull_flat(g.app.trader, symbol)


@handler("plan_step")
async def plan_step(g, symbol: str, trade_id: int, seen: dict) -> dict:
    """PlanStep (GATEWAY.md §4.2): the plan's due actions and the target sync, under the symbol lock, decided again on
    what is true NOW (a session may have closed, added or moved the stop since the pass saw `seen`)."""
    tr = g.app.trader
    async with tr.symbol_lock(symbol, "guardian plan"):
        pos = await tr.position(symbol)
        if not pos:
            return {"ok": True, "skipped": "flat"}
        await g._targets(targets_sync.settle(tr, symbol), symbol)   # a filled target is hit, not crossed
        fresh = g.j.get_trade(trade_id)
        if not fresh or fresh["status"] != "open":
            return {"ok": True, "skipped": "trade not open"}
        stop = min((s["trigger"] for s in await tr.stop_orders(symbol)), default=None)
        p = {"mark": float(pos["markPrice"]), "contracts": float(pos["contracts"]), "exchange_stop": stop}
        trade = {**fresh, **excursions(fresh, p["mark"])}
        done = await g._apply_plan(trade, p, plan_actions(trade, p))
        await g._targets(targets_sync.sync(tr, symbol, "guardian pass"), symbol)
    return {"ok": True, "trade_id": trade_id, "done": done}
