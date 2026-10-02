"""Event-driven hourly simulator for ONE trade with a plan (docs/BACKTEST.md): a short by default, a long when
`Plan.side == "long"` (research only, task #17: every rule below is mirrored, e.g. a long's stop sits below entry,
its targets above, it PAYS positive funding and its market entry buys at close + slippage). Bars are aligned perp rows
(patterns.align): {"ts" (bar OPEN, ms), "o","h","l","c","qv", ...}. A signal is known at the CLOSE of bar i.

Conventions (all conservative, fixed before any result was seen):
- Market entry: filled at the close of the signal bar, minus slippage, taker fee.
- Limit entry (sell above the market): fills only if a later bar trades THROUGH the price (high > limit), at the
  limit, maker fee, within `limit_ttl` bars. In the fill bar only the stop is checked (worst case: the stop can be
  hit after the fill, a target cannot be credited because the intrabar order is unknown).
- Sell-stop entry (round 2, entry="stop", trigger = limit_px below the close): fills in the first bar within
  `limit_ttl` whose low trades below the trigger, after a bar reached `arm_px` (the bounce attempt, if set); at the
  trigger or the bar open if it gapped below, taker fee + `stop_slip_mult` x slippage. A bar reaching the stop first
  cancels the order. In the fill bar only the stop is checked (as for limits).
- Funding budget (round 2, `fund_budget_r`): exit at a bar's close once funding paid >= that many R while the open
  price gain is below `fund_follow_r` R.
- Stop (reduce-only stop-market): if the bar OPENS past it, the fill is the open (gap); else the stop price. Stop
  fills pay taker fee + `stop_slip_mult` x slippage. Checked BEFORE targets: stop and target in one bar = stop.
- Targets: resting reduce-only limits, maker fee, no slippage, (price, fraction of the initial size).
- Breakeven / trailing are updated from a bar's low only AFTER that bar is done (effective next bar).
- Time stop: close of the bar `time_stop` bars after entry, taker + slippage.
- Funding: every print with entry < ts <= exit is paid/received on the fraction then open, at the bar open price
  (a short RECEIVES positive funding). A close-time decision executes after that hour's funding print.
- Slippage bps = clip(k / sqrt(24h quote volume in $M), min, max): thin books cost more.
PnL is in % of the initial notional; R = pnl / planned risk ((stop - entry) / entry)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

H = 3_600_000


@dataclass(frozen=True)
class Costs:
    taker_bps: float = 5.0          # Binance USDⓈ-M regular tier (verified 2026-09: 0.05% taker / 0.02% maker)
    maker_bps: float = 2.0
    slip_k_bps: float = 20.0        # 20 bp at $1M/24h, 4.5 bp at $20M, 63 bp at $0.1M
    slip_min_bps: float = 1.0
    slip_max_bps: float = 150.0
    stop_slip_mult: float = 2.0     # stop-markets fire into a moving book
    stop_overshoot: float = 0.0     # stress: extra fill = this share of how far the bar ran past the stop
    funding: bool = True

    def slip(self, vol24_usd: float) -> float:
        """Slippage as a fraction of price for a market order on a coin with this 24h quote volume."""
        m = max(vol24_usd / 1e6, 1e-6)
        return min(self.slip_max_bps, max(self.slip_min_bps, self.slip_k_bps / m ** 0.5)) / 1e4

    def charge(self, kind: str) -> float:
        """Slippage simulate() charges a fill of `kind`, in multiples of slip(): the one table fill_report.py compares
        live fills with (tests pin it to simulate()). A market entry and a time / eod / funding-budget exit (exit/time)
        1x, a limit entry and a target (resting, maker) 0, a stop entry and a stop exit stop_slip_mult x."""
        return {"entry/market": 1.0, "exit/time": 1.0, "entry/limit": 0.0, "exit/target": 0.0,
                "entry/stop": self.stop_slip_mult, "exit/stop": self.stop_slip_mult}[kind]


COSTS = Costs()
FREE = Costs(taker_bps=0, maker_bps=0, slip_k_bps=0, slip_min_bps=0, funding=False)


@dataclass(frozen=True)
class Plan:
    stop: float                                   # absolute price above entry
    targets: tuple[tuple[float, float], ...] = ()  # (price, fraction of the INITIAL size), fractions sum <= 1
    time_stop: int = 24                            # bars after the entry bar
    entry: str = "market"                          # market | limit
    limit_px: float | None = None
    limit_ttl: int = 6
    be_r: float | None = None                      # MFE >= be_r * R -> stop to entry
    trail_act_r: float | None = None               # MFE >= trail_act_r * R -> trail ...
    trail_dist_r: float = 1.0                      # ... at lowest low + trail_dist_r * R
    meta: dict[str, Any] = field(default_factory=dict, compare=False)
    arm_px: float | None = None                    # entry="stop": a bar must trade >= this before the trigger
    fund_budget_r: float | None = None             # exit at a close once funding PAID >= this many R ...
    fund_follow_r: float = 0.5                     # ... while the open price gain is < this many R
    side: str = "short"                            # short | long (research: the desk trades shorts only)


def vol24(rows: list[dict], i: int) -> float:
    return sum(r["qv"] for r in rows[max(0, i - 23):i + 1])


def _funding_index(rows: list[dict], funding: list | None) -> dict[int, float]:
    """bar index -> funding rate printed at that bar's OPEN (Binance prints carry a few ms of jitter)."""
    if not funding:
        return {}
    pos = {r["ts"]: k for k, r in enumerate(rows)}
    out = {}
    for ts, rate in funding:
        k = pos.get(int(round(ts / 60_000)) * 60_000)
        if k is not None:
            out[k] = float(rate)
    return out


def simulate(rows: list[dict], i: int, plan: Plan, funding: list | None = None, costs: Costs = COSTS,
             fidx: dict[int, float] | None = None, vol24_usd: float | None = None) -> dict[str, Any] | None:
    """Simulate a trade signalled at the close of bar i. None if the plan is invalid; {"filled": False} if a
    resting entry never filled. `vol24_usd` overrides the 24h volume behind the slippage (sub-hourly rows, trail5m).
    Written once for both sides with s = +1 (short) / -1 (long): for a short every expression reduces to the
    original short-only code."""
    n = len(rows)
    if i >= n - 1 or plan.side not in ("short", "long"):
        return None
    s = 1 if plan.side == "short" else -1
    adv = (lambda b: b["h"]) if s > 0 else (lambda b: b["l"])     # the bar extreme that hurts the position
    fav = (lambda b: b["l"]) if s > 0 else (lambda b: b["h"])     # ... and the one that helps it
    worse, better = (max, min) if s > 0 else (min, max)           # for the position: higher hurts a short
    slip = costs.slip(vol24(rows, i) if vol24_usd is None else vol24_usd)
    if plan.entry == "market":
        fill_i, entry, fee_in, first = i, rows[i]["c"] * (1 - s * slip), costs.taker_bps / 1e4, i + 1
        slip_cost = slip
    elif plan.entry == "stop":      # stop entry beyond the market: breakdown (short) / breakout (long) resumption
        trig = plan.limit_px
        if not trig or s * (rows[i]["c"] - trig) <= 0:
            return None
        armed, fill_i = plan.arm_px is None, None
        for j in range(i + 1, min(n, i + 1 + plan.limit_ttl)):
            if armed and s * (trig - fav(rows[j])) > 0:
                fill_i = j
                break
            if s * (adv(rows[j]) - plan.stop) >= 0:    # invalidated before the trigger: order cancelled
                break
            armed = armed or s * (adv(rows[j]) - plan.arm_px) >= 0
        if fill_i is None:
            return {"filled": False, "signal_i": i}
        entry = better(trig, rows[fill_i]["o"]) * (1 - s * costs.stop_slip_mult * slip)   # stop-market, gap-aware
        fee_in, first, slip_cost = costs.taker_bps / 1e4, fill_i, costs.stop_slip_mult * slip
    else:
        lim = plan.limit_px
        if not lim or s * (lim - rows[i]["c"]) <= 0:
            return None
        fill_i = next((j for j in range(i + 1, min(n, i + 1 + plan.limit_ttl)) if s * (adv(rows[j]) - lim) > 0),
                      None)
        if fill_i is None:
            return {"filled": False, "signal_i": i}
        entry, fee_in, first, slip_cost = lim, costs.maker_bps / 1e4, fill_i, 0.0
    stop0 = plan.stop
    if not entry or s * (stop0 - entry) <= 0:
        return None
    risk = s * (stop0 - entry) / entry
    fidx = fidx if fidx is not None else (_funding_index(rows, funding) if costs.funding else {})
    stop, stop_kind = stop0, "stop"
    rem, gross, fees, fund = 1.0, 0.0, fee_in, 0.0
    pend = sorted(plan.targets, key=lambda t: -s * t[0])  # nearest target first
    best, worst, overrun = entry, entry, 0.0   # overrun = how far (in R) the stop bar ran past entry: squeeze size
    exit_i, reason = None, None
    last = min(n - 1, fill_i + plan.time_stop)
    f_from = fill_i + (2 if plan.entry == "market" else 1)   # a close-time entry comes right after that print

    def close(frac: float, px: float, fee_bps: float) -> None:
        nonlocal rem, gross, fees
        gross += frac * s * (entry - px) / entry
        fees += frac * fee_bps / 1e4 * px / entry
        rem -= frac

    for j in range(first, last + 1):
        b = rows[j]
        if costs.funding and j >= f_from and j in fidx and rem > 1e-9:
            fund += s * rem * fidx[j] * b["o"] / entry          # a short receives positive funding, a long pays it
        in_fill_bar = plan.entry != "market" and j == fill_i
        if not in_fill_bar and s * (b["o"] - stop) >= 0:
            overrun = s * (adv(b) - entry) / entry / risk
            px = b["o"] * (1 + s * costs.stop_slip_mult * slip)
            slip_cost += rem * costs.stop_slip_mult * slip
            worst = worse(worst, b["o"])
            close(rem, px, costs.taker_bps)
            exit_i, reason = j, f"{stop_kind}_gap"
            break
        if s * (adv(b) - stop) >= 0:
            overrun = s * (adv(b) - entry) / entry / risk
            px = (stop + costs.stop_overshoot * (adv(b) - stop)) * (1 + s * costs.stop_slip_mult * slip)
            slip_cost += rem * costs.stop_slip_mult * slip
            worst = worse(worst, stop)
            close(rem, px, costs.taker_bps)
            exit_i, reason = j, stop_kind
            break
        worst = worse(worst, adv(b))
        if not in_fill_bar:
            while pend and s * (pend[0][0] - fav(b)) >= 0 and rem > 1e-9:
                tp, frac = pend.pop(0)
                close(min(frac, rem), tp, costs.maker_bps)
            best = better(best, fav(b))
            if rem <= 1e-9:
                exit_i, reason = j, "target"
                break
        mfe_r = s * (entry - best) / entry / risk
        if plan.be_r is not None and mfe_r >= plan.be_r and s * (stop - entry) > 0:
            stop, stop_kind = entry, "breakeven"
        if plan.trail_act_r is not None and mfe_r >= plan.trail_act_r:
            t = best * (1 + s * plan.trail_dist_r * risk)
            if s * (stop - t) > 0:
                stop, stop_kind = t, "trail"
        budget = (plan.fund_budget_r is not None and -fund / risk >= plan.fund_budget_r
                  and s * (entry - b["c"]) / entry / risk < plan.fund_follow_r)
        if j == last or budget:
            reason = "fund_budget" if budget and j != last else "time" if j == fill_i + plan.time_stop else "eod"
            if costs.funding and j + 1 in fidx:   # the close-time exit executes after this hour's print
                fund += s * rem * fidx[j + 1] * b["c"] / entry
            px = b["c"] * (1 + s * slip)
            slip_cost += rem * slip
            close(rem, px, costs.taker_bps)
            exit_i = j
            break
    if exit_i is None:   # fill bar was the last bar of data
        exit_i, reason = fill_i, "eod"
        close(rem, rows[fill_i]["c"] * (1 + s * slip), costs.taker_bps)
    pnl = gross - fees + fund
    return {"filled": True, "side": plan.side, "signal_i": i, "entry_i": fill_i, "exit_i": exit_i,
            "entry_ts": rows[fill_i]["ts"],
            "entry_px": entry, "stop0": stop0, "risk_pct": risk * 100, "bars": exit_i - fill_i, "reason": reason,
            "pnl_pct": pnl * 100, "gross_pct": gross * 100, "fees_pct": fees * 100, "funding_pct": fund * 100,
            "slip_pct": slip_cost * 100, "r": pnl / risk, "mae_pct": s * (worst - entry) / entry * 100,
            "mfe_pct": s * (entry - best) / entry * 100, "mae_r": s * (worst - entry) / entry / risk,
            "mfe_r": s * (entry - best) / entry / risk, "overrun_r": overrun}
