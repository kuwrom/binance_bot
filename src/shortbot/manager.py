"""Per-trade plan execution, as a PURE function: (trade + live position + plan) -> actions.
The guardian applies the actions; tests can check the decisions without an exchange.

Only protective / pre-approved things are executed automatically (see TradePlan.auto); everything else
becomes an alert for the analyst and human."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .tape_math import clear_above_round, clear_below_round, round_zone

BREAKEVEN_OFFSET = 0.001  # stop just below entry so a breakeven exit covers fees
FUNDING_BUDGET_R = 0.5    # exit a non-working short once the funding it paid reaches half its risk
BE_MAX_GIVE_PCT = 0.5     # a breakeven stop may sit this far above entry to clear a round-number zone
ROUND_GAP_PCT = 1.0       # only round grids at least 1% apart count as levels for auto stops


@dataclass
class Action:
    kind: str                 # move_stop | close | alert | target_done
    reason: str
    price: float | None = None
    fraction: float | None = None      # of CURRENT position, for close
    target_price: float | None = None  # target being executed (recorded in targets_hit)
    urgent: bool = False
    cooldown_s: float = 3600
    key: str = ""                      # an alert's stable identity (its cooldown key): never PnL or age
    cum: float | None = None           # a target close: the share of the ORIGINAL size gone by this target


def excursions(trade: dict[str, Any], mark: float) -> dict[str, float]:
    entry = trade["entry_price"]
    pnl_pct = (entry - mark) / entry * 100
    return {"pnl_pct": pnl_pct,
            "mfe_pct": max(trade.get("mfe_pct") or 0.0, pnl_pct),
            "mae_pct": max(trade.get("mae_pct") or 0.0, -pnl_pct),
            "low_since_entry": min(trade.get("low_since_entry") or entry, mark)}


def _clear_round(cand: float, why: str, stop: float | None, entry: float) -> tuple[float, str]:
    """Auto stops never rest in a round-number zone (sweep study: price is pulled to the round and runs the stops
    just past it). Prefer just past the zone if that still tightens (and, for breakeven, gives back at most
    BE_MAX_GIVE_PCT); otherwise just under it."""
    lvl, up = round_zone(cand, ROUND_GAP_PCT), clear_above_round(cand, ROUND_GAP_PCT)
    limit = entry * (1 + BE_MAX_GIVE_PCT / 100) if why.startswith("breakeven") else float("inf")
    if (stop is None or up < stop) and up <= limit:
        return up, f"{why}; cleared above round {lvl:g}"
    return clear_below_round(cand, ROUND_GAP_PCT), f"{why}; kept under round {lvl:g}"


def plan_actions(trade: dict[str, Any], pos: dict[str, Any], now: datetime | None = None) -> list[Action]:
    plan = trade.get("plan") or {}
    auto = plan.get("auto") or {}
    entry, mark = trade["entry_price"], pos["mark"]
    stop = pos.get("exchange_stop")
    ex = excursions(trade, mark)
    pnl, acts = ex["pnl_pct"], []
    now = now or datetime.now(UTC)
    age_h = (now - datetime.fromisoformat(trade["opened_at"])).total_seconds() / 3600

    # funding budget (round 2/3, learning #22): shorts paying funding must earn it. Once funding paid reaches
    # FUNDING_BUDGET_R x 1R and price has not followed through, exit (auto unless plan.auto.funding_budget is False)
    paid = -(trade.get("funding") or 0.0)          # journal funding < 0 = paid by us
    r1 = plan.get("r1_usdt") or ((trade.get("initial_stop") or 0) - entry) * (trade.get("amount") or 0)
    if paid > 0 and r1 > 0 and paid >= FUNDING_BUDGET_R * r1 and pnl <= 0:
        why = f"funding budget: paid {paid:.2f} USDT >= {FUNDING_BUDGET_R}R ({r1:.2f}) with no follow-through"
        if auto.get("funding_budget", True):
            return [Action("close", why, fraction=1.0, urgent=True)]
        acts.append(Action("alert", why, urgent=True, cooldown_s=4 * 3600, key="funding budget"))

    # time stop
    tsh = plan.get("time_stop_hours")
    if tsh and age_h >= tsh:
        if auto.get("time_stop"):
            return [Action("close", f"time stop: {age_h:.1f}h >= {tsh}h", fraction=1.0)]
        acts.append(Action("alert", f"time stop reached ({age_h:.1f}h >= {tsh}h), PnL {pnl:+.1f}%. Plan says "
                           "exit if no follow-through.", urgent=True, cooldown_s=4 * 3600, key="time stop"))

    # targets: fractions are of the ORIGINAL position and CUMULATIVE. At a target, close only what the plan wants
    # gone by then minus what is already gone (manual partials count), so a manual partial never double-counts and
    # the runner survives for the later targets and the trail (lesson: ONDO #5 lost its runner this way). Automatic
    # targets rest on the book as reduce-only limits (targets.py, #65); a close here is the market fallback for one the
    # mark crossed without its limit having filled (gone from the book, refused, part-filled): what today did.
    hit = set(trade.get("targets_hit") or [])
    orig, cur = trade.get("amount") or pos["contracts"], pos["contracts"]
    cum = 0.0
    for t in sorted(plan.get("targets") or [], key=lambda t: -t["price"]):
        cum = min(1.0, cum + t["fraction"])
        if t["price"] in hit or mark > t["price"]:
            continue
        to_close = cum * orig - (orig - cur)
        if to_close <= orig * 0.01:   # manual partials count, auto or not: no nagging for a target already taken
            acts.append(Action("target_done", f"target {t['price']} reached: already taken by earlier exits",
                               target_price=t["price"]))
            continue
        if auto.get("targets"):
            frac = 1.0 if cum >= 1.0 else min(1.0, to_close / cur) if cur else 1.0
            acts.append(Action("close", f"target {t['price']} hit", fraction=frac, target_price=t["price"], cum=cum))
            cur -= cur * frac
        else:
            acts.append(Action("alert", f"target {t['price']} reached (PnL {pnl:+.1f}%): take "
                               f"{t['fraction']:.0%} per plan?", target_price=t["price"], cooldown_s=4 * 3600,
                               key=f"target {t['price']:g}"))

    # stop management: breakeven and trailing, tighten-only and must stay above mark
    new_stop, why = None, ""
    be = plan.get("breakeven_at_pct")
    if be and auto.get("breakeven") and pnl >= be:
        cand = entry * (1 - BREAKEVEN_OFFSET)
        if stop is None or cand < stop:
            new_stop, why = cand, f"breakeven: profit {pnl:.1f}% >= {be}%"
    ta, tp = plan.get("trail_activate_pct"), plan.get("trail_pct")
    if ta and tp and auto.get("trail") and ex["mfe_pct"] >= ta:
        cand = ex["low_since_entry"] * (1 + tp / 100)
        if (stop is None or cand < stop) and (new_stop is None or cand < new_stop):
            new_stop, why = cand, f"trail {tp}% above low {ex['low_since_entry']:.6g} (mfe {ex['mfe_pct']:.1f}%)"
    if new_stop is not None and round_zone(new_stop, ROUND_GAP_PCT) is not None:
        new_stop, why = _clear_round(new_stop, why, stop, entry)
    if new_stop is not None and new_stop > mark * 1.002:
        acts.append(Action("move_stop", why, price=new_stop))

    # review cadence
    rv = plan.get("review_every_hours")
    if rv:
        acts.append(Action("alert", f"review due (every {rv}h): {trade.get('setup')} short, age {age_h:.1f}h, "
                           f"PnL {pnl:+.1f}%, MFE {ex['mfe_pct']:.1f}%, MAE {ex['mae_pct']:.1f}%",
                           cooldown_s=rv * 3600, key="review due"))
    return acts
