"""Weekly research refresh (the improvement loop's clock, docs/FAILURES.md): run as a separate process so the guardian
never blocks on it.

    .venv/bin/python -m shortbot.refresh           # what the guardian spawns every 7 days

1. extend data/hist.db with the newest archive days (incremental; cached files are skipped)
2. rerun the full backtest ledger (rounds 0-3) into data/research/bt_<date>/ - the walk-forward check: every
   shadow candidate meets data it has never seen
3. re-seed the registry (strategy episodes, week-study detectors computed from hist.db - no extra API calls, so the
   guardian's rate budget is untouched - and labelled scan candidates), then re-validate the analog panel
4. ALWAYS write data/research/refresh_<date>.json and a journal decision, even when a step failed; a failed week is
   retried after RETRY_S instead of waiting another 7 days"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KV_KEY = "research_refresh_at"
EVERY_S = 7 * 86_400
RETRY_S = 6 * 3600
ROOT = Path(__file__).resolve().parents[2]          # the repo, whatever the spawning process's CWD
OUT = ROOT / "data" / "research"


def due(journal, now: float | None = None) -> bool:
    last = float(journal.get(KV_KEY) or 0)
    return (now or time.time()) - last >= EVERY_S


def _run(cmd: list[str], timeout: int) -> dict[str, Any]:
    t = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=ROOT)
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
    os.chdir(ROOT)
    from .config import Settings
    from .journal import Journal
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bt_dir = OUT / f"bt_{day}"
    OUT.mkdir(parents=True, exist_ok=True)
    steps = [_run([sys.executable, "-m", "shortbot.histdata", "build", "--days", "64"], 3 * 3600),
             _run([sys.executable, "-m", "shortbot.backtest.run", "--days60", "--round3", "--out", str(bt_dir)], 3600)]
    j = Journal(Settings.from_env().db_path)
    has_bt = (bt_dir / "results.json").exists()

    def seed() -> dict[str, Any]:
        from .histdata import load60
        from .registry_seed import feed_candidates, seed_all
        out = seed_all(j, bt_dir if has_bt else None, load60(days=10))
        out["scan_episodes"] = feed_candidates(j)
        return out

    def analogs() -> dict[str, Any]:
        from .analogs import validate_run
        return validate_run(str(bt_dir)) if has_bt else {"skipped": "no backtest output"}

    steps += [_step("registry seed", seed), _step("analog validation", analogs)]
    ok = all(x.get("rc", 0) == 0 and "error" not in x for x in steps)
    report = {"day": day, "ok": ok, "steps": steps, "backtest_dir": str(bt_dir)}
    (OUT / f"refresh_{day}.json").write_text(json.dumps(report, indent=1, default=str))
    j.log_decision("research_refresh", f"weekly refresh {'ok' if ok else 'WITH ERRORS (retry in 6h)'}: review "
                   f"{OUT / f'refresh_{day}.json'} and {bt_dir}/report.md (walk-forward check of shadow candidates)",
                   None, snapshot={"ok": ok, "steps": [{k: v for k, v in x.items() if k not in ('tail', 'result')}
                                                       for x in steps]})
    # success: next run in 7 days. failure: pretend the last run was (7 days - 6h) ago, so it retries in 6h
    j.set(KV_KEY, str(time.time() if ok else time.time() - EVERY_S + RETRY_S))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
