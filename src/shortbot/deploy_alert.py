"""`python -m shortbot.deploy_alert KIND MESSAGE [--urgent]`: an alert from deploy/autodeploy.sh (deploy/DEPLOY.md §10).

The same path as every desk alert: an alerts row in the journal, so the brief and the web console list it, and Telegram
when it is configured. No cooldown: the script sends each problem once (it remembers the commit it alerted about).
Exit 0 when delivered (or logged, without Telegram), 1 otherwise."""

from __future__ import annotations

import argparse
import asyncio

from .config import Settings
from .journal import Journal
from .notify import Notifier
from .redact import install_log_redaction

DRAIN_S = 30.0   # the alert row lands before exit, also behind another process's write lock (#79)


async def send(kind: str, message: str, urgent: bool = False) -> bool:
    s = Settings.from_env()
    j = Journal(s.db_path, s.mode)
    try:
        return await Notifier(s, j).send(kind, message, cooldown_s=0, urgent=urgent)
    finally:
        await j.w.drain(DRAIN_S)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m shortbot.deploy_alert", description=__doc__)
    p.add_argument("kind")
    p.add_argument("message")
    p.add_argument("--urgent", action="store_true")
    a = p.parse_args(argv)
    install_log_redaction()
    raise SystemExit(0 if asyncio.run(send(a.kind, a.message, a.urgent)) else 1)


if __name__ == "__main__":
    main()
