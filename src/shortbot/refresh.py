"""Weekly research refresh (the improvement loop's clock, docs/FAILURES.md): run as a separate process so the guardian
never blocks on it.

    .venv/bin/python -m shortbot.refresh           # what the guardian spawns every 7 days

Everything lands in <data dir>/research/<UTC date>-<sha7>/refresh[-n]/ (provenance.run_dir; the data dir is the
journal's, so a Docker install writes to /app/data/research):
1. fingerprint hist.db, then extend it in place with the newest archive days (incremental; cached files skipped)
2. rerun the full backtest ledger (rounds 0-3) into bt60_r3/ - the walk-forward check: every shadow candidate meets
   data it has never seen; its results.json carries its own provenance
3. re-seed the registry (strategy episodes, week-study detectors computed from hist.db - no extra API calls, so the
   guardian's rate budget is untouched - and labelled scan candidates), then re-validate the analog panel
4. ALWAYS write refresh.json (steps, hist.db before/after the rebuild, provenance) and a journal decision, even when
   a step failed; a failed week is retried after RETRY_S (into refresh-2/) instead of waiting another 7 days"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import Any

KV_KEY = "research_refresh_at"
EVERY_S = 7 * 86_400
RETRY_S = 6 * 3600
HIST_DAYS = 64
CMD = (sys.executable, "-m", "shortbot.refresh")   # what the guardian spawns
STOP_GRACE_S = 5.0                                 # SIGTERM to the group, then SIGKILL


async def spawn() -> asyncio.subprocess.Process:
    """Start the refresh as the leader of a new process group (#80): the histdata and backtest steps it runs join that
    group, so stop() ends them all. Started in the guardian's group, they outlived a guardian that stopped."""
    return await asyncio.create_subprocess_exec(*CMD, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.DEVNULL, start_new_session=True)


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:   # the group is gone
        pass
    except PermissionError:      # macOS answers EPERM for a group whose members exited but are not yet reaped
        pass                     # stop() still waits on the leader; a live member we can't signal shows up there


async def stop(proc: asyncio.subprocess.Process | None, grace_s: float = STOP_GRACE_S) -> bool:
    """End a spawned refresh and every process in its group: SIGTERM, then SIGKILL after grace_s for anything left (a
    step that ignored SIGTERM, or one its leader left behind). True when the refresh was still running, i.e. its run
    was cut short. A refresh that already finished is left alone: it waited for its steps and wrote its own next run,
    and its pid may belong to another process by now."""
    if proc is None or proc.returncode is not None:
        return False
    _signal_group(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), grace_s)
    except TimeoutError:
        pass
    _signal_group(proc.pid, signal.SIGKILL)
    try:
        await asyncio.wait_for(proc.wait(), grace_s)
    except TimeoutError:
        raise RuntimeError(f"pid {proc.pid} still running after SIGKILL") from None   # stop_refresh tells the human
    return True


def due(journal, now: float | None = None) -> bool:
    last = float(journal.get(KV_KEY) or 0)
    return (now or time.time()) - last >= EVERY_S


def _run(cmd: list[str], timeout: int) -> dict[str, Any]:
    t = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"cmd": " ".join(cmd[1:]), "rc": p.returncode, "s": round(time.time() - t),
                "tail": (p.stdout or p.stderr or "")[-400:]}
    except subprocess.TimeoutExpired:
        return {"cmd": " ".join(cmd[1:]), "rc": -1, "s": round(time.time() - t), "error": f"timeout {timeout}s"}
    except Exception as e:  # noqa: BLE001
        return {"cmd": " ".join(cmd[1:]), "rc": -1, "error": f"{type(e).__name__}: {e}"}


def _step(name: str, fn) -> dict[str, Any]:
    try:
        return {"cmd": name, "result": fn()}
    except Exception as e:  # noqa: BLE001 - one failed step must not lose the others or the report
        return {"cmd": name, "error": f"{type(e).__name__}: {e}"}


def main() -> int:
    from . import provenance as P
    desk = P.desk_db()                           # Settings.db_path, resolved before the chdir
    data = desk.parent
    if data.name == "data":
        os.chdir(data.parent)       # relative defaults elsewhere ("data/scripts_catalog.json") point into it too
    from .journal import Journal
    now = datetime.now(UTC)
    day, git = now.strftime("%Y-%m-%d"), P.git_state()
    run_dir = P.run_dir("refresh", git, now)
    run_dir.mkdir(parents=True, exist_ok=True)
    hist, bt_dir = data / "hist.db", run_dir / "bt60_r3"
    before = P.fingerprint(hist)                 # the build about to be overwritten in place: its last record
    steps = [_run([sys.executable, "-m", "shortbot.histdata", "build", "--days", str(HIST_DAYS), "--db", str(hist),
                   "--log", str(data / "logs" / "histdata.log")], 3 * 3600)]
    after = P.fingerprint(hist)
    steps.append(_run([sys.executable, "-m", "shortbot.backtest.run", "--days60", "--round3", "--db", str(hist),
                       "--out", str(bt_dir)], 3600))
    j = Journal(desk)
    has_bt = (bt_dir / "results.json").exists()

    def seed() -> dict[str, Any]:
        from .histdata import load60
        from .registry_seed import feed_candidates, seed_all
        out = seed_all(j, bt_dir if has_bt else None, load60(hist, days=10))
        out["scan_episodes"] = feed_candidates(j)
        return out

    def analogs() -> dict[str, Any]:
        from .analogs import validate_run
        return validate_run(str(bt_dir)) if has_bt else {"skipped": "no backtest output"}

    steps += [_step("registry seed", seed), _step("analog validation", analogs)]
    ok = all(x.get("rc", 0) == 0 and "error" not in x for x in steps)
    report = {"day": day, "ok": ok, "steps": steps, "backtest_dir": str(bt_dir),
              "hist_db": {"before": before, "after": after, "changed": before.get("id") != after.get("id")},
              "provenance": P.stamp(P.config("shortbot.refresh", [], histdata_days=HIST_DAYS),
                                    {"hist_db": after}, git=git, now=now)}
    (run_dir / "refresh.json").write_text(json.dumps(report, indent=1, default=str))
    j.log_decision("research_refresh", f"weekly refresh {'ok' if ok else 'WITH ERRORS (retry in 6h)'}: review "
                   f"{run_dir / 'refresh.json'} and {bt_dir}/report.md (walk-forward check of shadow candidates)",
                   None, snapshot={"ok": ok, "dir": str(run_dir), "hist_db_changed": report["hist_db"]["changed"],
                                   "steps": [{k: v for k, v in x.items() if k not in ('tail', 'result')}
                                             for x in steps]})
    # success: next run in 7 days. failure: pretend the last run was (7 days - 6h) ago, so it retries in 6h
    j.set(KV_KEY, str(time.time() if ok else time.time() - EVERY_S + RETRY_S))
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
