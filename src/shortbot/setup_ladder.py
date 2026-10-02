"""Per-setup drawdown ladder, run like a pod (docs/PROPOSALS.md P2 spec; learning #43).

Each setup (with the variants rolled into it) keeps its own ledger of net R since its epoch:
- NORMAL (1x) -> HALF (0.5x) when the drawdown from the high-water mark reaches `halve`;
- HALF -> NORMAL when it recovers to halve / 2;
- NORMAL or HALF -> SHADOW (0x, scans keep logging it) at `stop`.
SHADOW lasts until a code-evaluated re-enable on criteria frozen at the stop (never by the agent that proposed the
trades; live mode also needs a human clear). The setup then returns at HALF with the high-water mark reset to the
cumulative R at that moment.

Thresholds are the 80% / 95% quantiles of the 100-trade max drawdown of a setup that truly earns +0.10R per trade
(backtest/drawdown.py):
- below 50 trades, a prior table from the 60-day backtest;
- from 50 trades on, a bootstrap of the setup's own residual R, clamped to [0.75x, 1.5x] of the prior.

State, kept in journal kv `setup_ladder:<setup>`, holds only events: epoch resets, each stop with its frozen
criteria, and each re-enable. The ladder state itself is recomputed from the trades every time, so it is deterministic.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

PRIORS = {"pump_fade": (8.6, 12.3), "post_pump_decay": (7.7, 10.8), "funding_hi": (8.2, 11.5),
          "bounce_fade": (11.1, 15.7), "level_fade": (11.3, 16.2)}
DEFAULT_SIGMA, K_HALVE, K_STOP = 0.80, 12, 17
MIN_N, CLAMP, MU_REQUIRED = 50, (0.75, 1.5), 0.10
CRITERIA = {"min_episodes": 30, "min_days": 10, "mean_gt": 0.0, "t_day_ge": 1.0, "size": 0.5,
            "live_needs_human": True}
MULT = {"NORMAL": 1.0, "HALF": 0.5, "SHADOW": 0.0}
ROLLUP_RHO, ROLLUP_DAYS, ROLLUP_OVERLAP = 0.5, 20, 0.70
KV = "setup_ladder:"


def prior(setup: str) -> tuple[float, float]:
    return PRIORS.get(setup, (round(K_HALVE * DEFAULT_SIGMA, 2), round(K_STOP * DEFAULT_SIGMA, 2)))


@lru_cache(maxsize=256)
def _boot(resid: tuple[float, ...]) -> tuple[float, float]:
    from .backtest.drawdown import bootstrap_maxdd
    q = bootstrap_maxdd(list(resid), MU_REQUIRED, 100, paths=4000, block=5, seed=7)
    return q[0.80], q[0.95]


def thresholds(setup: str, rs: list[float]) -> dict[str, Any]:
    """(halve, stop) in R: the prior below MIN_N trades, else the clamped bootstrap of the setup's own residuals."""
    ph, ps = prior(setup)
    if len(rs) < MIN_N:
        return {"halve": ph, "stop": ps, "source": "prior", "n": len(rs)}
    m = statistics.fmean(rs)
    bh, bs = _boot(tuple(round(r - m, 6) for r in rs))
    clamp = lambda x, p: round(min(max(x, CLAMP[0] * p), CLAMP[1] * p), 2)  # noqa: E731
    return {"halve": clamp(bh, ph), "stop": clamp(bs, ps), "source": "bootstrap", "n": len(rs)}


def walk(trades: list[dict[str, Any]], halve: float, stop: float, reenables: list[str]) -> dict[str, Any]:
    """Replay the ledger. trades: [{"closed_at", "r"}] oldest first; reenables: ISO times of code-evaluated
    re-enables. A re-enable moves SHADOW to HALF and resets the high-water mark to the cumulative R at that moment;
    a later stop starts a new cycle. Returns state, cumulative R, high-water mark, drawdown and every index that
    entered SHADOW."""
    cum = hwm = 0.0
    state, stops, re, ri = "NORMAL", [], sorted(reenables), 0

    def apply_reenables(until: str | None) -> None:
        nonlocal ri, state, hwm
        while ri < len(re) and (until is None or re[ri] <= until):
            if state == "SHADOW":
                state, hwm = "HALF", cum
            ri += 1
    for k, t in enumerate(trades):
        apply_reenables(t["closed_at"])
        cum += t["r"]
        hwm = max(hwm, cum)
        dd = hwm - cum
        if state == "SHADOW":
            continue
        if dd >= stop:
            state = "SHADOW"
            stops.append(k)
        elif state == "NORMAL" and dd >= halve:
            state = "HALF"
        elif state == "HALF" and dd <= halve / 2:
            state = "NORMAL"
    apply_reenables(None)
    return {"state": state, "cum_r": round(cum, 3), "hwm_r": round(hwm, 3), "dd_r": round(hwm - cum, 3),
            "stops_idx": stops}


def reenable_check(criteria: dict[str, Any], episodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Shadow evidence since the stop: [{"day", "ret"}] (72h short return of the setup's scan detections).
    It must not reject edge > 0: n, distinct days, mean and day-clustered t over the frozen bars."""
    by: dict[str, list[float]] = {}
    for e in episodes:
        by.setdefault(e["day"], []).append(e["ret"])
    means = [statistics.fmean(v) for v in by.values()]
    n, days = len(episodes), len(by)
    mean = statistics.fmean(e["ret"] for e in episodes) if episodes else None
    t = None
    if len(means) >= 3 and statistics.stdev(means) > 0:
        t = statistics.fmean(means) / (statistics.stdev(means) / math.sqrt(len(means)))
    fails = [f"{n} episodes < {criteria['min_episodes']}"] if n < criteria["min_episodes"] else []
    fails += [f"{days} days < {criteria['min_days']}"] if days < criteria["min_days"] else []
    fails += [f"mean {mean} <= {criteria['mean_gt']}"] if mean is None or mean <= criteria["mean_gt"] else []
    fails += [f"t_day {t} < {criteria['t_day_ge']}"] if t is None or t < criteria["t_day_ge"] else []
    return {"ok": not fails, "n": n, "days": days, "mean": None if mean is None else round(mean, 4),
            "t_day": None if t is None else round(t, 2), "fails": fails}


def daily_corr(a: dict[str, float], b: dict[str, float]) -> tuple[float | None, int]:
    """Pearson of two setups' daily R sums over the union of their trading days (zeros on no-trade days);
    the count is the number of days on which BOTH traded."""
    days = sorted(set(a) | set(b))
    both = sum(1 for d in days if d in a and d in b)
    if both < 2:
        return None, both
    x, y = [a.get(d, 0.0) for d in days], [b.get(d, 0.0) for d in days]
    if statistics.pstdev(x) == 0 or statistics.pstdev(y) == 0:
        return None, both
    return statistics.correlation(x, y), both


def family(setup: str, rows: list[dict[str, Any]], overlaps: dict[str, float] | None = None) -> list[str]:
    """The setups sharing this ladder: itself plus any setup whose daily R correlates >= 0.5 over >= 20 common days,
    or whose registry episodes overlap >= 70% (overlaps: {setup: share}). Experiments on the same setup are already
    in it (same `setup` field)."""
    daily: dict[str, dict[str, float]] = {}
    for r in rows:
        if r.get("setup") and r.get("r_multiple") is not None:
            d = daily.setdefault(r["setup"], {})
            day = r["closed_at"][:10]
            d[day] = d.get(day, 0.0) + r["r_multiple"]
    out = [setup]
    for other in sorted(daily):
        if other == setup:
            continue
        rho, both = daily_corr(daily.get(setup, {}), daily[other])
        if (rho is not None and rho >= ROLLUP_RHO and both >= ROLLUP_DAYS) or \
                (overlaps or {}).get(other, 0.0) >= ROLLUP_OVERLAP:
            out.append(other)
    return out


def events(journal, setup: str) -> dict[str, Any]:
    raw = journal.get(KV + setup)
    return json.loads(raw) if raw else {"epoch": None, "stops": [], "reenables": [], "log": []}


def _save(journal, setup: str, ev: dict[str, Any]) -> None:
    journal.set(KV + setup, json.dumps(ev))


def status(journal, setup: str | None, epoch: str | None = None, overlaps: dict[str, float] | None = None,
           now: datetime | None = None) -> dict[str, Any]:
    """The ladder for a new trade's setup: state, size multiplier, drawdown, thresholds, family and reasons.
    `epoch` = the created_at of the setup's current registry version. A stop seen for the first time is recorded
    with frozen criteria (the only write this function makes)."""
    if not setup:
        return {"state": "NORMAL", "mult": 1.0, "reasons": []}
    rows = journal.closed_results()
    fam = family(setup, rows, overlaps)
    ev = events(journal, setup)
    start = max(x for x in (epoch, ev.get("epoch"), "") if x is not None)
    trades = [{"closed_at": r["closed_at"], "r": r["r_multiple"]} for r in rows
              if r.get("setup") in fam and r.get("r_multiple") is not None and (r.get("opened_at") or "") >= start]
    th = thresholds(setup, [t["r"] for t in trades])
    w = walk(trades, th["halve"], th["stop"], ev.get("reenables") or [])
    known = {x["at"] for x in ev.get("stops") or []}
    new = [trades[k]["closed_at"] for k in w["stops_idx"] if trades[k]["closed_at"] not in known]
    for at in new:                                   # freeze the criteria the moment a stop is first seen
        ev.setdefault("stops", []).append({"at": at, "criteria": dict(CRITERIA), "thresholds": th})
        ev.setdefault("log", []).append({"kind": "stop", "at": at, "dd_r": w["dd_r"]})
    if new:
        _save(journal, setup, ev)
    reasons = []
    if w["state"] == "HALF":
        reasons.append(f"setup ladder: {setup} drawdown {w['dd_r']}R >= {th['halve']}R: half size")
    elif w["state"] == "SHADOW":
        reasons.append(f"setup ladder: {setup} drawdown hit {th['stop']}R: SHADOW (no new entries) until the frozen "
                       f"re-enable criteria pass")
    return {"setup": setup, "family": fam, "state": w["state"], "mult": MULT[w["state"]], **w, "thresholds": th,
            "epoch": start or None, "stop": (ev.get("stops") or [None])[-1], "reasons": reasons}


def reenable(journal, setup: str, episodes: list[dict[str, Any]], live: bool, human_clear: bool = False,
             now: datetime | None = None) -> dict[str, Any]:
    """Code-evaluated re-enable: frozen criteria on the shadow episodes since the stop. Live also needs a human."""
    ev = events(journal, setup)
    stops = ev.get("stops") or []
    stop = stops[-1] if stops else None
    if not stop or any(r >= stop["at"] for r in ev.get("reenables") or []):
        return {"ok": False, "reason": "not stopped"}
    chk = reenable_check(stop["criteria"], [e for e in episodes if e["day"] >= stop["at"][:10]])
    if not chk["ok"]:
        return {**chk, "ok": False, "reason": "frozen criteria not met"}
    if live and stop["criteria"].get("live_needs_human") and not human_clear:      # "ok" last: never overridden
        return {**chk, "ok": False, "reason": "criteria pass; live mode needs a human clear"}
    at = (now or datetime.now(timezone.utc)).isoformat()
    ev.setdefault("reenables", []).append(at)
    ev.setdefault("log", []).append({"kind": "reenable", "at": at, "evidence": chk, "human": human_clear})
    _save(journal, setup, ev)
    return {**chk, "ok": True, "reenabled_at": at}


def reset_epoch(journal, setup: str, who: str, why: str, now: datetime | None = None) -> dict[str, Any]:
    """A journalled review: start a new ledger now. A SHADOW stop stays until re-enabled; nothing else carries over."""
    ev = events(journal, setup)
    at = (now or datetime.now(timezone.utc)).isoformat()
    ev["epoch"] = at
    ev.setdefault("log", []).append({"kind": "epoch_reset", "at": at, "who": who, "why": why})
    _save(journal, setup, ev)
    return {"ok": True, "epoch": at}


def shadow_episodes(journal, setup: str) -> list[dict[str, Any]]:
    """Labelled scan candidates that detected this setup: {"day", "ret"} with the 72h short return."""
    out = []
    for c in journal.candidates(labelled_only=True):
        if setup in (c.get("setups") or []) and c.get("short_ret_72h") is not None:
            out.append({"day": (c.get("ts") or "")[:10], "ret": c["short_ret_72h"]})
    return out
