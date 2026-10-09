"""Next-level check for an open position (docs/POSITION_MANAGEMENT.md "Next-level check", docs/PROBABILITY.md).

From the CURRENT price: the odds of the next unhit plan target before the current stop within the plan's remaining
time (24h without a time stop), the reward to that target against the give-back to the stop, both in the trade's
initial R, and the costs of holding: the exit fee and the funding the current rate charges until the expected exit.

It is the zero-drift baseline (geometry plus costs), NOT an edge estimate: under it the full EV of holding is minus the
costs. `ev_R_bracket` is target-vs-stop only (the paths that hit neither within the window are left out), so a
negative value says the remaining reward is small against what the stop gives back. Use it for exit risk-reward, never
to pick entries; it gates nothing until it passes the calibration gate (docs/PROBABILITY.md "Sizing gate"). Advice and
shadow logging only: `reassess` shows it, the ladder logs it, nothing trades from it (learning #97)."""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .probability.baseline import short_geometry, side_sign, touch_down, touch_up

log = logging.getLogger("shortbot.exit_odds")

TAKER, MAKER = 0.0005, 0.0002   # Binance USDⓈ-M regular tier, as backtest/sim.Costs (verified 2026-09)
DEFAULT_HOURS = 24.0             # horizon when the plan has no time stop
HOLD_STEPS = 40                  # trapezoid steps for the expected hold
LABEL = "NO-EDGE BASELINE (drift = 0): geometry plus costs, not an edge estimate"


def horizon_hours(plan: dict[str, Any], age_h: float) -> float:
    """The plan's remaining time (at least 1h), or 24h when it has no time stop."""
    tsh = plan.get("time_stop_hours")
    return max(1.0, float(tsh) - age_h) if tsh else DEFAULT_HOURS


def pick_target(side: str, price: float, plan_targets: Iterable[float], hit: Iterable[float]) -> float | None:
    """The nearest plan target not yet hit and still beyond the price in the trade's favour."""
    s, done = side_sign(side), set(hit)
    left = [t for t in plan_targets if t not in done and s * (price - t) > 0]
    return min(left, key=lambda t: abs(price - t)) if left else None


def funding_frac(side: str, rate: float | None, interval_h: float, hours: float) -> float:
    """Funding the position pays over `hours` if the current rate holds, as a fraction of notional (negative = it
    earns). A short pays when the rate is negative, a long when it is positive."""
    if not rate or hours <= 0:
        return 0.0
    return -side_sign(side) * rate * hours / max(interval_h, 1e-9)


def expected_hold(side: str, price: float, stop: float, target: float, sigma_h: float, hours: float,
                  steps: int = HOLD_STEPS) -> float:
    """E[min(exit time, horizon)] in hours = ∫ P(neither barrier hit by t) dt over the window (trapezoid rule on the
    same three-way baseline): the funding a position pays runs only until it exits. The integral stops at 10x the
    no-time-limit mean exit time (a·b/σ² in log-price), past which almost no path is left."""
    a, b = abs(math.log(stop / price)), abs(math.log(price / target))
    dt = min(hours, 10 * a * b / sigma_h**2) / steps
    alive = [1.0] + [short_geometry(price, stop, target, sigma_h, k * dt, side=side)["p_time"]
                     for k in range(1, steps + 1)]
    return sum((alive[k] + alive[k + 1]) / 2 for k in range(steps)) * dt


def next_level(side: str, price: float, stop: float | None, target: float | None, r_unit: float,
               sigma_h: float | None, hours: float, funding_rate: float | None,
               funding_interval_h: float | None) -> dict[str, Any] | None:
    """The block; None without a stop on the losing side of the price, a volatility estimate or a risk unit."""
    s = side_sign(side)
    if stop is None or not sigma_h or sigma_h <= 0 or r_unit <= 0 or s * (stop - price) <= 0:
        return None
    iv = float(funding_interval_h or 8)
    per_r = price / r_unit                           # a fraction of the price, in R
    giveback = s * (stop - price) / r_unit
    out: dict[str, Any] = {
        "label": LABEL, "side": side, "price": price, "target": target, "stop": stop, "hours": round(hours, 2),
        "sigma_hourly": sigma_h, "r_unit": r_unit, "giveback_R": round(giveback, 3), "funding_rate": funding_rate,
        "funding_interval_h": iv, "funding_interval_read": funding_interval_h is not None,
        "funding_R_to_horizon": round(funding_frac(side, funding_rate, iv, hours) * per_r, 3)}
    if target is None:   # nothing left to aim at: only the stop and the carry
        touch = touch_up if s == 1 else touch_down
        p_stop = touch(price, stop, sigma_h, hours)
        out.update({"p_target_first": None, "p_stop_first": round(p_stop, 4), "p_neither": round(1 - p_stop, 4),
                    "reward_R": None, "expected_reward_R": None, "ev_R_bracket": None,
                    "fees_R": round(TAKER * per_r, 3), "expected_hold_h": round(hours, 2),
                    "funding_R_expected": out["funding_R_to_horizon"], "ev_R_net": None,
                    "note": "no plan target left beyond the price: trail, or exit by the plan"})
        return out
    g = short_geometry(price, stop, target, sigma_h, hours, side=side)
    reward = s * (price - target) / r_unit
    hold_h = expected_hold(side, price, stop, target, sigma_h, hours)
    fees = (g["p_target"] * MAKER + (1 - g["p_target"]) * TAKER) * per_r   # targets rest (maker); stop/time: taker
    funding = funding_frac(side, funding_rate, iv, hold_h) * per_r
    bracket = g["p_target"] * reward - g["p_stop"] * giveback
    out.update({"p_target_first": round(g["p_target"], 4), "p_stop_first": round(g["p_stop"], 4),
                "p_neither": round(g["p_time"], 4), "reward_R": round(reward, 3),
                "expected_reward_R": round(g["p_target"] * reward, 3), "ev_R_bracket": round(bracket, 3),
                "fees_R": round(fees, 3), "expected_hold_h": round(hold_h, 2), "funding_R_expected": round(funding, 3),
                "ev_R_net": round(bracket - fees - funding, 3), "exit_now_R": round(-TAKER * per_r, 3)})
    return out


def for_trade(trade: dict[str, Any], price: float, stop: float | None, age_h: float, sigma_h: float | None,
              funding_rate: float | None, funding_interval_h: float | None,
              side: str = "short") -> dict[str, Any] | None:
    """The block for a journal trade: its next unhit plan target, its initial R per unit and its remaining time."""
    plan = trade.get("plan") or {}
    tgts = [float(t["price"]) for t in plan.get("targets") or [] if t.get("price")]
    entry, init = trade.get("entry_price"), trade.get("initial_stop")
    r_unit = abs(init - entry) if entry and init else abs((stop or price) - price)
    return next_level(side, price, stop, pick_target(side, price, tgts, trade.get("targets_hit") or []), r_unit,
                      sigma_h, horizon_hours(plan, age_h), funding_rate, funding_interval_h)


def actions(nl: dict[str, Any] | None, open_pnl_r: float, entry: float,
            structure: float | None = None) -> list[dict[str, Any]]:
    """`protect_profit` when the next level's EV net of costs is negative while the position is in profit;
    `funding_drag` when the funding expected until the exit exceeds the expected reward."""
    if not nl:
        return []
    out: list[dict[str, Any]] = []
    s, price, stop, ev = side_sign(nl["side"]), nl["price"], nl["stop"], nl["ev_R_net"]
    if ev is not None and ev < 0 and open_pnl_r > 0:
        inside = lambda x: x is not None and s * (stop - x) > 0 and s * (x - price) > 0  # noqa: E731
        be = entry if inside(entry) else None
        kw: dict[str, Any] = {"breakeven_stop": be, "structure_stop": structure if inside(structure) else None}
        if be is not None:
            alt = next_level(nl["side"], price, be, nl["target"], nl["r_unit"], nl["sigma_hourly"], nl["hours"],
                             nl["funding_rate"], nl["funding_interval_h"])
            kw["at_breakeven"] = {k: alt[k] for k in ("p_target_first", "p_stop_first", "giveback_R", "ev_R_net")} \
                if alt else None
        out.append({"action": "protect_profit", "why": (
            f"next level {nl['target']:g}: P(first) {nl['p_target_first']:.2f} for {nl['reward_R']:.2f}R vs "
            f"P(stop first) {nl['p_stop_first']:.2f} giving back {nl['giveback_R']:.2f}R; EV {ev:+.2f}R net of fees "
            f"and funding while {open_pnl_r:+.2f}R in profit: tighten to breakeven/structure or take a partial "
            "(no-edge baseline, advice only)"), **kw})
    fund, er = nl["funding_R_expected"], nl["expected_reward_R"]
    if fund > 0 and (er is None or fund > er):
        rate = nl["funding_rate"] or 0.0
        out.append({"action": "funding_drag", "why": (
            f"funding {rate * 100:.4f}% per {nl['funding_interval_h']:g}h costs ~{fund:.2f}R over the expected "
            f"{nl['expected_hold_h']:.1f}h hold, more than the expected reward "
            f"{'(no target left)' if er is None else f'{er:.2f}R'}: shorten the hold, take profit or exit"),
            "funding_R_expected": fund, "expected_reward_R": er})
    return out


def trade_horizon(trade: dict[str, Any], now: datetime | None = None) -> float:
    """horizon_hours for a journal trade, from its opening time."""
    opened = trade.get("opened_at")
    age = ((now or datetime.now(UTC)) - datetime.fromisoformat(opened)).total_seconds() / 3600 if opened else 0.0
    return horizon_hours(trade.get("plan") or {}, age)


async def market_inputs(ex: Any, symbol: str, journal: Any = None,
                        hours: float = DEFAULT_HOURS) -> tuple[Any, dict[str, Any], int | None]:
    """(DataHub, σ for the window (engine.sigma_hourly: the gated model times the applied σ scale), funding interval
    in hours) for the block; an unread input is empty/None (the block is then skipped, or the interval assumed 8h and
    flagged), never an error: reassess must answer."""
    from .exchange import to_raw
    from .market import DataHub
    from .probability.engine import sigma_hourly
    from .probability.recal import policy
    hub = DataHub(ex)
    sig: dict[str, Any] = {}
    iv: int | None = None
    try:
        scale, model = policy(journal)
        sig = await sigma_hourly(hub.context(symbol), hours, scale, model, har=True)
    except Exception as e:  # noqa: BLE001 - advice only: no sigma, no block
        log.warning("next-level sigma unread for %s (%s: %s)", symbol, type(e).__name__, e)
    try:
        iv = int((await ex.funding_intervals()).get(to_raw(symbol), {}).get("interval_h") or 8)
    except Exception as e:  # noqa: BLE001 - advice only: 8h assumed and flagged in the block
        log.warning("funding interval unread for %s (%s: %s)", symbol, type(e).__name__, e)
    return hub, sig, iv
