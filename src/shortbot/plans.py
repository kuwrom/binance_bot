"""TradePlan: how ONE specific short is managed. Written by the analyst at entry (defaults come from the
setup), approved by the human together with the order, stored on the trade, and executed/monitored by
the guardian. This is what makes management dynamic per trade instead of one global rule set.

Mechanical rules the guardian may execute itself (each gated by `auto`):
- breakeven: once profit >= breakeven_at_pct, move the stop to entry (tighten-only, protective)
- trail: once profit >= trail_activate_pct, keep the stop trail_pct above the lowest price since entry
- targets: take `fraction` of the position at each target price
- time_stop: close if the trade is older than time_stop_hours
Anything with auto=False only produces an alert for the analyst/human.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass
class Target:
    price: float
    fraction: float  # of the ORIGINAL position, 0 < f <= 1


@dataclass
class TradePlan:
    trigger: str = ""                       # what confirmed the entry
    invalidation: str = ""                  # what proves the thesis wrong (beyond the stop)
    targets: list[Target] = field(default_factory=list)
    time_stop_hours: float | None = None
    breakeven_at_pct: float | None = None
    trail_activate_pct: float | None = None
    trail_pct: float | None = None
    review_every_hours: float = 4.0
    kill_conditions: list[str] = field(default_factory=list)   # qualitative, for the analyst
    auto: dict[str, bool] = field(default_factory=lambda: {
        "breakeven": True, "trail": True, "targets": False, "time_stop": False})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


PLAN_FIELDS = {f.name for f in fields(TradePlan)}


def build_plan(defaults: dict[str, Any], overrides: dict[str, Any] | None) -> TradePlan:
    """Setup defaults, then analyst overrides. Unknown keys are rejected so typos don't silently vanish."""
    merged = {**defaults, **(overrides or {})}
    unknown = set(merged) - PLAN_FIELDS
    if unknown:
        raise ValueError(f"unknown plan fields {sorted(unknown)}; allowed: {sorted(PLAN_FIELDS)}")
    auto = {**TradePlan().auto, **merged.pop("auto", {})}
    targets = [t if isinstance(t, Target) else Target(float(t["price"]), float(t["fraction"]))
               for t in merged.pop("targets", [])]
    return TradePlan(**merged, targets=targets, auto=auto)


def validate_plan(plan: TradePlan, entry: float, stop: float) -> list[str]:
    errs: list[str] = []
    total = 0.0
    for t in plan.targets:
        if not 0 < t.fraction <= 1:
            errs.append(f"target {t.price}: fraction must be in (0, 1]")
        if t.price >= entry:
            errs.append(f"target {t.price} must be below entry {entry} for a short")
        total += t.fraction
    if total > 1 + 1e-9:
        errs.append(f"target fractions sum to {total:.2f} > 1")
    for name in ("time_stop_hours", "breakeven_at_pct", "trail_activate_pct", "trail_pct"):
        v = getattr(plan, name)
        if v is not None and v <= 0:
            errs.append(f"{name} must be positive")
    if (plan.trail_pct is None) != (plan.trail_activate_pct is None):
        errs.append("trail_pct and trail_activate_pct go together")
    if stop <= entry:
        errs.append("stop must be above entry")
    return errs
