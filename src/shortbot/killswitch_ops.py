"""Kill-switch actions (state and gate: killswitch.py): halt = trip + withdraw the resting entries; flatten = HALTED +
market-close every short reduce-only. Neither ever cancels a protective stop on an open position. The halt_trading /
flatten_all tools and the `shortbot kill-switch` / `shortbot flatten` subcommands are declared in cmd_kill.
"""

from __future__ import annotations

from typing import Any

from . import audit
from . import killswitch as ks
from .entries import EntryDesk
from .entries_cancel import cancel_resting
from .intents import Intent
from .killswitch import HALTED

CLI_BY = "shortbot CLI (agent or human)"   # the allowlisted CLI can't tell who typed it; shortbot-admin can


async def halt(tr, state: str, by: str, reason: str, evidence: dict | None = None,
               cause: str = "manual") -> dict[str, Any]:
    """Trip to `state`; when the desk is (now) HALTED, withdraw the resting entries right away."""
    res = await ks.trip(tr.j, tr.ex.settings, state, by, reason, evidence, cause)
    if ks.read(tr.j)["state"] == HALTED:
        try:
            res["resting"] = await withdraw_resting(EntryDesk(tr), f"kill switch HALTED: {reason}")
        except Exception as e:  # noqa: BLE001 - the guardian's sweep retries
            res["resting"] = {"error": f"{type(e).__name__}: {e}"}
            await ks.notify(tr.j, tr.ex.settings, "kill_switch_cancel_failed", f"HALTED, but resting entries were "
                            f"not withdrawn ({e}); the guardian retries every cycle", urgent=True)
    return res


async def withdraw_resting(desk, reason: str) -> dict[str, list[int]]:
    """Every resting entry withdrawn (cancel_resting): inside the running intent (a command's: halt_trading,
    flatten_all, kill-switch; an opening gate's watchdog), else as a withdraw_entries intent of its own (the watchdog
    from a read: trading_state, the brief), actor the reader's (#36 step 2b)."""
    if audit.intent_id() is not None:
        return await cancel_resting(desk, reason)
    return await desk.gw.submit(Intent("withdraw_entries", {"reason": reason}))


async def flatten(tr, by: str, reason: str, lesson: str = "") -> dict[str, Any]:
    """Flatten all: HALTED first (nothing new opens meanwhile), resting entries withdrawn, then every short closed
    through Trader.close (reduce-only market buy, journal + exchange-truth PnL). The state stays HALTED."""
    if not (reason or "").strip():
        raise ValueError("flatten needs a reason")
    why = f"flatten all: {reason.strip()}"
    st = await halt(tr, HALTED, by, why, cause="flatten")
    closed, failed = [], []
    for p in await tr.raw_positions():
        sym = p["symbol"]
        if p.get("side") != "short":
            failed.append({"symbol": sym, "error": f"{p.get('side')} position: not a short, close it manually"})
            continue
        try:
            r = await tr.close(sym, why, lesson)
        except Exception as e:  # noqa: BLE001 - one failure must not stop the others
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        (closed if r.get("ok") else failed).append({**r, "symbol": sym})
    left = [p["symbol"] for p in await tr.raw_positions()]
    if failed or left:
        await ks.notify(tr.j, tr.ex.settings, "flatten_incomplete", f"still open: {left}; failures: {failed}. "
                        "Stops are untouched; close these by hand.", urgent=True)
    tr.j.log_decision("flatten_all", why, None, snapshot={"by": by, "closed": closed, "failed": failed,
                                                         "still_open": left, "resting": st.get("resting")})
    return {"ok": not failed and not left, "state": ks.read(tr.j)["state"], "closed": closed, "failed": failed,
            "still_open": left, "resting": st.get("resting")}
