"""Tape classifier: "which game is being played right now?" Maps signal features to a tape STATE, a STANCE and
the playbooks worth running. Declarative rules, first match wins, so new states are one line.
Rule order encodes priority: danger first (stand aside), then fade setups, then waiting states."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TapeRule:
    state: str
    when: Callable[[dict[str, Any]], bool]
    stance: str                     # stand_aside | fade | short_bounces | wait_for_trigger | no_edge
    playbooks: tuple[str, ...]
    why: str


def _f(f: dict[str, Any], k: str, default: Any = None) -> Any:
    v = f.get(k)
    return default if v is None else v


RULES: list[TapeRule] = [
    TapeRule("pump_in_progress", lambda f: f.get("pump_state") == "in_progress", "stand_aside", (),
             "rush orders still hitting: pumps peak within minutes; wait"),
    TapeRule("liquidation_cascade", lambda f: _f(f, "liq_long_15m_usd", 0) > 50_000
             and _f(f, "liq_long_15m_usd", 0) > 3 * max(_f(f, "liq_long_prior_15m_avg_usd", 0), 1),
             "stand_aside", (), "forced selling overshoots and gets bought back"),
    TapeRule("accepted_breakout", lambda f: f.get("sweep_state") == "accepted", "stand_aside", (),
             "stops cascaded through the level and price held: trend, not a trap"),
    TapeRule("spot_led_rally", lambda f: _f(f, "perp_ret_6h", 0) > 0 and _f(f, "spot_ret_6h", 0) >= _f(f, "perp_ret_6h", 0)
             and _f(f, "spot_ti_6h", 0) > 0.10 and _f(f, "basis_bp", 1) <= 0, "stand_aside", (),
             "real spot demand is leading"),
    TapeRule("attention_rising", lambda f: bool(f.get("mention_hot")) or bool(f.get("vol_hot")), "stand_aside", (),
             "attention/volume still accelerating: continuation risk"),
    TapeRule("pre_unlock", lambda f: f.get("unlock_days") is not None and 3 <= f["unlock_days"] <= 30
             and f.get("unlock_recipient") in ("team", "investor", "cliff") and (f.get("unlock_pct_supply") or 0) >= 2,
             "fade", ("unlock",), "supply shock ahead: pressure builds into the unlock; exit around it"),
    TapeRule("hype_fade", lambda f: bool(f.get("hype_fade")), "fade", ("pump_fade",),
             "attention peaked and is cooling while price is still extended"),
    TapeRule("failed_sweep", lambda f: f.get("sweep_state") == "failed_sweep", "fade",
             ("pump_fade", "exp_level_fade"),
             "liquidity above the level was taken and price closed back below: short the failure, stop above the wick"),
    TapeRule("post_pump_decay", lambda f: f.get("pump_state") == "decaying", "short_bounces",
             ("pump_fade",), "pump is fading below its VWAP: short bounces into it"),
    TapeRule("exhausting_push", lambda f: bool(f.get("cvd_divergence")) and bool(f.get("crowded_longs")),
             "wait_for_trigger", ("exp_level_fade",),
             "buyers weakening into crowded longs: rest limit sells above the highs"),
    TapeRule("crowded_longs", lambda f: bool(f.get("crowded_longs")), "wait_for_trigger", ("exp_level_fade",),
             "fuel for a flush, but no turn yet: limit entries at levels only"),
]


def classify(features: dict[str, Any]) -> dict[str, Any]:
    for r in RULES:
        try:
            if r.when(features):
                return {"state": r.state, "stance": r.stance, "playbooks": list(r.playbooks), "why": r.why}
        except (TypeError, ValueError):
            continue
    return {"state": "no_edge", "stance": "no_edge", "playbooks": [], "why": "no tape footprint of note"}
