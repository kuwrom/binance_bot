"""Exchange writes leave a process only where they may (#36 step 7, docs/GATEWAY.md §8). GuardedUSDM.fetch asks
`refusal` before every request, so a write that would bypass the order gateway raises before it is sent.

- Each process has a role, set as its app is built (app.get_app -> configure):
  - `gateway`: shortbot-gateway;
  - `client`: an MCP server, a CLI call or the web console while SHORTBOT_GATEWAY sends the sessions' writes to the
    gateway, or the guardian while it sends its own (Settings.gateway / gateway_guardian);
  - `local`: anything else (the flag off, tools, tests): in-process writes, as before step 5.
- A non-GET request passes when:
  - its path is /fapi/v1/listenKey (POST, PUT, DELETE): the user-data stream's key changes no order or position, and
    the guardian keeps its stream until step 9 (§1.2);
  - it runs inside `writing(why)`: "gateway" (the gateway's runs and its start-up), "breakglass"
    (gateway_client._without: the gateway can't take a protective intent), "probe" (probes.run, after its demo-only
    refusals: raw cases that record Binance's own answer, §1.2);
  - the role is local.
- Anything else raises WriteRefused, an errors.Refused (nothing was sent): a stray create_order from an MCP path, an
  exchange write inside a journal-only command, a guardian write outside break-glass, a gateway write outside a run."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit

from .errors import Refused

CLIENTS = ("mcp", "cli", "web")
WHY = ("gateway", "breakglass", "probe")
LISTEN_KEY = "/fapi/v1/listenKey"
_role = "local"
_CTX: ContextVar[str | None] = ContextVar("shortbot_writing", default=None)


class WriteRefused(Refused):
    """An exchange write outside the order gateway: refused here, nothing sent."""


def configure(process_kind: str | None, settings: Any) -> str:
    """This process's role (module doc), from its kind (audit.process_kind) and its settings."""
    global _role
    if process_kind == "gateway":
        _role = "gateway"
    elif (process_kind in CLIENTS and settings.gateway) or (process_kind == "guardian" and settings.gateway_guardian):
        _role = "client"
    else:
        _role = "local"
    return _role


def role() -> str:
    return _role


@contextmanager
def writing(why: str) -> Iterator[None]:
    """Exchange writes may leave this process inside (module doc)."""
    if why not in WHY:
        raise ValueError(f"writing({why!r}): one of {WHY}")
    token = _CTX.set(why)
    try:
        yield
    finally:
        _CTX.reset(token)


def refusal(method: str, url: str) -> str | None:
    """Why this request may not leave the process (None: it may)."""
    if method.upper() == "GET" or _role == "local" or _CTX.get() is not None:
        return None
    path = urlsplit(url).path
    if path == LISTEN_KEY:
        return None
    where = "the gateway, outside an intent it runs" if _role == "gateway" else \
        "a client process, outside break-glass"
    return (f"{method.upper()} {path} refused: an exchange write outside the order gateway ({where}); nothing was "
            "sent (docs/GATEWAY.md §8 step 7)")
