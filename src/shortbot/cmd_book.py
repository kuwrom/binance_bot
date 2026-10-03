"""Book-planning command (commands.py: an MCP tool and a `shortbot` subcommand). Read-only: it proposes, never
places."""

from __future__ import annotations

from typing import Any

from .app import get_app
from .commands import Arg, command
from .weight import RESEARCH


@command(cli="book-plan", weight=RESEARCH, help="which scan candidates to work next, given the open + resting book",
         args=(Arg("top", "--top"), Arg("min_change", "--min-change"),
               Arg("direction", "--direction", choices=("gainers", "losers", "both", "now"))))
async def book_plan(top: int = 6, min_change: float = 5.0, direction: str = "both") -> dict[str, Any]:
    """Which scan candidates to work next given the open + resting book (learning #30, allocator.py): free slots
    (max_positions minus positions and resting groups), read-approved shorts ranked by registry evidence then
    score, slots 4-5 only for event setups (delisting / listing fade / unlock), half size for a repeated
    setup+sector. Returns add/skip with reasons and size_mult; place orders with propose_entries (budget applies)."""
    from .allocator import book_plan as plan
    return await plan(get_app(), top, min_change, direction)
