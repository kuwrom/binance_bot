"""Long-lived shortbot processes on old code (#58). The guardian and every MCP server import the code once and live
for days, so a merged safety fix (kill switch, per-symbol lock) never reaches them until they restart.

- Each one registers at startup: kv `proc:<pid>` = role, mode, package dir and the code it IMPORTED
  (`code_version()`, provenance.code_sha() taken when this module is imported: once per process, shared with the
  audit trail). Hashing later would see the code on disk then, and a stale process would look current.
- `check()` lists live `shortbot-mcp` / `shortbot-guardian` processes and flags those that are unregistered (code
  from before #58, or a registration that failed) or whose code differs from their package dir on disk now. The
  guardian runs it hourly (alerts, removes rows of dead pids); the session brief shows the count. Detection only:
  nothing is ever signalled.
- The process table: /proc on Linux (the slim image has no `ps`), `ps -axww -o pid=,etime=,command=` elsewhere
  (macOS ps has no `etimes`). On Linux only our pid namespace counts: a host also sees a container's guardian under
  another pid, and a containerised guardian can't see the host's MCP servers (the host's brief covers those).
- Code is judged only within our mount namespace (`mnt`): with compose `pid: host` the host and the containers share
  one pid table but not their files, so a package dir that exists only in the other view is unknown here
  (`unknown_here`), never "gone". A row registered before the field is judged as before.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import provenance

log = logging.getLogger("shortbot.procs")

STARTED = time.time()
CODE = provenance.code_sha()
GRACE_S = 120.0              # a process registers within seconds of starting; also the pid-reuse tolerance
ALERT_COOLDOWN_S = 6 * 3600
PS_TIMEOUT_S = 10
HINT = {"mcp": "reconnect shortbot via /mcp in that Claude Code session", "guardian": "restart the guardian"}
VM_SHARES = ("virtiofs", "fakeowner", "fuse.grpcfuse", "grpcfuse", "osxfs", "9p", "fuse.sshfs")
_PY = r"(?:\S*/)?[Pp]ython[\d.]*"
_OPTS = r"(?:-\S+\s+(?:[^-\s]\S*\s+)??)*"     # interpreter options, some with an argument (-X dev)
_ROLE = re.compile(rf"^(?:{_PY}\s+{_OPTS})?(?:(?:\S*/)?shortbot-(mcp|guardian)|-m\s+shortbot\.(mcp_server|guardian))"
                   r"(?:\s|$)")


def _namespace(kind: str) -> str:
    try:
        return os.readlink(f"/proc/self/ns/{kind}")
    except OSError:
        return ""   # no namespaces (macOS): one process table and one filesystem view per host


NS = _namespace("pid")
MNT = _namespace("mnt")


def code_version() -> str:
    """The code this process imported (sha256 over the package), fixed at startup."""
    return CODE


# ------------------------------------------------------------ registration
def register(j, role: str, mode: str) -> None:
    """Best-effort and posted (#79): a process that can't register shows up as unregistered (alerted), it never fails
    or waits to start."""
    rec = {"pid": os.getpid(), "role": role, "code": CODE, "pkg": str(provenance.PKG), "mode": mode, "ns": NS,
           "mnt": MNT, "started": STARTED, "argv": " ".join(sys.argv[:2])}
    j.post(lambda j: j.set(f"proc:{os.getpid()}", json.dumps(rec)))


def register_app(role: str) -> None:
    try:
        from .app import get_app
        app = get_app()
        register(app.journal, role, app.settings.mode)
    except Exception:  # noqa: BLE001 - logged: the process shows up as unregistered (alerted)
        log.exception("could not register %s pid %d", role, os.getpid())


def rows(j) -> dict[int, dict[str, Any]]:
    out = {}
    for k, v in j.db.execute("SELECT k, v FROM kv WHERE k LIKE 'proc:%'").fetchall():
        try:
            pid = int(k[5:])
        except ValueError:
            continue
        try:
            out[pid] = dict(json.loads(v))
        except (ValueError, TypeError):
            out[pid] = {"ns": NS}   # unreadable: removed by the next check
    return out


def forget(j, pids) -> None:
    """Posted (#79): bookkeeping of dead processes, never awaited."""
    ks = [(f"proc:{pid}",) for pid in pids]
    if ks:
        j.post(lambda j: j.db.executemany("DELETE FROM kv WHERE k=?", ks))


# ------------------------------------------------------------ the process table
@dataclass
class Proc:
    pid: int
    age_s: float
    cmd: str
    role: str | None


def role_of(cmd: str) -> str | None:
    """'mcp' / 'guardian' for an interpreter running that entry point (not grep, pkill, tail, uv run, the CLI...)."""
    m = _ROLE.match(cmd)
    if not m:
        return None
    return "mcp" if (m.group(1) or m.group(2)) in ("mcp", "mcp_server") else "guardian"


def parse_etime(s: str) -> float:
    """ps etime: [[dd-]hh:]mm:ss."""
    days, _, rest = s.strip().rpartition("-")
    secs = 0
    for part in rest.split(":"):
        secs = secs * 60 + int(part)
    return int(days or 0) * 86400 + secs


def parse_ps(out: str) -> dict[int, Proc]:
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        cmd = parts[2].strip() if len(parts) > 2 else ""
        procs[int(parts[0])] = Proc(int(parts[0]), parse_etime(parts[1]), cmd, role_of(cmd))
    return procs


def stat_start(stat: str) -> int:
    """/proc/<pid>/stat field 22 (starttime, clock ticks after boot); comm (field 2) may hold spaces and ')'."""
    return int(stat[stat.rindex(")") + 2:].split()[19])


def nspid_depth(status: str) -> int | None:
    """How many pid namespaces deep (the NSpid line, Linux >= 4.1); None when the kernel doesn't say."""
    for line in status.splitlines():
        if line.startswith("NSpid:"):
            return len(line.split()) - 1
    return None


def _linux() -> dict[int, Proc]:
    tick, up = os.sysconf("SC_CLK_TCK"), float(Path("/proc/uptime").read_text().split()[0])
    mine = nspid_depth(Path("/proc/self/status").read_text())
    procs = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        d = Path("/proc") / name
        try:
            cmd = " ".join(a.decode(errors="replace") for a in (d / "cmdline").read_bytes().split(b"\0") if a)
            age = up - stat_start((d / "stat").read_text()) / tick
            role = role_of(cmd)
            if role and mine is not None and nspid_depth((d / "status").read_text()) != mine:
                role = None   # another pid namespace (a container seen from the host): not ours to judge
        except (OSError, ValueError, IndexError):
            continue   # exited while we looked
        procs[int(name)] = Proc(int(name), age, cmd, role)
    return procs


def snapshot() -> dict[int, Proc]:
    """Every process this host shows us: pid -> age, command and shortbot role (blocking: call it in a thread)."""
    if sys.platform.startswith("linux") and Path("/proc/self/stat").exists():
        return _linux()
    out = subprocess.run(["ps", "-axww", "-o", "pid=,etime=,command="], capture_output=True, text=True,
                         timeout=PS_TIMEOUT_S, check=True, env={**os.environ, "LC_ALL": "C"}).stdout
    return parse_ps(out)


def disk_codes(pkgs) -> dict[str, str | None]:
    """The code on disk now per package dir (None: the dir is gone, e.g. a removed worktree)."""
    return {p: provenance.code_sha(Path(p)) if Path(p).is_dir() else None for p in pkgs}


def _observe(pkgs) -> tuple[dict[int, Proc], dict[str, str | None], float]:
    live = snapshot()
    return live, disk_codes(pkgs), time.time()


# ------------------------------------------------------------ the audit
def age(s: float) -> str:
    s = int(s)
    if s >= 86400:
        return f"{s // 86400}d {s % 86400 // 3600}h"
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m"


def _elsewhere(r: dict, mnt: str) -> bool:
    """Registered from another mount namespace: its package dir isn't ours to read (a row without the field: ours)."""
    return r.get("mnt", mnt) != mnt


def unknown_here(rows: dict[int, dict], live: dict[int, Proc], ns: str | None = None,
                 mnt: str | None = None) -> list[int]:
    """Live shortbot processes of our pid namespace registered from another mount namespace: code unknown here."""
    ns, mnt = NS if ns is None else ns, MNT if mnt is None else mnt
    return sorted(pid for pid, p in live.items() if p.role and (r := rows.get(pid)) is not None
                  and r.get("ns", "") == ns and _elsewhere(r, mnt))


def audit(rows: dict[int, dict], live: dict[int, Proc], disk: dict[str, str | None], now: float,
          ns: str | None = None, mnt: str | None = None) -> tuple[list[dict[str, Any]], list[int]]:
    """(findings, pids whose rows are dead). Rows of another pid namespace, or registered after the listing, are
    left alone; a live pid that started at another time than its row is a reused pid (dead row). A live process
    registered from another mount namespace is unknown here, never stale (unknown_here)."""
    ns, mnt = NS if ns is None else ns, MNT if mnt is None else mnt
    dead = []
    for pid, row in rows.items():
        started = float(row.get("started") or 0)
        if row.get("ns", "") != ns or started > now - GRACE_S:
            continue
        p = live.get(pid)
        if p is None or abs(now - p.age_s - started) > GRACE_S:
            dead.append(pid)
    findings = []
    for pid, p in sorted(live.items()):
        if not p.role:
            continue
        r = rows.get(pid)
        if r and r.get("ns", "") == ns and pid not in dead:
            if _elsewhere(r, mnt):
                continue
            pkg = r.get("pkg")
            now_code = disk.get(pkg) if pkg is not None else None   # disk is keyed by the rows' pkg values
            if now_code is not None and now_code == r.get("code"):
                continue
            why, detail = "stale", (f"its code directory {r.get('pkg')} is gone" if now_code is None else
                                    f"it runs code {str(r.get('code'))[:12]}, the code on disk is now {now_code[:12]}")
        elif p.age_s < GRACE_S:
            continue   # may not have registered yet
        else:
            why, detail = "unregistered", ("unregistered: it runs code from before process registration (#58), or "
                                           "failed to register, and may ignore the kill switch and per-symbol locks")
        since = datetime.fromtimestamp(now - p.age_s, UTC).strftime("%Y-%m-%d %H:%M UTC")
        findings.append({"pid": pid, "role": p.role, "age_s": p.age_s, "why": why,
                         "text": f"shortbot-{p.role} pid {pid}, up {age(p.age_s)} (since {since}): {detail}. "
                                 f"Fix: {HINT[p.role]}."})
    return findings, dead


async def check(j) -> tuple[list[dict[str, Any]], list[int], int, list[int]]:
    """(findings, dead row pids, live mcp/guardian processes checked, pids whose code is unknown here). The table and
    the hashing run in a thread."""
    rs = rows(j)
    live, disk, now = await asyncio.to_thread(_observe, {r["pkg"] for r in rs.values() if r.get("pkg")})
    findings, dead = audit(rs, live, disk, now)
    elsewhere = [pid for pid in unknown_here(rs, live) if pid not in dead]
    return findings, dead, sum(1 for p in live.values() if p.role) - len(elsewhere), elsewhere


async def brief_line(j) -> str:
    findings, _, n, elsewhere = await check(j)
    other = (f"; {len(elsewhere)} registered from another mount namespace (pid {', '.join(map(str, elsewhere))}, "
             "a container or the host): code unknown here, judged on its own side" if elsewhere else "")
    if not findings:
        return f"- shortbot processes: none stale ({n} mcp/guardian checked{other})"
    each = "; ".join(f"pid {f['pid']} {f['role']} up {age(f['age_s'])} ({f['why']})" for f in findings)
    todo = " / ".join(HINT[r] for r in sorted({f["role"] for f in findings}, reverse=True))
    return f"- ⚠️ {len(findings)} shortbot processes on stale code: {each}. Fix: {todo}{other}"


# ------------------------------------------------------------ Docker VM
def mount_type(path: str | Path, mountinfo: str | Path = "/proc/self/mountinfo") -> str | None:
    """Filesystem type of the mount holding `path` (longest mount point that contains it)."""
    p, best, fs = os.path.abspath(path), "", None
    for line in Path(mountinfo).read_text().splitlines():
        left, _, right = line.partition(" - ")
        fields = left.split()
        if len(fields) < 5 or not right:
            continue
        mp = fields[4].replace("\\040", " ")
        if (p == mp or p.startswith(mp.rstrip("/") + "/")) and len(mp) > len(best):
            best, fs = mp, right.split()[0]
    return fs


def docker_vm(data_dir: str | Path, dockerenv: str | Path = "/.dockerenv", release: str | None = None,
              mountinfo: str | Path = "/proc/self/mountinfo") -> str | None:
    """Why this process runs in a container behind a VM (Docker Desktop on a Mac...), or None. A container always
    reports Linux, so the host can't be asked: Docker Desktop's kernel says linuxkit, a VM file share mounts the data
    dir as virtiofs, fakeowner, grpcfuse, osxfs, 9p or sshfs. flock (data/locks) and SQLite WAL don't cross it."""
    if not os.path.exists(dockerenv):
        return None
    release = os.uname().release if release is None else release
    if "linuxkit" in release.lower():
        return f"the kernel is Docker Desktop's VM ({release})"
    try:
        fs = mount_type(data_dir, mountinfo)
    except OSError:
        return None
    return f"{data_dir} is on a {fs} share from the host" if fs in VM_SHARES else None
