"""Kill-switch MCP tools (killswitch.py). The agent may read and TRIP the switch; nothing here loosens it (human-only:
`shortbot-admin clear` in a real terminal). Registered on the shared server by importing this module from
mcp_server."""

from __future__ import annotations

from typing import Any

from . import killswitch, killswitch_ops
from .app import get_app
from .mcp_server import tool

MCP_BY = "agent (MCP)"


@tool
async def trading_state() -> dict[str, Any]:
    """Kill switch: ACTIVE | REDUCING (only closes, partial closes, stop tightening, cancelling entries) | HALTED
    (REDUCING + resting limit entries withdrawn), who set it, why, when, and the guardian heartbeat. A stale
    guardian trips HALTED here. Only a human clears it; when it is not ACTIVE, manage and reduce, don't work around it."""
    app = get_app()
    await killswitch.watch_guardian(app.trader)
    return killswitch.status(app.journal)


@tool
async def halt_trading(reason: str, state: str = "HALTED") -> dict[str, Any]:
    """Trip the kill switch (always allowed: it only removes risk). REDUCING blocks new entries, adds, risk-raising
    amends, stop widening and ladder opt-ins; HALTED also withdraws every resting limit entry (a pre-armed stop only
    with a limit confirmed unfilled). Protective stops on open positions are never touched. Escalate-only; tell the
    human why."""
    return await killswitch_ops.halt(get_app().trader, state.upper(), MCP_BY, reason)


@tool
async def flatten_all(reason: str, lesson: str = "") -> dict[str, Any]:
    """Emergency: trip HALTED, withdraw resting entries, then buy back EVERY short at market (reduce-only), each
    journalled with exchange-truth PnL and this lesson. The state stays HALTED."""
    return await killswitch_ops.flatten(get_app().trader, MCP_BY, reason, lesson)
