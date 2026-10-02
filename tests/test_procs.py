"""Stale long-lived processes (#58): each guardian / MCP server registers the code it imported at startup; the guardian
(hourly) and the session brief flag live ones that are unregistered or whose code differs from the code on disk now.
Detection only: nothing is ever signalled."""

import json
import os
import subprocess
import sys
import time

import pytest

from shortbot import procs, provenance
from shortbot.journal import Journal
from shortbot.procs import Proc

MAC_MCP = "/Library/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python " \
          ".venv/bin/shortbot-mcp"
NOW = 1_800_000_000.0
DAY = 86_400


def pkg(tmp_path, name="pkg", body="x = 1\n"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "a.py").write_text(body)
    return d


def row(pid, role="mcp", code="c0", pkg_dir="/nowhere", started=NOW - DAY, ns=None):
    return {"pid": pid, "role": role, "code": code, "pkg": str(pkg_dir), "mode": "demo",
            "ns": procs.NS if ns is None else ns, "started": started}


# ------------------------------------------------------------ parsing
@pytest.mark.parametrize("text, secs", [("00:07", 7), ("05:03", 303), ("02:03:04", 7384), ("1-15:04:50", 140690),
                                        ("12-00:00:00", 12 * DAY)])
def test_etime(text, secs):
    assert procs.parse_etime(text) == secs


def test_ps_output_is_parsed_with_spaces_in_the_command():
    out = f"  3168    01-15:04:50 {MAC_MCP}\n81536          03:56 /usr/bin/python3 -m shortbot.guardian --x 'a b'\n" \
          "   12 00:01 \n"
    got = procs.parse_ps(out)
    assert got[3168] == Proc(3168, 140690, MAC_MCP, "mcp")
    assert got[81536].role == "guardian" and got[81536].age_s == 236
    assert got[12].cmd == "" and got[12].role is None


@pytest.mark.parametrize("cmd, role", [
    (MAC_MCP, "mcp"),
    ("/app/.venv/bin/python /app/.venv/bin/shortbot-guardian", "guardian"),
    ("/Users/x/binance_bot/.venv/bin/python3.13 -u /Users/x/binance_bot/.venv/bin/shortbot-mcp", "mcp"),
    ("python -m shortbot.mcp_server", "mcp"),
    ("/usr/bin/python3 -X dev -m shortbot.guardian", "guardian"),
    (".venv/bin/shortbot-guardian", "guardian"),
    ("grep shortbot-mcp", None),
    ("pkill -f shortbot-guardian", None),
    ("tail -f data/logs/guardian.log", None),
    ("uv run shortbot-mcp", None),
    ("/bin/zsh -c nohup .venv/bin/shortbot-guardian >> data/logs/guardian.log", None),
    (".venv/bin/python .venv/bin/shortbot brief", None),
    ("python -c import shortbot.guardian", None),
    ("python -m shortbot.guardian_jobs", None),
    ("/x/python /x/shortbot-mcp-old", None),
])
def test_role_of(cmd, role):
    assert procs.role_of(cmd) == role


def test_proc_stat_start_survives_a_comm_with_spaces_and_parens():
    fields = " ".join(str(i) for i in range(3, 23))          # fields 3..22: starttime (22) is the 20th
    assert procs.stat_start(f"4242 (my ) proc) {fields} 23 24") == 22


def test_nspid_depth():
    assert procs.nspid_depth("Name:\tpython\nNSpid:\t812\t7\nNSsid:\t1\n") == 2
    assert procs.nspid_depth("Name:\tpython\nNSpid:\t812\n") == 1
    assert procs.nspid_depth("Name:\tpython\n") is None   # kernel < 4.1: no filtering


# ------------------------------------------------------------ registration
def test_code_version_is_fixed_at_import(monkeypatch):
    """A stale process must report the code it imported, never the code on disk when first asked."""
    v = procs.code_version()
    assert len(v) == 64
    monkeypatch.setattr(provenance, "code_sha", lambda *a, **k: "changed on disk")
    assert procs.code_version() == v


def test_register_writes_the_record():
    j = Journal(":memory:")
    assert procs.register(j, "mcp", "demo")
    r = json.loads(j.get(f"proc:{os.getpid()}"))
    assert r["pid"] == os.getpid() and r["role"] == "mcp" and r["mode"] == "demo" and r["ns"] == procs.NS
    assert r["code"] == procs.code_version() and r["pkg"] == str(provenance.PKG)
    assert abs(r["started"] - procs.STARTED) < 1 and r["started"] <= time.time()


def test_register_never_raises():
    class Broken:
        def set(self, k, v):
            raise RuntimeError("database is locked")
    assert procs.register(Broken(), "guardian", "demo") is False


def test_register_app_never_raises(monkeypatch):
    import shortbot.app as app_mod

    def boom(*a, **k):
        raise RuntimeError("bad .env")
    monkeypatch.setattr(app_mod, "get_app", boom)
    assert procs.register_app("mcp") is False


# ------------------------------------------------------------ audit
def test_an_unregistered_old_mcp_server_is_flagged_with_the_reconnect_hint():
    live = {3168: Proc(3168, DAY + 15 * 3600, MAC_MCP, "mcp")}
    [f], dead = procs.audit({}, live, {}, NOW)
    assert not dead and f["pid"] == 3168 and f["role"] == "mcp" and f["why"] == "unregistered"
    assert "pid 3168" in f["text"] and "1d 15h" in f["text"] and "reconnect shortbot via /mcp in that Claude Code " \
        "session" in f["text"] and "kill switch" in f["text"]


def test_a_young_unregistered_process_gets_a_grace_period():
    live = {9: Proc(9, procs.GRACE_S - 5, MAC_MCP, "mcp")}
    assert procs.audit({}, live, {}, NOW) == ([], [])


def test_registered_on_current_code_is_fine_and_changed_code_is_stale(tmp_path):
    d = pkg(tmp_path)
    code = provenance.code_sha(d)
    rows = {5: row(5, code=code, pkg_dir=d), 6: row(6, role="guardian", code="old", pkg_dir=d)}
    live = {5: Proc(5, DAY, MAC_MCP, "mcp"), 6: Proc(6, DAY, "python -m shortbot.guardian", "guardian")}
    [f], dead = procs.audit(rows, live, {str(d): code}, NOW)
    assert not dead and f["pid"] == 6 and f["why"] == "stale" and "restart the guardian" in f["text"]
    assert f"code on disk is now {code[:12]}" in f["text"]


def test_the_code_on_disk_is_hashed_per_package_dir(tmp_path):
    """A process run from a worktree is compared with its own checkout, not the main one."""
    a, b = pkg(tmp_path, "a"), pkg(tmp_path, "b", "y = 2\n")
    disk = procs.disk_codes({str(a), str(b), str(tmp_path / "gone")})
    assert disk == {str(a): provenance.code_sha(a), str(b): provenance.code_sha(b), str(tmp_path / "gone"): None}
    rows = {7: row(7, code=disk[str(b)], pkg_dir=b), 8: row(8, code="x", pkg_dir=tmp_path / "gone")}
    live = {7: Proc(7, DAY, MAC_MCP, "mcp"), 8: Proc(8, DAY, MAC_MCP, "mcp")}
    [f], _ = procs.audit(rows, live, disk, NOW)
    assert f["pid"] == 8 and "is gone" in f["text"]


def test_dead_and_reused_pids_are_cleaned_fresh_and_foreign_rows_kept():
    rows = {1: row(1), 2: row(2, started=NOW - 3 * DAY), 3: row(3, started=NOW - 10),
            4: row(4, ns="pid:[4026532999]"), 5: row(5, role="cli")}
    live = {2: Proc(2, 600, MAC_MCP, "mcp"),          # pid 2 now belongs to a new, unregistered process
            5: Proc(5, DAY, ".venv/bin/python .venv/bin/shortbot scan", None)}
    findings, dead = procs.audit(rows, live, {}, NOW)
    assert sorted(dead) == [1, 2]                      # 3 registered after the listing; 4 is another pid namespace
    assert [(f["pid"], f["why"]) for f in findings] == [(2, "unregistered")]


def test_forget_and_rows_round_trip():
    j = Journal(":memory:")
    for pid in (11, 12):
        j.set(f"proc:{pid}", json.dumps(row(pid)))
    j.set("proc:13", "not json")
    rs = procs.rows(j)
    assert set(rs) == {11, 12, 13} and rs[11]["role"] == "mcp" and rs[13]["ns"] == procs.NS
    procs.forget(j, [11, 13])
    assert set(procs.rows(j)) == {12} and j.get("proc:11") is None


async def test_check_combines_the_journal_the_listing_and_the_disk(monkeypatch, tmp_path):
    d = pkg(tmp_path)
    j = Journal(":memory:")
    j.set("proc:5", json.dumps(row(5, code=provenance.code_sha(d), pkg_dir=d, started=time.time() - DAY)))
    j.set("proc:6", json.dumps(row(6, started=time.time() - DAY)))
    monkeypatch.setattr(procs, "snapshot", lambda: {5: Proc(5, DAY, MAC_MCP, "mcp"),
                                                    9: Proc(9, 2 * DAY, MAC_MCP, "mcp")})
    findings, dead, n = await procs.check(j)
    assert [f["pid"] for f in findings] == [9] and dead == [6] and n == 2


# ------------------------------------------------------------ the real process table (read-only)
def test_snapshot_lists_a_real_shortbot_named_process(tmp_path):
    script = tmp_path / "shortbot-mcp"
    script.write_text("import sys\nprint('up', flush=True)\nsys.stdin.read()\n")
    p = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "up"
        snap = procs.snapshot()
        assert snap[p.pid].role == "mcp" and 0 <= snap[p.pid].age_s < 60
        assert os.getpid() in snap and snap[os.getpid()].role is None
    finally:
        p.stdin.close()   # it exits on its own: nothing is signalled
        p.wait(10)
    assert p.pid not in procs.snapshot()


# ------------------------------------------------------------ Docker VM (flock and WAL don't cross it)
def test_docker_vm(tmp_path):
    env = tmp_path / ".dockerenv"
    info = tmp_path / "mountinfo"
    info.write_text("22 1 0:21 / / rw - overlay overlay rw\n"
                    "35 22 0:31 /Users/me/bot/data /app/data rw,relatime - virtiofs data rw\n")
    assert procs.docker_vm("/app/data", dockerenv=env, release="6.10.14-linuxkit", mountinfo=info) is None
    env.write_text("")
    assert "linuxkit" in procs.docker_vm("/app/data", dockerenv=env, release="6.10.14-linuxkit", mountinfo=info)
    assert "virtiofs" in procs.docker_vm("/app/data/x", dockerenv=env, release="6.8.0-aws", mountinfo=info)
    info.write_text("22 1 0:21 / / rw - overlay overlay rw\n35 22 259:1 /home/u/bot/data /app/data rw - ext4 "
                    "/dev/nvme0n1p1 rw\n")
    assert procs.docker_vm("/app/data", dockerenv=env, release="6.8.0-aws", mountinfo=info) is None   # EC2: fine


# ------------------------------------------------------------ guardian (monitor lane) and session brief
def stale_world(monkeypatch, j):
    j.set("proc:6", json.dumps(row(6, started=time.time() - DAY)))     # dead
    monkeypatch.setattr(procs, "snapshot", lambda: {3168: Proc(3168, DAY + 15 * 3600, MAC_MCP, "mcp"),
                                                    81: Proc(81, 3 * 3600, "python -m shortbot.guardian", "guardian")})


async def test_the_guardian_alerts_each_stale_process_once_per_cooldown_and_cleans_dead_rows(monkeypatch):
    from shortbot.guardian import MONITOR
    from tests.fakes import FakeCcxt
    from tests.test_guardian import guardian_with
    g = guardian_with(FakeCcxt())
    assert MONITOR["processes"][0] == 3600
    assert any(job.name == "processes" for job in g._lanes()["monitor"].jobs)
    stale_world(monkeypatch, g.j)
    await g.check_processes()
    alerts = {a["kind"]: a for a in g.j.alerts()}
    assert set(alerts) == {"stale_process:3168", "stale_process:81"} and g.j.get("proc:6") is None
    assert "reconnect shortbot via /mcp" in alerts["stale_process:3168"]["message"]
    assert "restart the guardian" in alerts["stale_process:81"]["message"]
    await g.check_processes()
    assert len(g.j.alerts()) == 2   # 6 h cooldown per pid


async def test_the_guardian_registers_itself_and_warns_about_a_docker_vm(monkeypatch):
    from tests.fakes import FakeCcxt
    from tests.test_guardian import guardian_with
    g = guardian_with(FakeCcxt())
    monkeypatch.setattr(procs, "docker_vm", lambda data_dir: None)
    await g._startup()
    assert json.loads(g.j.get(f"proc:{os.getpid()}"))["role"] == "guardian"
    assert [a["kind"] for a in g.j.alerts()] == ["guardian_started"]
    monkeypatch.setattr(procs, "docker_vm", lambda data_dir: "the kernel is Docker Desktop's VM (6.10.14-linuxkit)")
    await g._startup()
    [vm] = [a for a in g.j.alerts() if a["kind"] == "guardian_docker_vm"]
    assert "linuxkit" in vm["message"] and "data/locks" in vm["message"]


async def test_the_brief_shows_one_line_with_the_stale_count(monkeypatch):
    from shortbot.cli_brief import brief
    from tests.test_brief import app
    a = app()
    stale_world(monkeypatch, a.journal)
    lines = [ln for ln in (await brief(a)).splitlines() if "shortbot process" in ln]
    assert len(lines) == 1 and "2 shortbot processes on stale code" in lines[0] and "pid 3168" in lines[0]
    assert a.journal.get("proc:6") is not None      # the brief only reads
    monkeypatch.setattr(procs, "snapshot", lambda: {})
    assert any("none stale" in ln for ln in (await brief(a)).splitlines())

    def broken():
        raise OSError("ps: not found")
    monkeypatch.setattr(procs, "snapshot", broken)
    assert any("⚠️ stale-process check failed" in ln for ln in (await brief(a)).splitlines())
