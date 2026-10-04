"""A test run never reaches the network or starts a shortbot process (#80).

conftest installs Guard for the session as an audit hook (https://docs.python.org/3/library/audit_events.html), like
data_guard's (#43). A DNS lookup of a non-loopback host, or a connect / sendto to a non-loopback IP address, raises
PermissionError before any packet leaves and is recorded with the test that made it; the test then fails at teardown
(conftest.no_network) even when the code under test swallowed the error, as an HTTP client's retry or a guardian
job's error handling does. Unix sockets and loopback stay open: local servers, fakes (fake_http.py, replay_fakes.py
never open a socket at all).

A child process is outside this hook. `python -m shortbot...` and the shortbot* scripts are refused unless the test is
marked `spawns_shortbot` (a deliberate child, e.g. a fresh-process rerun): an entry point such as shortbot.refresh
reaches Binance by itself, and before #80 four such children per run kept downloading history for hours after the
test that spawned them had ended. Children inherit SHORTBOT_NO_DOTENV, so none of them loads .env either.

These events come from CPython's socket module; a uvloop event loop resolves and connects in libuv and bypasses them.
Nothing in shortbot installs uvloop."""

from __future__ import annotations

import ipaddress
import itertools
import os
import socket
from pathlib import PurePath

SPAWN_MARK = "spawns_shortbot"
_DNS = {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex", "socket.gethostbyaddr"}
_SEND = {"socket.connect", "socket.sendto", "socket.sendmsg"}   # args (socket, address)
_INET = (socket.AF_INET, socket.AF_INET6)


def _local(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    if host in (None, "", "localhost"):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:   # a name: resolving it is the lookup this guard refuses
        return False


def shortbot_child(argv: list) -> str | None:
    """The shortbot entry point a child would run: `python -m shortbot[.x]` or a shortbot* script; None otherwise."""
    args = [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a) for a in argv]
    if args and PurePath(args[0]).name.startswith("shortbot"):
        return args[0]
    return next((m for flag, m in itertools.pairwise(args) if flag == "-m" and m.split(".")[0] == "shortbot"), None)


class Guard:
    def __init__(self) -> None:
        self.seen: list[str] = []
        self.test = "(collection)"
        self.spawn_ok = False      # the running test is marked spawns_shortbot

    def _refuse(self, what: str) -> None:
        self.seen.append(f"{self.test}: {what}")
        raise PermissionError(f"tests never reach the network (#80, tests/net_guard.py): {what}")

    def __call__(self, event: str, args: tuple) -> None:
        if event in _DNS:
            if not _local(args[0]):
                host = args[0].decode(errors="replace") if isinstance(args[0], bytes) else args[0]
                self._refuse(f"{event} {host!r}")
        elif event in _SEND:
            sock, addr = args[0], args[1]
            if sock.family in _INET and isinstance(addr, tuple) and not _local(addr[0]):
                self._refuse(f"{event} {addr!r}")
        elif event == "subprocess.Popen" and not self.spawn_ok:
            child = shortbot_child(list(args[1]))
            if child:
                self._refuse(f"{event} {child} (mark the test {SPAWN_MARK} if the child is deliberate)")


SESSION = Guard()   # the one conftest installs for the run
