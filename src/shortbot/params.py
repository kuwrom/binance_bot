"""Research parameters: the AI analyst may tune risk limits; the always-on guardian may not.

Two layers
----------
1. Baseline (`RiskLimits.from_env()`): the fixed values from `.env`. The guardian on AWS only ever
   uses these; it never reads research overrides.
2. Research overrides (this module): an interactive Claude session can change any `RiskLimits`
   field with `set_risk_params` (MCP tool), e.g. try 5x leverage as an experiment. Every change needs
   a reason and an experiment name, and is written to the journal.

Attribution: each override is owned by the experiment that last set it. The active experiments are the tags that
still own at least one override, and a trade carries all of them, sorted and joined by " + " (a new tag never hides
an older one whose changes still apply), plus the full limits it was sized under (entry_features.limits).

Overrides must stay inside `ResearchBounds` (outer envelope in `.env`, human-only). These are a
sanity rail against typos (e.g. 100x instead of 10x), not the strategy's limits. They are re-checked on every
load: an override the human's current bounds no longer allow is dropped back to the .env baseline and alerted.
Overrides apply in demo mode; in live mode they are ignored unless the human sets AI_PARAMS_IN_LIVE=true, and they
are stored per mode (Journal.mode_key), so a demo experiment never carries into live.
"""

from __future__ import annotations

import json
from dataclasses import asdict, fields, replace
from typing import Any

from .config import ResearchBounds, RiskLimits, Settings
from .journal import Journal

KV_OVERRIDES = "research_params"       # {param: value}, flat (old processes read it as is)
KV_EXPERIMENT = "research_experiment"  # the active-experiments label, also the legacy owner of unowned overrides
KV_OWNERS = "research_owners"          # {param: tag}: the experiment that last set each override
JOIN = " + "
INT_FIELDS = {f.name for f in fields(RiskLimits) if f.type in (int, "int")}
TUNABLE = tuple(f.name for f in fields(RiskLimits))


class ParamError(ValueError):
    pass


class ResearchParams:
    def __init__(self, settings: Settings, journal: Journal):
        self.s = settings
        self.j = journal

    # ------------------------------------------------------------------ read
    @property
    def active(self) -> bool:
        """Overrides only apply in demo, or in live if the human explicitly allowed it."""
        return not self.s.is_live or self.s.allow_ai_params_live

    def _k(self, key: str) -> str:
        return self.j.mode_key(key)

    def _stored(self) -> tuple[str | None, dict[str, Any], dict[str, str]]:
        """(raw overrides text, overrides, owners). An override without an owner (written before owners existed, or
        by an old process) belongs to the stored experiment tag."""
        raw = self.j.get(self._k(KV_OVERRIDES))
        ov = json.loads(raw) if raw else {}
        owners = json.loads(self.j.get(self._k(KV_OWNERS)) or "{}")
        legacy = self.j.get(self._k(KV_EXPERIMENT)) or "untagged"
        return raw, ov, {k: owners.get(k) or legacy for k in ov}

    def _checked(self, retry: bool = True) -> tuple[dict[str, Any], dict[str, str]]:
        """(overrides, owners) still allowed by the CURRENT bounds and baseline. The human's tightening wins: an
        override outside the bounds is dropped back to the .env baseline, never clamped (the edge of the envelope is
        a treatment nobody approved, and the trades would keep the experiment's label); if what is left is
        inconsistent with the baseline, all are dropped. The drop is saved once and alerted."""
        raw, ov, own = self._stored()
        keep: dict[str, Any] = {}
        dropped: list[str] = []
        for k, v in ov.items():
            if k not in TUNABLE:
                dropped.append(f"{k}: not a risk limit any more")
                continue
            lo, hi = self.s.bounds.get(k)
            if not isinstance(v, (int, float)) or not lo <= v <= hi:
                dropped.append(f"{k}={v} outside the current bounds [{lo}, {hi}]")
                continue
            keep[k] = v
        if keep and (errs := consistency_errors(replace(self.s.limits, **keep))):
            dropped.append(f"all overrides: inconsistent with the .env baseline ({'; '.join(errs)})")
            keep = {}
        own = {k: own[k] for k in keep}
        if dropped and not self._drop(raw, keep, own, dropped) and retry:
            return self._checked(retry=False)   # another process changed them meanwhile: check what it wrote
        return keep, own

    def _drop(self, raw: str | None, keep: dict, own: dict, dropped: list[str]) -> bool:
        """Compare-and-set: save the checked overrides only if nobody changed them since `raw` was read (a concurrent
        set_risk_params stands). Only the winner alerts."""
        label = _label(own)
        n = self.j.db.execute("UPDATE kv SET v=? WHERE k=? AND v=?",
                              (json.dumps(keep), self._k(KV_OVERRIDES), raw)).rowcount
        if n != 1:
            self.j.db.commit()
            return False
        self._write(None, own, label)
        msg = (f"research overrides dropped ({self.s.mode}): {'; '.join(dropped)}. Active now: {label or 'baseline'}. "
               "Re-propose with set_risk_params within the current bounds if still wanted.")
        self.j.log_decision("params_dropped", msg, snapshot={
            "mode": self.s.mode, "dropped": dropped, "active": label or None, "bounds": self.s.bounds.as_dict(),
            "after": asdict(replace(self.s.limits, **keep))})
        self.j.log_alert("params_dropped", msg)
        return True

    def overrides(self) -> dict[str, Any]:
        return self._checked()[0]

    def experiments(self) -> dict[str, dict[str, Any]]:
        """{tag: {param: value}} for every experiment that still owns an override."""
        ov, own = self._checked()
        out: dict[str, dict[str, Any]] = {}
        for k, tag in own.items():
            out.setdefault(tag, {})[k] = ov[k]
        return dict(sorted(out.items()))

    def experiment(self) -> str | None:
        """The active experiments, sorted and joined: the label every trade opened now carries."""
        return JOIN.join(self.experiments()) or None

    def effective(self) -> RiskLimits:
        base = self.s.limits
        if not self.active:
            return base
        ov = self.overrides()
        return replace(base, **ov) if ov else base

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.s.mode, "overrides_active": self.active, "experiment": self.experiment(),
            "experiments": self.experiments(), "effective": asdict(self.effective()),
            "baseline_env": asdict(self.s.limits), "overrides": self.overrides(), "bounds": self.s.bounds.as_dict(),
            "note": "Guardian (AWS) always uses baseline_env. Change with set_risk_params; reset with "
                    "reset_risk_params. Trades carry every active experiment (joined by ' + ').",
        }

    # ----------------------------------------------------------------- write
    def set(self, changes: dict[str, Any], reasoning: str, experiment: str) -> dict[str, Any]:
        if not self.active:
            raise ParamError("live mode: research overrides are disabled (human can set AI_PARAMS_IN_LIVE=true)")
        if not reasoning.strip() or not experiment.strip():
            raise ParamError("reasoning and experiment name are required (this is research: say what you test)")
        tag = experiment.strip()
        if JOIN in tag:
            raise ParamError(f"experiment name can't contain '{JOIN}' (it joins the active experiments)")
        unknown = sorted(set(changes) - set(TUNABLE))
        if unknown:
            raise ParamError(f"unknown params {unknown}; tunable: {list(TUNABLE)}")
        clean: dict[str, float] = {}
        errors: list[str] = []
        for k, v in changes.items():
            try:
                v = float(v)
            except (TypeError, ValueError):
                errors.append(f"{k}: not a number ({v!r})")
                continue
            if k in INT_FIELDS:
                if v != int(v):
                    errors.append(f"{k}: must be a whole number")
                    continue
                v = int(v)
            lo, hi = self.s.bounds.get(k)
            if not lo <= v <= hi:
                errors.append(f"{k}={v} outside research bounds [{lo}, {hi}] (human edits BOUND_* in .env)")
            clean[k] = v
        before = self.effective()
        current, own = self._checked()
        merged = {**current, **clean}
        after = replace(self.s.limits, **merged)
        errors += consistency_errors(after)
        if errors:
            raise ParamError("; ".join(errors))
        owners = {**own, **{k: tag for k in clean}}
        label = _label(owners)
        self._write(merged, owners, label)
        self.j.log_decision("set_params", reasoning, snapshot={
            "mode": self.s.mode, "experiment": tag, "active": label, "changes": clean,
            "before": asdict(before), "after": asdict(after)})
        return {"ok": True, "experiment": tag, "active_experiments": label, "changed": {
            k: {"from": getattr(before, k), "to": getattr(after, k)} for k in clean}, "effective": asdict(after)}

    def reset(self, reasoning: str) -> dict[str, Any]:
        before = self.effective()
        self._write({}, {}, "")
        self.j.log_decision("reset_params", reasoning or "reset to .env baseline", snapshot={
            "mode": self.s.mode, "before": asdict(before), "after": asdict(self.s.limits)})
        return {"ok": True, "effective": asdict(self.s.limits)}

    def _write(self, overrides: dict[str, Any] | None, owners: dict[str, str], label: str) -> None:
        """One transaction: overrides (None = already written by the caller), owners and the label."""
        rows = [(KV_OWNERS, json.dumps(owners)), (KV_EXPERIMENT, label)]
        if overrides is not None:
            rows.append((KV_OVERRIDES, json.dumps(overrides)))
        for k, v in rows:
            self.j.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (self._k(k), v))
        self.j.db.commit()


def _label(owners: dict[str, str]) -> str:
    return JOIN.join(sorted(set(owners.values())))


def consistency_errors(lim: RiskLimits) -> list[str]:
    errs = []
    if lim.min_stop_distance_pct >= lim.max_stop_distance_pct:
        errs.append("min_stop_distance_pct must be < max_stop_distance_pct")
    if lim.dd_halve_pct >= lim.dd_stop_pct:
        errs.append("dd_halve_pct must be < dd_stop_pct")
    if lim.loss_streak_halve >= lim.loss_streak_pause:
        errs.append("loss_streak_halve must be < loss_streak_pause")
    if lim.max_position_notional > lim.max_total_notional:
        errs.append("max_position_notional must be <= max_total_notional")
    return errs


__all__ = ["ResearchParams", "ParamError", "ResearchBounds", "TUNABLE"]
