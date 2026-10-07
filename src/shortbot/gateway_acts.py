"""The guardian's side inside the order gateway (#36 step 6, docs/GATEWAY.md §5): the context its position-pass intents
run with there (GuardianActs), and the start-up reconciliation the gateway runs before it listens (start_up).

start_up, under the reconcile lease (symlock.reconciling: one reconciler at a time; every deploy restarts the guardian
and the gateway together, and the guardian's positions pass and entry_reconcile hold the same lease), bounded by
STARTUP_S, for the guardian (origin.guardian: the .env baseline, its alerts), actor gateway:startup:
  1-2. the open intents of processes that are gone end (intents.sweep: received -> expired, running -> interrupted),
       each alerted intent_interrupted;
  3. a flat symbol's targets pulled (fills first, #65), entry_intents.reconcile, reconcile.reconcile (closed trades,
     each alerted trade_closed; `opening` rows after their grace), a stop restored on every short (bridges swept),
     the HALTED sweep if HALTED;
  4. the interrupted and unknown intents settled by their client ids (resolver.resolve: never re-sent).
Its writes are intents of its own (gateway.CTX: GuardianActs or this process's EntryDesk). A guardian pass holding the
lease past LEASE_WAIT_S does the same work: the start-up leaves it to that pass. A failure or the time limit is
alerted (gateway_startup_failed) and the gateway listens anyway: the guardian's passes reconcile every 60 s."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from . import audit, entry_gates, entry_intents, intents, origin, reconcile, resolver, targets_sync, weight, write_guard
from . import killswitch as ks
from .guardian_loop import send_safe
from .guardian_plan import PlanChecks
from .guardian_positions import FALLBACK_STOP_PCT
from .intents import Intent
from .symlock import ReconcileBusy, SymbolBusy

if TYPE_CHECKING:
    from .app import App
    from .gateway import LocalGateway
    from .notify import Notifier

log = logging.getLogger("shortbot.gateway")
STARTUP_S = 90.0
LEASE_WAIT_S = 60.0   # a guardian pass is seconds long; one that holds it longer is reconciling already


class GuardianActs(PlanChecks):
    """The ctx of restore_stop, force_close, cap_stop, pull_targets and plan_step when the gateway runs them
    (gateway.CTX "guardian"): what those handlers use of the Guardian. The app is the gateway's own (one Trader); the
    intent runs for the guardian (origin.guardian), so the Trader sees the .env baseline and alerts through `n`."""

    def __init__(self, app: App, n: Notifier):
        self.app, self.j, self.n = app, app.journal, n


async def start_up(acts: GuardianActs, desk_gw: LocalGateway, gw: LocalGateway) -> dict[str, Any]:
    """The module doc's start-up; what it did, or why it didn't."""
    tr, n = acts.app.trader, acts.n
    if not tr.ex.trade:
        return {"skipped": "no trade keys"}
    try:
        async with asyncio.timeout(STARTUP_S), tr.symlocks.reconciling("gateway start-up", LEASE_WAIT_S):
            # protective (weight.py): the stop restores and reconciliation never wait for weight budget
            with audit.acting("gateway:startup"), origin.guardian(n.as_trader_alert), write_guard.writing("gateway"), \
                    weight.tagged(weight.PROTECTIVE):
                done = await _steps(acts, desk_gw.ctx, gw)
    except ReconcileBusy as e:
        log.info("start-up reconciliation left to the guardian: %s", e)
        return {"skipped": str(e)}
    except Exception as e:  # noqa: BLE001 (incl. TimeoutError) - it listens anyway: the guardian reconciles too
        log.exception("start-up reconciliation failed")
        await send_safe(n, "gateway_startup_failed", f"The order gateway's start-up reconciliation failed "
                        f"({type(e).__name__}: {e}); it serves anyway, and the guardian's passes reconcile every 60 s.",
                        cooldown_s=3600)
        return {"failed": f"{type(e).__name__}: {e}"}
    log.info("start-up reconciliation: %s", done)
    return done


async def _steps(acts: GuardianActs, desk: Any, gw: LocalGateway) -> dict[str, Any]:
    tr, j, n = acts.app.trader, acts.j, acts.n
    swept = await j.write(intents.sweep)
    for r in swept:
        msg, urgent = resolver.swept(r)
        await send_safe(n, "intent_interrupted", msg, r["symbol"], urgent=urgent, cooldown_s=0)
    live = await tr.raw_positions()
    for sym in targets_sync.flat_symbols(tr, live):
        await _each(gw.submit(Intent("pull_targets", {"symbol": sym}, sym)), sym)
    await entry_intents.reconcile(desk)
    closed = await reconcile.reconcile(tr, live)
    await reconcile.announce(n, closed)
    restored = 0
    for p in await tr.positions():
        sym = p["symbol"]
        if p["side"] != "short" or p.get("error"):
            continue   # a long or an unreadable one: the guardian's pass alerts it
        res = await _each(gw.submit(Intent("restore_stop", {"symbol": sym, "fallback_pct": FALLBACK_STOP_PCT}, sym)),
                          sym)
        if isinstance(res, dict) and res:
            restored += 1
            swept_bridges = res.get("action") == "bridges_swept"
            await n.send("bridge_swept" if swept_bridges else "stop_missing", ("leftover stop-swap bridge(s) cancelled"
                         if swept_bridges else "No closePosition stop found at the gateway's start") + f": {res}", sym,
                         urgent=not swept_bridges, cooldown_s=0 if swept_bridges else 300)
    if ks.read(j)["state"] == ks.HALTED:
        await entry_gates.halt_sweep(desk, n)
    await resolver.resolve(tr)
    return {"swept": len(swept), "closed": len(closed), "restored": restored}


async def _each(step: Any, sym: str) -> Any:
    """One symbol's step: a busy symbol (a session acts on it) or a failure never costs the others."""
    try:
        return await step
    except SymbolBusy as e:
        log.info("start-up: %s skipped: %s", sym, e)
    except Exception:  # noqa: BLE001 - the guardian's next pass retries it
        log.exception("start-up: %s failed", sym)
    return None
