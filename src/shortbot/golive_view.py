"""The go-live report's plumbing gate, text, brief line and live probation (#46 §4; the criteria: golive.py). Read-only.

plumbing(j): evidence that the desk's controls work end to end, each piece valid VALID_DAYS days:
- drills (drill_log.latest_results): kill-switch, restart, backup, backup-offbox; the latest result per drill is a pass
  (deploy/DEPLOY.md §7a);
- probes (probe_result decisions, probes.py #55): each probe's latest result is ok, clean and matches the fake. The
  observational probe (user-stream: it reports which URL got which events, matches_fake None by design) needs ok and
  clean. A mismatch, an inconclusive or an unclean result counts once a later `probe_explained` decision starts with
  the probe's name (`shortbot log probe_explained "<probe>: <why, and the fix>"`);
- the backup drill: kv backup_drill_last_ok (backup_drill.py);
- the gateway soak (docs/GATEWAY.md D6, DEPLOY §4a): the gateway has run (kv gateway_heartbeat) and, over the last
  VALID_DAYS days, no breakglass intent outside a drill window, no unknown or interrupted intent left unresolved, no
  intent open past intents.MAX_OPEN_S.
text(rep): the report for a human, each criterion with its threshold and source, ending with the owner's steps.
brief_line(j, limits): the session brief's line, from a light report (no bootstrap).
probation(...): live mode's numbers: live trades so far of LIVE_PROBATION_N, live fills vs the model and vs demo."""

from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

from . import drill_log, gateway_health, intents, probes, uptime
from . import fill_report as FR
from .backtest.sim import COSTS
from .backup_drill import KV_KEY as BACKUP_KV

# plan-46 §3b: evidence older than a month predates the code that runs now (master deploys itself on every merge,
# deploy/DEPLOY.md §10), so a drill, probe or clean soak counts for 30 days
VALID_DAYS = 30.0
OBSERVATIONAL = ("user-stream",)   # probe_stream.user_stream reports what it saw: matches_fake is None by design
EXPLAINED = "probe_explained"


def _age_days(ts: Any, now: datetime) -> float | None:
    try:
        return (now - datetime.fromisoformat(str(ts))).total_seconds() / 86400
    except ValueError:
        return None


def _json(raw: Any) -> dict[str, Any]:
    try:
        v = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _drills(j: Any, now: datetime, blockers: list[str]) -> dict[str, dict[str, Any]]:
    latest, out = drill_log.latest_results(j), dict[str, dict[str, Any]]()
    for name in drill_log.NAMES:
        r = latest.get(name)
        if r is None:
            out[name] = {"ok": False, "verdict": None}
            blockers.append(f"drill {name}: never run")
            continue
        age = drill_log.result_age_days(r, now)
        ok = r.get("verdict") == "pass" and age is not None and age <= VALID_DAYS
        out[name] = {"ok": ok, "verdict": r.get("verdict"), "age_days": None if age is None else round(age, 1),
                     "ts": r.get("ts"), "decision_id": r.get("decision_id")}
        if r.get("verdict") != "pass":
            blockers.append(f"drill {name}: latest is {r.get('verdict')}")
        elif not ok:
            blockers.append(f"drill {name}: last pass {age or 0:.1f} days ago (> {VALID_DAYS:g})")
    return out


def _problem(name: str, res: dict[str, Any]) -> str | None:
    if not res.get("ok"):
        return f"failed ({res.get('error') or res.get('verdict')})"
    if not res.get("clean"):
        return "left something behind (clean false)"
    if res.get("matches_fake") is False:
        return "does not match the fake"
    if res.get("matches_fake") is None and name not in OBSERVATIONAL:
        return "inconclusive (matches_fake none)"
    return None


def _probes(j: Any, now: datetime, blockers: list[str]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    notes: list[dict[str, Any]] = []
    for r in j._rows("SELECT id, ts, action, reasoning, snapshot FROM decisions WHERE action IN ('probe_result', ?)"
                     " ORDER BY id", (EXPLAINED,)):
        if r["action"] == EXPLAINED:
            notes.append(r)
        elif name := _json(r["snapshot"]).get("probe"):
            latest[str(name)] = {**_json(r["snapshot"]), "decision_id": r["id"], "ts": r["ts"]}
    out: dict[str, dict[str, Any]] = {}
    for name in probes.ORDER:
        res = latest.get(name)
        if res is None:
            out[name] = {"ok": False}
            blockers.append(f"probe {name}: never run")
            continue
        age, why = _age_days(res["ts"], now), _problem(name, res)
        expl = next((str(n["reasoning"]) for n in notes if n["id"] > res["decision_id"]
                     and str(n["reasoning"]).startswith(f"{name}:")), None) if why else None
        fresh = age is not None and age <= VALID_DAYS
        out[name] = {"ok": fresh and (why is None or expl is not None), "age_days": None if age is None else
                     round(age, 1), "verdict": res.get("verdict"), "matches_fake": res.get("matches_fake"),
                     "problem": why, "explained": expl, "decision_id": res["decision_id"]}
        if not fresh:
            blockers.append(f"probe {name}: last result {age or 0:.1f} days ago (> {VALID_DAYS:g})")
        elif why and expl is None:
            blockers.append(f"probe {name}: {why}")
    return out


def _backup(j: Any, now: datetime, blockers: list[str]) -> dict[str, Any]:
    b = _json(j.get(BACKUP_KV))
    age = _age_days(b.get("ts"), now) if b.get("ts") else None
    if age is None:
        blockers.append(f"backup drill: no pass recorded (kv {BACKUP_KV})")
    elif age > VALID_DAYS:
        blockers.append(f"backup drill: last pass {age:.1f} days ago (> {VALID_DAYS:g})")
    return {"ok": age is not None and age <= VALID_DAYS, "age_days": None if age is None else round(age, 1),
            "copy": b.get("copy")}


def soak(j: Any, now: datetime, blockers: list[str] | None = None) -> dict[str, Any]:
    """The D6 soak over the last VALID_DAYS days, this mode's intents."""
    blockers = [] if blockers is None else blockers
    since = (now - timedelta(days=VALID_DAYS)).isoformat(timespec="milliseconds")
    rows = j._rows("SELECT id, actor, status, received_at FROM intents WHERE mode=? AND received_at >= ? AND "
                   "(actor LIKE 'breakglass:%' OR status IN ('unknown', 'interrupted', 'received', 'running')) "
                   "ORDER BY received_at", (j.mode, since))

    def at(r: dict[str, Any]) -> float:
        return datetime.fromisoformat(r["received_at"]).timestamp()
    glass = [r["id"] for r in rows if r["actor"].startswith("breakglass:") and not uptime.in_window(j, at(r), at(r))]
    unresolved = [r["id"] for r in rows if r["status"] in intents.UNRESOLVED]
    stale = [r["id"] for r in rows if r["status"] in intents.OPEN and now.timestamp() - at(r) > intents.MAX_OPEN_S]
    seen = gateway_health.read(j) is not None
    if not seen:
        blockers.append("gateway soak: the gateway never ran (no kv gateway_heartbeat)")
    for ids, what in ((glass, "breakglass intent(s) outside a drill window"),
                      (unresolved, "unknown or interrupted intent(s) unresolved"),
                      (stale, f"intent(s) open past {intents.MAX_OPEN_S:g} s")):
        if ids:
            blockers.append(f"gateway soak: {len(ids)} {what}: {', '.join(ids[:5])}")
    return {"ok": seen and not (glass or unresolved or stale), "gateway_seen": seen, "breakglass": glass,
            "unresolved": unresolved, "stale_open": stale, "since": since}


def plumbing(j: Any, now: datetime | None = None) -> dict[str, Any]:
    now, blockers = now or datetime.now(UTC), list[str]()
    d, p = _drills(j, now, blockers), _probes(j, now, blockers)
    b, s = _backup(j, now, blockers), soak(j, now, blockers)
    parts = [x["ok"] for x in (*d.values(), *p.values(), b, s)]
    return {"ok": not blockers, "blockers": blockers, "current": sum(parts), "total": len(parts),
            "valid_days": VALID_DAYS, "drills": d, "probes": p, "backup_drill": b, "soak": s}


# ------------------------------------------------------------------ live probation
def _mean(xs: list[float]) -> float | None:
    return round(statistics.fmean(xs), 2) if xs else None


def probation(live: Any, demo: Any, start: str, limits: Any) -> dict[str, Any]:
    from . import golive
    lt, dt = golive.sample(live, "", limits), golive.sample(demo, start, limits)
    cal = FR.calibrate(live)
    return {"n": len(lt), "target": golive.LIVE_PROBATION_N,
            "mean_r": _mean([t["r"] for t in lt]),
            "live_excess_bps": _mean([t["excess_bps"] for t in lt if t["excess_bps"] is not None]),
            "demo_excess_bps": _mean([t["excess_bps"] for t in dt if t["excess_bps"] is not None]),
            "live_k_bps": cal["k_bps"], "model_k_bps": COSTS.slip_k_bps, "calibrated": cal["ok"],
            "source": "fill_report (#35): excess_bps = fills vs the mainnet reference minus the backtest's model; "
                      "30 = registry_forward.MIN_TARGET_N"}


def probation_line(p: dict[str, Any]) -> str:
    def bp(x: float | None) -> str:
        return "n/a" if x is None else f"{x:+.1f} bp"
    return (f"live probation: {p['n']}/{p['target']} live trades (mean R {p['mean_r']}); live fills "
            f"{bp(p['live_excess_bps'])}/trade over the model vs demo {bp(p['demo_excess_bps'])}; live k "
            f"{p['live_k_bps']} bp vs model {p['model_k_bps']:g} bp" + ("" if p["calibrated"] else " (descriptive)"))


# ------------------------------------------------------------------ rendering
def text(rep: dict[str, Any]) -> str:
    b, w = rep["blockers"], rep["window"]
    out = [f"# go-live: {rep['verdict']}" + (f" ({len(b)} blockers: {', '.join(b)})" if b else ""),
           f"window: {w['from']} (max of the epoch {w['epoch']} and uptime_since {w['uptime_since'] or 'unset'}) to "
           f"{w['to']}; {rep['mode']} journal, demo evidence" + ("; light check (no bootstrap)" if rep["light"] else ""),
           ""]
    for name, c in rep["criteria"].items():
        mark = "ok" if c["ok"] else ("BLOCKER" if c["gated"] else "note")
        out += [f"- [{mark}] {name}: {c['summary']}", f"    threshold: {c['threshold']}", f"    source: {c['source']}"]
    pl = rep["criteria"]["plumbing"]["detail"]
    out += [f"    plumbing: {x}" for x in pl["blockers"]]
    if rep.get("live_probation"):
        out += ["", probation_line(rep["live_probation"])]
    return "\n".join([*out, "", "## the owner's steps", *rep["owner_steps"]])


def brief_line(j: Any, limits: Any = None, now: datetime | None = None) -> str:
    from . import golive
    rep = golive.report(j, limits, light=True, now=now)
    b = rep["blockers"]
    line = (f"- go-live: NOT READY — {len(b)} blockers: {', '.join(b)} (`shortbot golive` for the evidence)" if b else
            "- go-live: READY on the light check (`shortbot golive` for the full report; switching to live is the "
            "owner's step)")
    if rep.get("live_probation"):
        line += "; " + probation_line(rep["live_probation"])
    return line
