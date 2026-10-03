"""Which process holds a WAL database's write lock (#79): asked, never taken.

SQLite's unix VFS takes the WAL write lock as a POSIX byte-range lock on byte 120 of the -shm file (WAL_WRITE_LOCK,
https://www.sqlite.org/walformat.html). fcntl(F_GETLK) only reports a conflicting lock and its owner's pid: it takes
none and changes nothing.

The query runs in a child process, always: closing ANY descriptor of a file drops every POSIX lock the closing process
holds on it (https://www.sqlite.org/howtocorrupt.html, 2.2), so a process with the database open must never open and
close its -shm itself. `python -m shortbot.wal_holder <db>` prints the answer as JSON."""

from __future__ import annotations

import fcntl
import json
import os
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

WRITE_LOCK_BYTE = 120
TIMEOUT_S = 5.0
# struct flock per platform: Darwin (l_start, l_len, l_pid, l_type, l_whence); Linux (l_type, l_whence, l_start, l_len,
# l_pid), padded to its 32 bytes
_DARWIN = "@qqihh"
_LINUX = "@hhqqi4x"


def holder(db_path: str | Path) -> dict[str, Any] | None:
    """{pid, cmd} of the process holding the write lock of the WAL database at db_path; None when it is free, held by
    this process (POSIX locks never conflict with their own process), or unknown (no -shm, no POSIX locks)."""
    try:
        out = subprocess.run([sys.executable, "-m", "shortbot.wal_holder", str(db_path)], capture_output=True,
                             text=True, timeout=TIMEOUT_S)
        got = json.loads(out.stdout or "null")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return got if isinstance(got, dict) and got.get("pid") != os.getpid() else None


def _query(fd: int, offset: int) -> int | None:
    if sys.platform == "darwin":
        req = struct.pack(_DARWIN, offset, 1, 0, fcntl.F_WRLCK, os.SEEK_SET)
        _, _, pid, typ, _ = struct.unpack(_DARWIN, fcntl.fcntl(fd, fcntl.F_GETLK, req))
    else:
        req = struct.pack(_LINUX, fcntl.F_WRLCK, os.SEEK_SET, offset, 1, 0)
        typ, _, _, _, pid = struct.unpack(_LINUX, fcntl.fcntl(fd, fcntl.F_GETLK, req))
    return None if typ == fcntl.F_UNLCK else int(pid)


def _cmd(pid: int) -> str:
    proc = Path(f"/proc/{pid}/cmdline")
    try:
        if proc.exists():
            return proc.read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()[:200]
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        return out.stdout.strip()[:200] or "?"
    except (OSError, subprocess.SubprocessError):
        return "?"


def _main(db_path: str) -> dict[str, Any] | None:
    try:
        fd = os.open(f"{db_path}-shm", os.O_RDONLY)
    except OSError:
        return None
    try:
        pid = _query(fd, WRITE_LOCK_BYTE)
    except OSError:
        return None
    finally:
        os.close(fd)   # this child holds no lock on the file: nothing to drop
    return None if pid is None else {"pid": pid, "cmd": _cmd(pid)}


if __name__ == "__main__":
    print(json.dumps(_main(sys.argv[1])))
