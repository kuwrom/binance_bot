"""Terminal access: `shortbot scan`, `shortbot snapshot QNT`, `shortbot positions`, `shortbot brief`...
Every subcommand is a commands.py declaration, the same one its MCP tool comes from (one body for both surfaces):
the parser is generated from the registry and each command runs its declared body.
Audit (#24): each command runs as actor cli:<cmd>, under its #70 weight class; a write leaves a `call` decision row
(audit.py). Every state-changing MCP tool has a CLI form: a stale MCP server's refusal names it (#68)."""

from __future__ import annotations

import argparse
import asyncio
import json

from . import audit, commands, weight
from .app import close_app, get_app
from .commands import build_parser
from .redact import install_log_redaction


async def _run(args: argparse.Namespace) -> object:
    cmd = commands.by_cli(args.cmd)
    try:
        return await cmd.body(**cmd.cli_kwargs(args))
    finally:
        await close_app()


async def _audited(args: argparse.Namespace) -> object:
    cmd = commands.by_cli(args.cmd)
    with audit.acting(f"cli:{args.cmd}"), weight.tagged(cmd.weight, wait_s=weight.ENTRY_WAIT_S):
        if not cmd.write:
            return await _run(args)
        j = get_app().journal   # the singleton the body uses; close_app leaves its journal open
        return await audit.called(lambda: j, args.cmd, {k: v for k, v in vars(args).items() if k != "cmd"},
                                  _run(args))


def main() -> None:
    install_log_redaction()   # no handlers here: records go through logging.lastResort (#66)
    args = build_parser().parse_args()
    cmd = commands.by_cli(args.cmd)
    if cmd.raw:   # backup, hook-session: no exchange app, no audit stamp
        raise SystemExit(cmd.body(args))
    audit.set_process("cli")
    out = asyncio.run(_audited(args))
    print(out if isinstance(out, str) else json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
