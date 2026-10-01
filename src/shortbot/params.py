"""Research parameters: the AI analyst may tune risk limits; the always-on guardian may not.

Two layers
----------
1. Baseline (`RiskLimits.from_env()`): the fixed values from `.env`. The guardian on AWS only ever
   uses these; it never reads research overrides.
2. Research overrides (this module): an interactive Claude session can change any `RiskLimits`
   field with `set_risk_params` (MCP tool), e.g. try 5x leverage as an experiment. Every change needs
   a reason and an experiment name, is written to the journal, and trades opened under it are
   tagged with that experiment so the results can be compared later (`journal` -> by_experiment).

Overrides must stay inside `ResearchBounds` (outer envelope in `.env`, human-only). These are a
sanity rail against typos (e.g. 100x instead of 10x), not the strategy's limits. Overrides apply
in demo mode; in live mode they are ignored unless the human sets AI_PARAMS_IN_LIVE=true.
"""

from __future__ import annotations

import json
from dataclasses import asdict, fields, replace
from typing import Any

from .config import ResearchBounds, RiskLimits, Settings
from .journal import Journal

KV_OVERRIDES = "research_params"
KV_EXPERIMENT = "research_experiment"
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

    def overrides(self) -> dict[str, float]:
        raw = self.j.get(KV_OVERRIDES)
        return json.loads(raw) if raw else {}

    def experiment(self) -> str | None:
        return self.j.get(KV_EXPERIMENT) or None

    def effective(self) -> RiskLimits:
        base = self.s.limits
        if not self.active:
            return base
        ov = {k: v for k, v in self.overrides().items() if k in TUNABLE}
        return replace(base, **ov) if ov else base

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.s.mode, "overrides_active": self.active, "experiment": self.experiment(),
            "effective": asdict(self.effective()), "baseline_env": asdict(self.s.limits),
            "overrides": self.overrides(), "bounds": self.s.bounds.as_dict(),
            "note": "Guardian (AWS) always uses baseline_env. Change with set_risk_params; reset with "
                    "reset_risk_params.",
        }

    # ----------------------------------------------------------------- write
    def set(self, changes: dict[str, Any], reasoning: str, experiment: str) -> dict[str, Any]:
        if not self.active:
            raise ParamError("live mode: research overrides are disabled (human can set AI_PARAMS_IN_LIVE=true)")
        if not reasoning.strip() or not experiment.strip():
            raise ParamError("reasoning and experiment name are required (this is research: say what you test)")
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
        merged = {**self.overrides(), **clean}
        after = replace(self.s.limits, **merged)
        errors += consistency_errors(after)
        if errors:
            raise ParamError("; ".join(errors))
        self._save(merged, experiment.strip())
        self.j.log_decision("set_params", reasoning, snapshot={
            "experiment": experiment.strip(), "changes": clean,
            "before": asdict(before), "after": asdict(after)})
        return {"ok": True, "experiment": experiment.strip(), "changed": {
            k: {"from": getattr(before, k), "to": getattr(after, k)} for k in clean}, "effective": asdict(after)}

    def reset(self, reasoning: str) -> dict[str, Any]:
        before = self.effective()
        self._save({}, "")
        self.j.log_decision("reset_params", reasoning or "reset to .env baseline",
                            snapshot={"before": asdict(before), "after": asdict(self.s.limits)})
        return {"ok": True, "effective": asdict(self.s.limits)}

    def _save(self, overrides: dict[str, float], experiment: str) -> None:
        self.j.set(KV_OVERRIDES, json.dumps(overrides))
        self.j.set(KV_EXPERIMENT, experiment)


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
