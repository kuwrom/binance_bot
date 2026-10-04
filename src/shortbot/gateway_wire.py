"""The order gateway's wire (#36 step 4, docs/GATEWAY.md §3.2): a Unix socket next to the journal, one JSON object per
line, the peer's uid checked by the kernel's word.

- Path: `<journal dir>/run/gateway-<mode>.sock`. The mode is in the path and in the handshake, so a demo client never
  drives a live gateway. AF_UNIX paths are short (unix(7): sun_path is 108 bytes on Linux; 104 on macOS, sys/un.h),
  so a longer one is refused with the reason rather than truncated.
- Auth is the directory and the peer's uid. `run/` is 0700 and this user's (secure_dir): unix(7) says "Portable
  programs should not rely on [socket file permissions] for security", and creating a socket needs write and search
  permission on its directory. Each side then reads the other's effective uid from the kernel and refuses any other:
  Linux SO_PEERCRED (struct ucred {pid, uid, gid}, as of connect/listen: unix(7)); macOS LOCAL_PEERCRED at level
  SOL_LOCAL = 0 (struct xucred {cr_version, cr_uid, ...}: xnu sys/un.h, sys/ucred.h), the option getpeereid(3) is
  built on ("there is no way for either side to influence the credentials returned to its peer"). Under docker compose
  the services run as the host user's uid (`user:`), and user-namespace remapping is opt-in.
- Framing: json.dumps escapes every control character, so a message is one line; a line is at most LIMIT bytes
  (StreamReader limit: a longer one raises, and readline drops it, CPython asyncio/streams.py).

Messages (`v` = PROTOCOL; a mismatch is refused, never guessed at; v2: `sig`, `origin.process`, guardian kinds):
  -> {"v", "op": "hello", "mode"}                  <- {"op": "hello", "ok", "v", "mode", "pid", "code", "running"}
  -> {"op": "submit", "intent": {id, kind, args, symbol, side, actor, sig}, "origin": {"claude_session", "process"}}
                                                   <- {"id", "status": "accepted"} once journalled and started, then
                                                      {"id", "status": "final", "result"} | {"id", "status": "error",
                                                      "error": {type, message}}; or at once {"id", "status": "busy"}
  -> {"op": "status", "id"}                        <- {"id", "status": "status", "answer"}   (None: never journalled)
  Any refusal: {"status": "refused", "ok": false, "error"} (a foreign uid, protocol, mode, a malformed or unserved
  intent: nothing journalled; `stale: true` when its `sig`, gateway.signature_of, is not the gateway's: D5), then the
  gateway closes the connection."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import struct
import sys
from pathlib import Path
from typing import Any

import pydantic_core

from .intents import Intent

PROTOCOL = 2
LIMIT = 4 * 1024 * 1024    # bytes per line, both sides
MAX_PATH = 103             # macOS sun_path is 104 bytes with its NUL (Linux: 108)


def socket_path(settings: Any) -> Path:
    p = Path(settings.db_path).parent / "run" / f"gateway-{settings.mode}.sock"
    if len(os.fsencode(p)) > MAX_PATH:
        raise ValueError(f"gateway socket path {p} is {len(os.fsencode(p))} bytes: AF_UNIX allows 104 on macOS (108 "
                         "on Linux). Keep the journal in a shorter directory")
    return p


def secure_dir(d: Path) -> None:
    """`d` exists, is a directory of this user and is 0700 (tightened if it was looser); else PermissionError."""
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = d.stat()
    if st.st_uid != os.geteuid():
        raise PermissionError(f"{d} is owned by uid {st.st_uid}, not this process's uid {os.geteuid()}: refusing it")
    if stat.S_IMODE(st.st_mode) != 0o700:
        d.chmod(0o700)


def peer_uid(sock: Any) -> int:
    """The effective uid of the process at the other end of a connected AF_UNIX stream socket."""
    if sys.platform == "linux":
        _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                              struct.calcsize("3i")))
        return int(uid)
    if sys.platform == "darwin":
        raw = sock.getsockopt(0, socket.LOCAL_PEERCRED, struct.calcsize("IIh16I"))   # SOL_LOCAL, struct xucred
        version, uid = struct.unpack_from("II", raw)
        if version != 0:   # XUCRED_VERSION
            raise OSError(f"unknown xucred version {version}")
        return int(uid)
    raise OSError(f"no peer credentials on {sys.platform}")


def foreign(writer: asyncio.StreamWriter) -> str | None:
    """Why the peer of this connection isn't this user (None: it is)."""
    try:
        uid = peer_uid(writer.get_extra_info("socket"))
    except OSError as e:
        return f"peer credentials unreadable ({e})"
    return None if uid == os.geteuid() else f"peer uid {uid} is not this process's uid {os.geteuid()}"


def jsonable(obj: Any) -> Any:
    """A body's result as the MCP SDK and the web console serialise it (non-JSON values as text)."""
    return json.loads(pydantic_core.to_json(obj, fallback=str))


def encode(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg, default=str, separators=(",", ":")) + "\n").encode()


def json_line(raw: bytes) -> dict[str, Any]:
    if not raw.endswith(b"\n"):
        raise ConnectionError("the peer closed mid-message" if raw else "the peer closed the connection")
    msg = json.loads(raw)
    if not isinstance(msg, dict):
        raise ValueError(f"a message is a JSON object, got {type(msg).__name__}")
    return msg


async def read(reader: asyncio.StreamReader, timeout_s: float | None) -> dict[str, Any]:
    """The next message. ConnectionError: the peer closed; ValueError: not one (too long, not JSON); TimeoutError."""
    return json_line(await asyncio.wait_for(reader.readline(), timeout_s))


def intent_dict(it: Intent) -> dict[str, Any]:
    from .gateway import signature_of  # here: gateway pulls in the command registry
    return {"id": it.id, "kind": it.kind, "args": it.args, "symbol": it.symbol, "side": it.side, "actor": it.actor,
            "sig": signature_of(it.kind)}
