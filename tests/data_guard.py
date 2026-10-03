"""The repo's data/ belongs to the live desk: a test run never writes it (#43).

conftest points every data path into a temp dir (isolated_data) and installs Guard for the session. Guard is an audit hook
(https://docs.python.org/3/library/audit_events.html): any write this process aims under data/ (open for writing, a
SQLite connection, mkdir, remove, rename, copy...) raises PermissionError before it happens and is recorded with the
test that tried it, so the run fails even when the code under test swallowed the error. The mtime snapshot of data/
before and after the session is the backstop for writes made outside this process (a test's subprocess). Locally the
desk running from this checkout changes data/ by itself (its journal and stream WAL files move every few seconds), so
there a change no hook saw is only reported; under CI (`CI` set, as GitHub Actions does) nothing else writes data/ and it
fails the run."""

from __future__ import annotations

import os
from pathlib import Path

REPO_DATA = Path(__file__).resolve().parents[1] / "data"
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
# audit event -> positions of the path arguments it writes (https://docs.python.org/3/library/audit_events.html)
_PATH_ARGS = {"os.mkdir": (0,), "os.remove": (0,), "os.rmdir": (0,), "os.rename": (0, 1), "os.truncate": (0,),
              "os.utime": (0,), "os.chmod": (0,), "os.link": (1,), "os.symlink": (1,), "shutil.copyfile": (1,),
              "shutil.copytree": (1,), "shutil.rmtree": (0,), "sqlite3.connect": (0,)}


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """`root` and everything under it: relative path -> (mtime_ns, size). {} when root doesn't exist."""
    out: dict[str, tuple[int, int]] = {}
    for d, dirs, files in os.walk(root):
        for p in ([d] if d == str(root) else []) + [os.path.join(d, n) for n in (*dirs, *files)]:
            try:
                st = os.lstat(p)
            except FileNotFoundError:   # removed mid-walk
                continue
            out[os.path.relpath(p, root)] = (st.st_mtime_ns, st.st_size)
    return out


def changes(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> list[str]:
    return sorted([f"created {p}" for p in after.keys() - before.keys()]
                  + [f"removed {p}" for p in before.keys() - after.keys()]
                  + [f"modified {p}" for p in before.keys() & after.keys() if before[p] != after[p]])


def _path(x: object) -> str | None:
    if isinstance(x, os.PathLike):
        x = os.fspath(x)
    if isinstance(x, bytes):
        x = os.fsdecode(x)
    if not isinstance(x, str) or x in ("", ":memory:"):
        return None
    if x.startswith("file:"):   # a SQLite URI: file:<path>?mode=ro
        x = x[5:].split("?", 1)[0]
    return os.path.abspath(x)


class Guard:
    def __init__(self, root: Path = REPO_DATA):
        self.roots = {os.path.abspath(root), os.path.realpath(root)}
        self.seen: list[str] = []
        self.test = "(collection)"

    def _inside(self, p: str | None) -> bool:
        return p is not None and any(p == r or p.startswith(r + os.sep) for r in self.roots)

    def __call__(self, event: str, args: tuple) -> None:
        if event == "open":
            path, mode, flags = args
            if not ((isinstance(mode, str) and any(c in mode for c in "wax+")) or (flags or 0) & _WRITE_FLAGS):
                return
            targets: tuple[object, ...] = (path,)
        elif event in _PATH_ARGS:
            targets = tuple(args[i] for i in _PATH_ARGS[event] if i < len(args))
        else:
            return
        for t in targets:
            if self._inside(p := _path(t)):
                self.seen.append(f"{self.test}: {event} {p}")
                raise PermissionError(f"tests never write the live data dir (#43, tests/data_guard.py): {event} {p}")


SESSION = Guard()   # the one conftest installs for the run
