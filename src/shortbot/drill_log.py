"""What the plumbing drills (#46) leave in the journal, and how a reader (the go-live report) finds it.

- `drill_step` decisions: one per step of a drill run (snapshot: run, drill, step, intent id and kind, status, ms, ok,
  the step's result in short).
- `drill_result` decisions: one per run that got past its refusals (a refusal journals nothing, so it never replaces
  an earlier result). Snapshot: drill, run, verdict (pass | fail | awaiting_clear: the kill-switch drill until the
  owner's clear is verified), ok, mode, started, finished, code_version, checks {name: {ok, detail}}, steps, intents,
  override (the veto_override the drill's trades carried, if any) and the drill's own fields. Latest wins.
- kv backup_drill_last_ok (backup_drill.py) and an attestation (`shortbot drill attest backup-offbox`) are results too.
Readers: latest_results(), result_age_days()."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from . import audit
from .journal import now_iso

STEP, RESULT = "drill_step", "drill_result"
OVERRIDE = "plumbing drill #46"          # the veto_override every drill trade carries (and says so)
NAMES = ("kill-switch", "restart", "backup", "backup-offbox")
ATTESTS = ("backup-offbox",)


def check(ok: bool, detail: Any = None) -> dict[str, Any]:
    return {"ok": bool(ok), "detail": detail}


def verdict(checks: dict[str, dict[str, Any]]) -> str:
    return "pass" if checks and all(c["ok"] for c in checks.values()) else "fail"


def result(j: Any, drill: str, verdict_: str, checks: dict[str, dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """Journal one run's drill_result (posted, like every decision) and return it."""
    failed = [k for k, c in checks.items() if not c["ok"]]
    res = {"drill": drill, "verdict": verdict_, "ok": verdict_ == "pass", "mode": j.mode,
           "finished": now_iso(), "code_version": audit.CODE_VERSION, "checks": checks, **extra}
    res["decision_id"] = j.log_decision(RESULT, f"{drill}: {verdict_}" + (f" (failed: {', '.join(failed)})"
                                                                           if failed else ""), None, snapshot=res)
    return res


def latest_results(j: Any) -> dict[str, dict[str, Any]]:
    """The latest drill_result per drill, with its decision id and time (`ts`), any mode: each carries its own."""
    out: dict[str, dict[str, Any]] = {}
    for r in j._rows(f"SELECT id, ts, snapshot FROM decisions WHERE action='{RESULT}' ORDER BY id"):
        try:
            snap = json.loads(r["snapshot"] or "{}")
        except ValueError:
            continue
        if isinstance(snap, dict) and snap.get("drill"):
            out[str(snap["drill"])] = {**snap, "decision_id": r["id"], "ts": r["ts"]}
    return out


def result_age_days(res: dict[str, Any], now: datetime | None = None) -> float | None:
    try:
        at = datetime.fromisoformat(str(res.get("ts")))
    except ValueError:
        return None
    return ((now or datetime.now(UTC)) - at).total_seconds() / 86400


def attest(j: Any, what: str, evidence: str) -> dict[str, Any]:
    """The owner's record of a drill the desk can't run itself (an off-box restore): a drill_result, verdict pass,
    with the evidence text as given."""
    if what not in ATTESTS:
        return {"ok": False, "error": f"attest one of {', '.join(ATTESTS)}"}
    if len((evidence or "").strip()) < 20:
        return {"ok": False, "error": "--evidence must say what was restored, from where, and what was checked "
                                      "(at least 20 characters)"}
    return result(j, what, "pass", {"attested": check(True, evidence.strip())}, attested=True,
                  by=audit.actor(), started=now_iso())
