"""Kill-switch commands (killswitch.py; commands.py: MCP tools and `shortbot` subcommands). The agent may read and TRIP
the switch; nothing here loosens it (human-only: `shortbot-admin clear` in a real terminal)."""

from __future__ import annotations

from typing import Any

from . import audit, killswitch, killswitch_ops
from .app import get_app
from .commands import Arg, command
from .killswitch import HALTED, REDUCING
from .weight import PROTECTIVE

MCP_BY = "agent (MCP)"


@command()
async def trading_state() -> dict[str, Any]:
    """Kill switch: ACTIVE | REDUCING (only closes, partial closes, stop tightening, cancelling entries) | HALTED
    (REDUCING + resting limit entries withdrawn), who set it, why, when, and the guardian heartbeat. A stale
    guardian trips HALTED here. Only a human clears it; when it is not ACTIVE, manage and reduce, don't work around it."""
    app = get_app()
    await killswitch.watch_guardian(app.trader)
    return killswitch.status(app.journal)


@command(mcp=False, cli="kill-switch", write=True, orders=True, weight=PROTECTIVE, help="show the kill switch, or trip "
         "it to REDUCING | HALTED (clearing is human-only: shortbot-admin)", args=(
    Arg("state", nargs="?", choices=(REDUCING, HALTED)), Arg("reason", "--reason", "required to trip")))
async def kill_switch(state: str | None = None, reason: str | None = None) -> dict[str, Any]:
    """trading_state without a state; halt_trading with one (a reason required). Protective: a halt withdraws the
    resting entries (#70)."""
    if not state:
        return await trading_state()
    if not (reason or "").strip():
        return {"ok": False, "error": "--reason is required to trip the kill switch"}
    return await killswitch_ops.halt(get_app().trader, state, killswitch_ops.CLI_BY, reason)


@command(write=True, orders=True, weight=PROTECTIVE, stale_exempt=True, hint="kill-switch HALTED --reason REASON")
async def halt_trading(reason: str, state: str = "HALTED") -> dict[str, Any]:
    """Trip the kill switch (always allowed: it only removes risk). REDUCING blocks new entries, adds, risk-raising
    amends, stop widening and ladder opt-ins; HALTED also withdraws every resting limit entry (a pre-armed stop only
    with a limit confirmed unfilled). Protective stops on open positions are never touched. Escalate-only; tell the
    human why."""
    return await killswitch_ops.halt(get_app().trader, state.upper(), MCP_BY, reason)


@command(cli="flatten", write=True, orders=True, weight=PROTECTIVE, help="HALTED + market-close every short "
         "reduce-only; the state stays HALTED", args=(Arg("reason", "--reason"), Arg("lesson", "--lesson")))
async def flatten_all(reason: str, lesson: str = "") -> dict[str, Any]:
    """Emergency: trip HALTED, withdraw resting entries, then buy back EVERY short at market (reduce-only), each
    journalled with exchange-truth PnL and this lesson. The state stays HALTED."""
    return await killswitch_ops.flatten(get_app().trader, _by(), reason, lesson)


def _by() -> str:
    """Who flattened, as the kill-switch row shows it: the agent through MCP, or the CLI (agent or human)."""
    return MCP_BY if audit.actor().startswith("mcp:") else killswitch_ops.CLI_BY
