"""Terminal access: `shortbot scan`, `shortbot snapshot QNT`, `shortbot positions`, `shortbot brief`...
Arguments live in cli_args.py, `brief` (what the SessionStart hook prints) in cli_brief.py; dispatch is here.
Audit (#24): each command runs as actor cli:<cmd>; one not in READS leaves a `call` decision row (audit.py).
Every state-changing MCP tool has a CLI form (stale_guard.CLI): a stale MCP server's refusal names it (#68)."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging

from . import audit, commands, killswitch_ops, weight
from .app import close_app, get_app
from .cli_args import build_parser
from .entries import EntryDesk
from .positioning import PositionOps
from .redact import install_log_redaction

# Commands that leave no `call` row: they change nothing (or journal their own row, like `log`). Any other is logged.
READS = frozenset()



async def _run(args: argparse.Namespace) -> object:
    if args.cmd in commands._BY_CLI:   # migrated (#36 step 1)
        cmd = commands.by_cli(args.cmd)
        try:
            return await cmd.body(**cmd.cli_kwargs(args))
        finally:
            await close_app()
    app = get_app()
    try:
        match args.cmd:
            case "learn":
                return {"ok": True, "learning_id": app.journal.add_learning(
                    args.statement, args.evidence, args.setup, args.status, args.confidence,
                    args.provenance or ["cli"], kind=args.kind, n=args.n, days=args.days, sample=args.sample,
                    review_by=args.review_by)}
            # order actions: same code path as the MCP tools (risk limits, vetoes, plans, journal, alerts)
            case "open":
                return await app.trader.open_short(args.symbol, args.stop, args.setup, args.thesis,
                                                   json.loads(args.plan) if args.plan else None, args.notional,
                                                   args.leverage, args.veto_override, args.conviction,
                                                   args.budget_override)
            case "close":
                return await app.trader.close(args.symbol, args.reason, args.lesson, args.fraction,
                                              args.expect_contracts, args.of_original)
            case "flatten":
                return await killswitch_ops.run_cli(app, args)
            case "move-stop":
                return await app.trader.move_stop(args.symbol, args.price, args.reason)
            # MCP tools with logic of their own: the tool's body runs here (one copy of the code)
            case "update-plan":
                return await tool_body("mcp_trade", "update_plan")(args.symbol, json.loads(args.changes), args.reason)
            case "set-params":
                return await tool_body("mcp_research", "set_risk_params")(dict(args.changes), args.reason,
                                                                          args.experiment)
            case "reset-params":
                return await tool_body("mcp_research", "reset_risk_params")(args.reason)
            case "update-learning":
                return await tool_body("mcp_research", "update_learning")(
                    args.learning_id, status=args.status, evidence=args.evidence, confidence=args.confidence,
                    provenance=args.provenance, kind=args.kind, n=args.n, days=args.days, sample=args.sample,
                    review_by=args.review_by)
            case "start-session":
                return await tool_body("mcp_research", "start_session")(args.notes)
            case "widen-stop":
                return await PositionOps(app.trader).widen_stop(args.symbol, args.price, args.reason)
            case "add":
                return await PositionOps(app.trader).add_to_winner(args.symbol, args.notional, args.stop, args.reason)
            case "propose":
                return await EntryDesk(app.trader).propose_group(json.loads(args.proposals), args.ttl_hours,
                                                                 not args.no_prearm, args.warmup_override)
            case "cancel-group":
                return await EntryDesk(app.trader).cancel_group(args.group_id, args.reason)
            case "amend":
                return await EntryDesk(app.trader).amend(args.entry_id, args.price, args.reason)
            case "unlock-add":
                from .market.events import EventStore
                uid = EventStore(app.settings.db_path).add_unlock(args.base, args.date, args.pct, args.recipient,
                                                                  args.source, args.notes)
                return {"ok": True, "unlock_id": uid}
    finally:
        await close_app()


def tool_body(module: str, name: str):
    """The function under an MCP tool's wrapper, for a command whose tool holds logic of its own: one copy of the code
    (a fix to the tool reaches the CLI), run as cli:<cmd>. Importing the MCP modules builds the MCPServer, whose
    constructor runs logging.basicConfig (stderr, INFO); the CLI keeps its own root logger (#66), so it is restored."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        mod = importlib.import_module(f"{__package__}.{module}")
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
    return getattr(mod, name)


# Exchange weight classes (#70, weight.py), as for the MCP tools: order / close / stop / amend paths never wait for
# budget, scans and studies yield to them, every other command is a desk read (untagged).
PROTECTIVE_CMDS = frozenset({"open", "close", "move-stop", "widen-stop", "add", "propose", "cancel-group", "amend",
                             "update-plan", "set-params"})
RESEARCH_CMDS = frozenset()


async def _audited(args: argparse.Namespace) -> object:
    cmd = commands._BY_CLI.get(args.cmd)
    cls = cmd.weight if cmd else weight.pick(args.cmd, PROTECTIVE_CMDS, RESEARCH_CMDS)
    with audit.acting(f"cli:{args.cmd}"), weight.tagged(cls, wait_s=weight.ENTRY_WAIT_S):
        if (not cmd.write) if cmd else args.cmd in READS:
            return await _run(args)
        j = get_app().journal   # the singleton _run uses; close_app leaves its journal open
        return await audit.called(lambda: j, args.cmd, {k: v for k, v in vars(args).items() if k != "cmd"},
                                  _run(args))


def main() -> None:
    install_log_redaction()   # no handlers here: records go through logging.lastResort (#66)
    args = build_parser().parse_args()
    cmd = commands._BY_CLI.get(args.cmd)
    if cmd and cmd.raw:   # backup, hook-session: no exchange app, no audit stamp
        raise SystemExit(cmd.body(args))
    audit.set_process("cli")
    out = asyncio.run(_audited(args))
    print(out if isinstance(out, str) else json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
