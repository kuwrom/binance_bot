"""deploy/autodeploy.sh against a local origin (a bare repo standing in for GitHub) with stub steps: it deploys a new
`deploy` commit, rolls back and alerts once on a failed step, never deploys over local changes or a rewritten branch,
and does nothing when nothing moved. deploy_alert writes the alerts row the brief lists."""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from shortbot import alerts_view, deploy_alert
from shortbot.journal import Journal

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "autodeploy.sh"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV}, check=True, capture_output=True,
                          text=True).stdout.strip()


class Desk:
    """origin (bare) <- dev (pushes `deploy`) ; server: a clone on branch deploy, with the script in it."""

    def __init__(self, tmp: Path):
        self.tmp, self.origin, self.dev, self.server = tmp, tmp / "origin.git", tmp / "dev", tmp / "server"
        git(tmp, "init", "-q", "--bare", "-b", "master", str(self.origin))
        git(tmp, "init", "-q", "-b", "master", str(self.dev))
        (self.dev / "deploy").mkdir()
        shutil.copy(SCRIPT, self.dev / "deploy" / "autodeploy.sh")
        (self.dev / ".gitignore").write_text("data/\n")
        self.commit("v1")
        git(self.dev, "remote", "add", "origin", str(self.origin))
        git(self.dev, "push", "-q", "origin", "master", "master:deploy")
        git(tmp, "clone", "-q", "-b", "deploy", str(self.origin), str(self.server))
        self.calls = tmp / "calls.txt"
        self.alerts = tmp / "alerts.txt"
        (tmp / "alert.sh").write_text(f'#!/bin/sh\nprintf "%s|%s\\n" "$1" "$2" >> {self.alerts}\n')
        (tmp / "alert.sh").chmod(0o755)

    def commit(self, text: str) -> str:
        (self.dev / "app.txt").write_text(text)
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-q", "-m", text)
        return git(self.dev, "rev-parse", "HEAD")

    def promote(self, text: str) -> str:
        sha = self.commit(text)
        git(self.dev, "push", "-q", "-f", "origin", "master:master", "master:deploy")
        return sha

    def run(self, tests="true", health="true", build=None, **extra: str) -> subprocess.CompletedProcess:
        env = {**os.environ, **GIT_ENV, **extra,
               "AUTODEPLOY_STATE": str(self.tmp / "state"), "AUTODEPLOY_LOCK": str(self.tmp / "lock"),
               "AUTODEPLOY_INSTALL": f"echo install >> {self.calls}",
               "AUTODEPLOY_TEST": f"echo tests >> {self.calls}; {tests}",
               "AUTODEPLOY_BUILD": build or f"echo build $(cat app.txt) >> {self.calls}",
               "AUTODEPLOY_HEALTH": health, "AUTODEPLOY_ALERT": str(self.tmp / "alert.sh"),
               "AUTODEPLOY_TEST_LOG": str(self.tmp / "tests.log")}
        return subprocess.run(["bash", str(self.server / "deploy" / "autodeploy.sh")], env=env, capture_output=True,
                              text=True, timeout=60)

    def head(self) -> str:
        return git(self.server, "rev-parse", "HEAD")

    def lines(self, f: Path) -> list[str]:
        return f.read_text().splitlines() if f.exists() else []


@pytest.fixture
def desk(tmp_path):
    return Desk(tmp_path)


def test_a_new_deploy_commit_is_installed_tested_and_built_and_nothing_moved_is_a_no_op(desk):
    r = desk.run()
    assert r.returncode == 0 and desk.lines(desk.calls) == []        # nothing new: no step runs
    v2 = desk.promote("v2")
    r = desk.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert desk.head() == v2 and (desk.server / "app.txt").read_text() == "v2"
    assert desk.lines(desk.calls) == ["install", "tests", "build v2"] and desk.lines(desk.alerts) == []
    assert f"deployed {v2[:12]}" in r.stdout
    assert desk.run().returncode == 0 and len(desk.lines(desk.calls)) == 3


def test_failing_tests_roll_back_alert_once_and_skip_that_commit_until_the_next(desk):
    v1 = desk.head()
    v2 = desk.promote("v2")
    assert desk.run(tests="false").returncode == 1
    assert desk.head() == v1 and (desk.server / "app.txt").read_text() == "v1"
    assert desk.lines(desk.calls) == ["install", "tests", "install"]   # never built: the old images keep running
    (alert,) = desk.lines(desk.alerts)
    assert alert.startswith(f"deploy_failed|{v2[:12]} was not deployed: tests on the server")
    assert desk.run().returncode == 0 and desk.head() == v1 and len(desk.lines(desk.alerts)) == 1   # not retried
    v3 = desk.promote("v3")
    assert desk.run().returncode == 0 and desk.head() == v3


def test_a_failed_health_check_rebuilds_the_old_commit(desk):
    v1 = desk.head()
    desk.promote("v2")
    assert desk.run(health="false").returncode == 1
    assert desk.head() == v1
    assert desk.lines(desk.calls) == ["install", "tests", "build v2", "install", "build v1"]   # old images back
    assert "health check" in desk.lines(desk.alerts)[0]


def test_local_changes_block_the_deploy_with_one_alert_until_they_are_gone(desk):
    v1 = desk.head()
    (desk.server / "app.txt").write_text("edited on the server")
    v2 = desk.promote("v2")
    for _ in range(2):
        assert desk.run().returncode == 1 and desk.head() == v1
    (alert,) = desk.lines(desk.alerts)
    assert alert.startswith("deploy_blocked|local changes to 1 tracked files")
    (desk.server / "notes.md").write_text("untracked files don't block")
    git(desk.server, "checkout", "--", "app.txt")
    assert desk.run().returncode == 0 and desk.head() == v2


def test_a_rewritten_deploy_branch_is_never_deployed(desk):
    v1 = desk.head()
    git(desk.dev, "reset", "-q", "--hard", "HEAD")
    git(desk.dev, "commit", "-q", "--amend", "-m", "v1 rewritten")
    git(desk.dev, "push", "-q", "-f", "origin", "master:deploy")
    assert desk.run().returncode == 1 and desk.head() == v1
    assert desk.lines(desk.alerts)[0].startswith("deploy_blocked|origin/deploy")


def test_a_run_waits_for_nothing_while_another_holds_the_lock(desk):
    if shutil.which("flock"):
        holder = subprocess.Popen(["flock", str(desk.tmp / "lock"), "sleep", "5"])
    else:
        (desk.tmp / "lock.d").mkdir()
        (desk.tmp / "lock.d" / "pid").write_text(str(os.getpid()))   # a live pid owns it
        holder = None
    try:
        desk.promote("v2")
        time.sleep(0.3)
        assert desk.run().returncode == 0 and desk.lines(desk.calls) == []
    finally:
        if holder:
            holder.kill()
            holder.wait()


async def test_deploy_alert_is_an_urgent_alerts_row():
    assert await deploy_alert.send("deploy_failed", "abc was not deployed", urgent=True)
    j = Journal(os.environ["SHORTBOT_DB"])
    (row,) = [a for a in j.alerts(10) if a["kind"] == "deploy_failed"]
    assert row["message"] == "abc was not deployed"
    assert alerts_view.is_urgent("deploy_failed") and alerts_view.is_urgent("deploy_blocked")


# ------------------------------------------------------------ the health check (#36 step 6)
NOW = "CAST(strftime('%s', 'now') AS INTEGER)"


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="needs the sqlite3 CLI")
@pytest.mark.parametrize("client, ok, deployed", [("true", "true", True), ("true", "false", False),
                                                  ("false", "null", True)])
def test_the_health_check_waits_for_the_guardian_to_reach_the_gateway(desk, client, ok, deployed):
    """With a gateway in the compose file: its heartbeat and the guardian's kv gateway_client:guardian stamp, ok (or
    not its client: SHORTBOT_GATEWAY=sessions or 0), both since the build."""
    home = desk.tmp / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    docker = home / ".local" / "bin" / "docker"   # first on the script's PATH: `compose config --services`
    docker.write_text("#!/bin/sh\n[ \"$1 $2\" = 'compose config' ] && printf 'guardian\\ngateway\\nweb\\n'\nexit 0\n")
    docker.chmod(0o755)
    sql = desk.tmp / "rows.sql"
    sql.write_text(f"""CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
INSERT OR REPLACE INTO kv VALUES ('guardian_heartbeat', {NOW});
INSERT OR REPLACE INTO kv VALUES ('gateway_heartbeat', json_object('at', {NOW}));
INSERT OR REPLACE INTO kv VALUES ('gateway_client:guardian', json_object('at', {NOW}, 'client', json('{client}'),
  'ok', json('{ok}')));""")
    desk.promote("v2")
    r = desk.run(health="health", build=f"mkdir -p data && sqlite3 data/shortbot.db < {sql}", HOME=str(home),
                 AUTODEPLOY_HEALTH_S="0")
    assert (r.returncode == 0) is deployed, r.stdout + r.stderr
    if not deployed:
        (alert,) = desk.lines(desk.alerts)
        assert "the guardian not reaching the gateway" in alert
