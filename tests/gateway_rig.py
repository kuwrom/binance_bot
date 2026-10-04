"""A real order gateway for tests (#36 step 4): served in the test's own event loop, its socket in a short directory
under /tmp (a pytest tmp path on macOS is longer than the 104 bytes sun_path holds). #80's net guard blocks only
non-loopback connections: a Unix socket is allowed."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from shortbot import audit, gateway_wire
from shortbot.gateway_server import GatewayServer


def short_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="sbgw-", dir="/tmp"))


@asynccontextmanager
async def served(journal: Any, ctx: Any = None, mode: str = "demo", acts: Any = None) -> AsyncIterator[GatewayServer]:
    """A gateway serving `journal`; while it runs, every client's socket path is its. acts: its GuardianActs."""
    d = short_dir()
    srv = GatewayServer(journal, mode, d / "run" / f"gateway-{mode}.sock", ctx, acts)
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


@pytest.fixture(params=["local", "gateway"])
async def guardian_transport(request, monkeypatch):
    """The guardian suites twice (#36 step 6): "local", as before; "gateway", every Guardian the test builds sends its
    intents over a real socket to a gateway serving its journal and Trader, with the gateway's own EntryDesk and
    GuardianActs. Nothing may break glass there: a route that silently fell back would pass otherwise."""
    if request.param == "local":
        yield "local"
        return
    from shortbot.entries import EntryDesk
    from shortbot.gateway import LocalGateway
    from shortbot.gateway_acts import GuardianActs
    from shortbot.gateway_guardian import GuardianClient, Link
    from shortbot.guardian import Guardian
    from shortbot.journal import Journal
    from shortbot.notify import Notifier
    built = []
    async with served(Journal(":memory:")) as srv:
        real = Guardian.__init__

        def init(self, app=None):
            real(self, app)
            on = replace(self.app.settings, gateway=True, gateway_guardian=True)
            srv.j, srv.acts = self.j, GuardianActs(self.app, Notifier(on, self.j))
            srv.gw = LocalGateway(lambda j=self.j: j, ctx=EntryDesk(self.app.trader))
            srv.gw_guardian = LocalGateway(lambda j=self.j: j, ctx=srv.acts)
            self.gateway_link = Link()
            self.gw = GuardianClient(lambda: self.j, lambda: on, self, self.n, self.gateway_link)
            self.entries.gw = GuardianClient(lambda: self.j, lambda: on, self.entries, self.n, self.gateway_link)
            built.append(self)
        monkeypatch.setattr(Guardian, "__init__", init)
        yield "gateway"
    for g in built:
        assert not g.j._rows("SELECT id FROM intents WHERE actor LIKE 'breakglass:%'"), "a guardian broke glass"


def transport(request) -> str:
    """This test's guardian_transport ("local" outside the parametrized suites)."""
    return getattr(request.node, "callspec", None) and request.node.callspec.params.get("guardian_transport") or "local"
