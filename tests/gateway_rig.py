"""A real order gateway for tests (#36 step 4): served in the test's own event loop, its socket in a short directory
under /tmp (a pytest tmp path on macOS is longer than the 104 bytes sun_path holds). #80's net guard blocks only
non-loopback connections: a Unix socket is allowed."""

from __future__ import annotations

import shutil
import tempfile
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncIterator
from unittest import mock

from shortbot import audit, gateway_wire
from shortbot.gateway_server import GatewayServer


def short_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="sbgw-", dir="/tmp"))


@asynccontextmanager
async def served(journal: Any, ctx: Any = None, mode: str = "demo") -> AsyncIterator[GatewayServer]:
    """A gateway serving `journal`; while it runs, every client's socket path is its."""
    d = short_dir()
    srv = GatewayServer(journal, mode, d / "run" / f"gateway-{mode}.sock", ctx)
    await srv.start()
    try:
        with mock.patch.object(gateway_wire, "socket_path", lambda settings: srv.path):
            yield srv
    finally:
        await srv.close(1.0)
        shutil.rmtree(d, ignore_errors=True)


def as_session(monkeypatch, kind: str = "mcp", claude_session: str | None = "sess-1") -> None:
    """This process as a session (mcp, cli, web), or another kind."""
    monkeypatch.setattr(audit, "_proc", {"kind": kind, "claude_session": claude_session})


def flag_on(settings: Any) -> Any:
    return replace(settings, gateway=True)
