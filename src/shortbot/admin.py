"""`shortbot-admin`: the HUMAN side of the kill switch (killswitch.py). Loosening the state lives in this separate
console script because the agent's allowlist, `Bash(.venv/bin/shortbot *)`, auto-approves every `shortbot`
subcommand, so a `shortbot` subcommand can't tell the human from the agent.

Speed bumps and a tripwire, not a security boundary (an agent with a shell can still write the SQLite file):
- a separate executable: the space before `*` is part of a Claude Code rule, so `.venv/bin/shortbot *` doesn't match
  `.venv/bin/shortbot-admin`; the owner also denies `Bash(*shortbot-admin*)` outright (deny holds in every mode);
- a real terminal: the confirmation code is read from /dev/tty, which Claude Code's Bash tool doesn't have, and
  it refuses to run when CLAUDECODE is set;
- every change is journalled (`kill_switch_clear`) and alerted, so a clear nobody typed gets noticed.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import secrets
from typing import Any

from . import killswitch as ks
from .config import Settings
from .journal import Journal


def _tty():
    return open("/dev/tty", "r+", encoding="utf-8")   # OSError (ENXIO) without a controlling terminal


def confirm(target: str, st: dict[str, Any]) -> str | None:
    """None when a human at a terminal typed the code shown; else why not."""
    if os.environ.get("CLAUDECODE"):
        return "refused under Claude Code: run shortbot-admin in your own terminal"
    try:
        tty = _tty()
    except OSError as e:
        return f"needs an interactive terminal (/dev/tty: {e})"
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(4))
    with tty:
        tty.write(f"kill switch {st['state']} since {st.get('at')} by {st.get('by')}: {st.get('reason')}\n"
                  f"set it to {target}? type {code} to confirm: ")
        tty.flush()
        typed = tty.readline().strip().upper()
    return None if typed == code else "confirmation code did not match: nothing changed"


async def run(args: argparse.Namespace, settings: Settings | None = None,
              journal: Journal | None = None) -> dict[str, Any]:
    s = settings or Settings.from_env()
    j = journal or Journal(s.db_path)
    if args.cmd == "status":
        return ks.status(j)
    st = ks.read(j)
    if ks.LEVEL[args.to] >= st["level"]:
        return {"ok": False, "error": f"the desk is {st['state']}: {args.to} is not a loosening (to trip it, use "
                                      "`shortbot kill-switch`)"}
    why = confirm(args.to, st)
    if why:
        return {"ok": False, "error": why}
    return await ks.clear(j, s, args.to, f"human:{getpass.getuser()} (tty)", args.reason)


def main() -> None:
    p = argparse.ArgumentParser(prog="shortbot-admin", description="Human-only kill-switch controls. Run it in "
                                "your own terminal, not through Claude.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="state, who set it and why, guardian heartbeat")
    c = sub.add_parser("clear", help="loosen the kill switch (asks for a code on this terminal)")
    c.add_argument("--to", choices=(ks.ACTIVE, ks.REDUCING), default=ks.ACTIVE)
    c.add_argument("--reason", required=True, help="what you reviewed and why trading may resume")
    out = asyncio.run(run(p.parse_args()))
    print(json.dumps(out, indent=2, default=str))
    if out.get("ok") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
