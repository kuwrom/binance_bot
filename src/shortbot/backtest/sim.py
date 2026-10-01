"""Event-driven hourly simulator for ONE short with a plan (docs/BACKTEST.md). Bars are aligned perp rows
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
    """Simulate a short signalled at the close of bar i. None if the plan is invalid; {"filled": False} if a
    limit never filled. `vol24_usd` overrides the 24h volume behind the slippage (sub-hourly rows, trail5m)."""
    n = len(rows)
    if i >= n - 1:
        return None
    slip = costs.slip(vol24(rows, i) if vol24_usd is None else vol24_usd)
    if plan.entry == "market":
        fill_i, entry, fee_in, first = i, rows[i]["c"] * (1 - slip), costs.taker_bps / 1e4, i + 1
        slip_cost = slip
    elif plan.entry == "stop":      # sell-stop below the market: breakdown resumption (round 2)
        trig = plan.limit_px
        if not trig or trig >= rows[i]["c"]:
            return None
        armed, fill_i = plan.arm_px is None, None
        for j in range(i + 1, min(n, i + 1 + plan.limit_ttl)):
            if armed and rows[j]["l"] < trig:
                fill_i = j
                break
            if rows[j]["h"] >= plan.stop:           # invalidated before the trigger: order cancelled
                break
            armed = armed or rows[j]["h"] >= plan.arm_px
        if fill_i is None:
            return {"filled": False, "signal_i": i}
        entry = min(trig, rows[fill_i]["o"]) * (1 - costs.stop_slip_mult * slip)   # stop-market into a falling book
        fee_in, first, slip_cost = costs.taker_bps / 1e4, fill_i, costs.stop_slip_mult * slip
    else:
        lim = plan.limit_px
        if not lim or lim <= rows[i]["c"]:
            return None
        fill_i = next((j for j in range(i + 1, min(n, i + 1 + plan.limit_ttl)) if rows[j]["h"] > lim), None)
        if fill_i is None:
            return {"filled": False, "signal_i": i}
        entry, fee_in, first, slip_cost = lim, costs.maker_bps / 1e4, fill_i, 0.0
    stop0 = plan.stop
    if not entry or stop0 <= entry:
        return None
    risk = (stop0 - entry) / entry
    fidx = fidx if fidx is not None else (_funding_index(rows, funding) if costs.funding else {})
    stop, stop_kind = stop0, "stop"
    rem, gross, fees, fund = 1.0, 0.0, fee_in, 0.0
    pend = sorted(plan.targets, key=lambda t: -t[0])  # nearest (highest) target first
    lo, hi, overrun = entry, entry, 0.0     # overrun = how far (in R) the stop bar ran past entry: squeeze size
    exit_i, reason = None, None
    last = min(n - 1, fill_i + plan.time_stop)
    f_from = fill_i + (2 if plan.entry == "market" else 1)   # a close-time entry comes right after that print

    def close(frac: float, px: float, fee_bps: float) -> None:
        nonlocal rem, gross, fees
        gross += frac * (entry - px) / entry
        fees += frac * fee_bps / 1e4 * px / entry
        rem -= frac

    for j in range(first, last + 1):
        b = rows[j]
        if costs.funding and j >= f_from and j in fidx and rem > 1e-9:
            fund += rem * fidx[j] * b["o"] / entry
        in_fill_bar = plan.entry != "market" and j == fill_i
        if not in_fill_bar and b["o"] >= stop:
            overrun = (b["h"] - entry) / entry / risk
            px = b["o"] * (1 + costs.stop_slip_mult * slip)
            slip_cost += rem * costs.stop_slip_mult * slip
            hi = max(hi, b["o"])
            close(rem, px, costs.taker_bps)
            exit_i, reason = j, f"{stop_kind}_gap"
            break
        if b["h"] >= stop:
            overrun = (b["h"] - entry) / entry / risk
            px = (stop + costs.stop_overshoot * (b["h"] - stop)) * (1 + costs.stop_slip_mult * slip)
            slip_cost += rem * costs.stop_slip_mult * slip
            hi = max(hi, stop)
            close(rem, px, costs.taker_bps)
            exit_i, reason = j, stop_kind
            break
        hi = max(hi, b["h"])
        if not in_fill_bar:
            while pend and b["l"] <= pend[0][0] and rem > 1e-9:
                tp, frac = pend.pop(0)
                close(min(frac, rem), tp, costs.maker_bps)
            lo = min(lo, b["l"])
            if rem <= 1e-9:
                exit_i, reason = j, "target"
                break
        mfe_r = (entry - lo) / entry / risk
        if plan.be_r is not None and mfe_r >= plan.be_r and entry < stop:
            stop, stop_kind = entry, "breakeven"
        if plan.trail_act_r is not None and mfe_r >= plan.trail_act_r:
            t = lo * (1 + plan.trail_dist_r * risk)
            if t < stop:
                stop, stop_kind = t, "trail"
        budget = (plan.fund_budget_r is not None and -fund / risk >= plan.fund_budget_r
                  and (entry - b["c"]) / entry / risk < plan.fund_follow_r)
        if j == last or budget:
            reason = "fund_budget" if budget and j != last else "time" if j == fill_i + plan.time_stop else "eod"
            if costs.funding and j + 1 in fidx:   # the close-time exit executes after this hour's print
                fund += rem * fidx[j + 1] * b["c"] / entry
            px = b["c"] * (1 + slip)
            slip_cost += rem * slip
            close(rem, px, costs.taker_bps)
            exit_i = j
            break
    if exit_i is None:   # fill bar was the last bar of data
        exit_i, reason = fill_i, "eod"
        close(rem, rows[fill_i]["c"] * (1 + slip), costs.taker_bps)
    pnl = gross - fees + fund
    return {"filled": True, "signal_i": i, "entry_i": fill_i, "exit_i": exit_i, "entry_ts": rows[fill_i]["ts"],
            "entry_px": entry, "stop0": stop0, "risk_pct": risk * 100, "bars": exit_i - fill_i, "reason": reason,
            "pnl_pct": pnl * 100, "gross_pct": gross * 100, "fees_pct": fees * 100, "funding_pct": fund * 100,
            "slip_pct": slip_cost * 100, "r": pnl / risk, "mae_pct": (hi - entry) / entry * 100,
            "mfe_pct": (entry - lo) / entry * 100, "mae_r": (hi - entry) / entry / risk,
            "mfe_r": (entry - lo) / entry / risk, "overrun_r": overrun}
