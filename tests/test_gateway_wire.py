"""The order gateway's wire (#36 step 4, docs/GATEWAY.md §3.2): one JSON object per line, the socket in a 0700
directory of this user, the peer's uid read from the kernel (Linux SO_PEERCRED, macOS LOCAL_PEERCRED)."""

import asyncio
import os
import socket
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from shortbot import gateway_wire as wire
from shortbot.config import Settings


def test_the_peer_uid_comes_from_the_kernel():
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        assert wire.peer_uid(a) == wire.peer_uid(b) == os.geteuid()
    finally:
        a.close()
        b.close()


async def test_one_json_object_per_line_round_trips():
    r = asyncio.StreamReader(limit=wire.LIMIT)
    msg = {"op": "submit", "text": "two\nlines", "n": 1.5, "none": None}
    line = wire.encode(msg)
    assert line.endswith(b"\n") and line.count(b"\n") == 1   # a newline inside a string is escaped
    r.feed_data(line + wire.encode({"op": "status", "id": "X"}))
    r.feed_eof()
    assert await wire.read(r, 1.0) == msg
    assert await wire.read(r, 1.0) == {"op": "status", "id": "X"}
    with pytest.raises(ConnectionError):
        await wire.read(r, 1.0)                                  # EOF: the peer closed


@pytest.mark.parametrize("raw", [b"not json\n", b"[1, 2]\n", b'{"no": "end"'])
async def test_a_malformed_line_is_refused(raw):
    r = asyncio.StreamReader(limit=wire.LIMIT)
    r.feed_data(raw)
    r.feed_eof()
    with pytest.raises((ValueError, ConnectionError)):
        await wire.read(r, 1.0)


async def test_a_line_over_the_limit_is_refused_and_a_slow_peer_times_out():
    r = asyncio.StreamReader(limit=16)
    r.feed_data(b'{"x": "' + b"a" * 64 + b'"}\n')
    with pytest.raises(ValueError):
        await wire.read(r, 1.0)
    with pytest.raises(TimeoutError):
        await wire.read(asyncio.StreamReader(), 0.01)


def test_the_socket_lives_next_to_the_journal_per_mode():
    s = Settings("live", "", "", Path("/app/data/shortbot.db"), "", "")
    assert wire.socket_path(s) == Path("/app/data/run/gateway-live.sock")
    assert wire.socket_path(replace(s, db_path=Path("data/shortbot.db"))) == Path("data/run/gateway-live.sock")
    with pytest.raises(ValueError, match="104"):
        wire.socket_path(replace(s, db_path=Path("/" + "d" * 120) / "shortbot.db"))


def test_the_run_dir_is_made_private_and_a_foreign_one_refused(tmp_path, monkeypatch):
    d = tmp_path / "run"
    wire.secure_dir(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700 and d.stat().st_uid == os.geteuid()
    d.chmod(0o755)                                              # loosened by hand: tightened again
    wire.secure_dir(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    monkeypatch.setattr(wire.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(PermissionError, match="owned by uid"):
        wire.secure_dir(d)
