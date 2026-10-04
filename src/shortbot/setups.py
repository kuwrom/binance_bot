"""Setup registry. A setup = a named, researchable short thesis with a playbook (Skill), an auto-detector
for scan candidates, and a default TradePlan.

Growing the desk:
- New idea: trade it as `exp_<name>` (allowed without registration) and tag it in the journal.
- Once candidates/trades show an edge, promote it: add a Setup here + a Skill in .claude/skills/<skill>/.
- Retire a setup by setting status="retired" (it stays in history, new trades are refused).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Setup:
    name: str
    description: str
    skill: str
    default_plan: dict[str, Any]
    detect: Callable[[dict[str, Any]], bool] | None = None  # reads the signal features dict
    status: str = "active"          # active | experimental | retired
    evidence: list[str] = field(default_factory=list)


SETUPS: dict[str, Setup] = {s.name: s for s in [
    Setup(
        "pump_fade",
        "Fade an overextended futures top-gainer after a confirmed turn (lower high, VWAP loss, taker flip).",
        "pump-fade",
        # no auto trail: post-entry returns mean-revert, trailing adds nothing for fades (learning #20)
        {"time_stop_hours": 12, "breakeven_at_pct": 5, "review_every_hours": 2},
        detect=lambda f: (f.get("change_24h") or 0) >= 10 and not f.get("delisting_scheduled"),
        evidence=["FMZ pump-short workflow", "small-cap reversal (Finance Research Letters 2021)"],
    ),
    Setup(
        "listing_fade",
        "Short SPOT-LISTED new perps (HODLer / spot-at-launch) from ~day 7 and hold weeks; never Alpha / perp-only "
        "listings (40% of those run +100% first). Small size (C), wide stop, long time stop.",
        "listing-fade",
        {"time_stop_hours": 24 * 45, "breakeven_at_pct": 10, "trail_activate_pct": 20, "trail_pct": 10,
         "review_every_hours": 24},
        detect=lambda f: f.get("listing_age_days") is not None and 6 <= f["listing_age_days"] <= 30
        and f.get("spot_available") is True and not f.get("delisting_scheduled"),
        evidence=["docs/LIFECYCLE_STUDY.md: 267 listings, day-7 short held 60d: HODLer 91% win / spot-with-perp 83% "
                  "(+19-20% w/ 30% stop), Alpha/perp-only 70% win but -2.9% (learning #15)",
                  "github.com/Asalio123/binance-listing-study (470 listings, death-inclusive)",
                  "literature check learning #18: best-backed setup for this desk"],
    ),
    Setup(
        "delisting",
        "Short a perp after Binance schedules its delisting (deliveryDate set); expect squeezes, exit before "
        "settlement.",
        "delisting-short",
        {"time_stop_hours": 72, "breakeven_at_pct": 5, "review_every_hours": 4,
         "kill_conditions": ["exit before the reduce-only / settlement time in the announcement"]},
        detect=lambda f: bool(f.get("delisting_scheduled")),
        evidence=["FMZ delisting grid (single source)"],
    ),
    Setup(
        "breakdown",
        "Continuation short on a 24h futures loser that keeps making lower highs (momentum, not reversal). "
        "Squeeze risk: crowded shorts / negative funding (vetoed).",
        "pump-fade",
        # no auto trail: it cost -0.12R OOS on the breakdown/bounce family (learning #20)
        {"time_stop_hours": 24, "breakeven_at_pct": 3, "review_every_hours": 2},
        detect=lambda f: (f.get("change_24h") or 0) <= -8 and bool(f.get("lower_high")) and not f.get("delisting_scheduled"),
        status="experimental",
        evidence=["cross-sectional/time-series momentum in crypto (Liu, Tsyvinski & Wu 2022)"],
    ),
    Setup(
        "unlock",
        "Short 2-4 weeks into a large team/cliff token unlock; exit around the unlock. Unlock dates are "
        "entered manually (no free API).",
        "unlock-short",
        {"time_stop_hours": 24 * 28, "breakeven_at_pct": 8, "review_every_hours": 24},
        detect=lambda f: f.get("unlock_days") is not None and 3 <= f["unlock_days"] <= 30
        and f.get("unlock_recipient") in ("team", "investor", "cliff") and (f.get("unlock_pct_supply") or 0) >= 2,
        status="experimental",
        evidence=["Keyrock: 16k unlock events"],
    ),
]}


def resolve(name: str) -> Setup:
    """Return the registered setup, or an ad-hoc experimental one for `exp_*` names."""
    if name in SETUPS:
        s = SETUPS[name]
        if s.status == "retired":
            raise ValueError(f"setup {name!r} is retired")
        return s
    if name.startswith("exp_") and len(name) > 4:
        return Setup(name, "ad-hoc experimental setup", "research-review",
                     # breakeven, no trail: fades mean-revert after entry, trailing adds nothing (learning #20)
                     {"time_stop_hours": 24, "breakeven_at_pct": 3},
                     status="experimental")
    raise ValueError(f"unknown setup {name!r}: use one of {sorted(SETUPS)} or 'exp_<name>' for a new experiment")


def detect_all(features: dict[str, Any]) -> list[str]:
    return [s.name for s in SETUPS.values() if s.detect and s.status != "retired" and s.detect(features)]


def describe() -> list[dict[str, Any]]:
    return [{"name": s.name, "status": s.status, "description": s.description, "skill": s.skill,
             "default_plan": s.default_plan, "evidence": s.evidence} for s in SETUPS.values()]
