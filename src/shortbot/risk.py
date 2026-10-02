"""Pure pre-trade risk checks. Every order that opens exposure must pass `check_open_short` (an add to a winner:
`check_add`).

The limits passed in are the EFFECTIVE limits: the .env baseline plus any AI research overrides
(params.py). The checks themselves are not tunable; only their thresholds are."""

from __future__ import annotations

from dataclasses import dataclass

from .config import RiskLimits


@dataclass(frozen=True)
class ShortRequest:
    symbol: str
    notional: float        # USDT position size (price * amount)
    entry_price: float
    stop_price: float
    leverage: int
    quote_volume_24h: float
    maint: tuple[float, float] | None = None   # the symbol's bracket (maint. rate, amount): Exchanges.maint_margin


@dataclass(frozen=True)
class AddRequest:
    """An add to a winning short (PositionOps.add_to_winner), as check_add sees it."""
    symbol: str
    add_qty: float         # contracts to add, at exchange precision
    mark: float            # a market add fills about here
    stop_price: float      # the protective stop resting after the add
    qty: float             # contracts short now
    entry: float           # the position's average entry now
    leverage: int          # the position's (isolated) leverage; 0 = unknown
    initial_size: float    # contracts of the planned full entry (plan.full_size): the base of max_add_frac
    r1: float              # the trade's pinned 1R, USDT
    adds_done: int         # adds already made on this trade (journal)
    maint: tuple[float, float] | None = None   # the bracket of the position after the add: Exchanges.maint_margin


@dataclass(frozen=True)
class AccountState:
    equity: float                  # USDT wallet balance + unrealized PnL
    open_symbols: tuple[str, ...]
    total_open_notional: float
    day_pnl: float                 # Trader.account()'s day P&L: equity vs the 00:00 UTC mark, net of transfers


class RiskViolation(Exception):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


# Maintenance margin rate assumed when the symbol's leverage bracket is unknown (no keys, the read failed). A first
# bracket's rate is about half the initial margin at its max leverage (BTC 125x 0.4%, ETH 75x 0.65%, 50x 1%): 2.5%
# covers any symbol that allows 20x; the old 1% only those that allow 50x.
FALLBACK_MAINT_MARGIN_PCT = 2.5


def stop_distance_pct(entry: float, stop: float) -> float:
    return (stop - entry) / entry * 100


def bracket_for(brackets: list[dict], notional: float) -> tuple[float, float]:
    """(maintenance margin rate, maintenance amount `cum`) of the leverage bracket holding this notional: the last one
    whose notionalFloor <= notional (GET /fapi/v1/leverageBracket rows; ccxt parse_position_risk picks the same)."""
    rows = sorted(brackets, key=lambda b: float(b["notionalFloor"]))
    pick = next((b for b in reversed(rows) if notional >= float(b["notionalFloor"])), rows[0])
    return float(pick["maintMarginRatio"]), float(pick.get("cum") or 0)


def liquidation_price(side: int, qty: float, entry: float, wallet: float, mmr: float, cum: float = 0.0,
                      other_mm: float = 0.0, other_upnl: float = 0.0) -> float:
    """Binance's one-way-mode liquidation price (FAQ "How to Calculate Liquidation Price of USDⓈ-M Futures
    Contracts"): LP = (WB - TMM1 + UPNL1 + cumB - Side*Q*EP) / (Q*MMR - Side*Q). side 1 long, -1 short; isolated:
    wallet = the isolated margin and other_mm = other_upnl = 0."""
    return (wallet - other_mm + other_upnl + cum - side * qty * entry) / (qty * mmr - side * qty)


def est_liquidation_distance_pct(leverage: int, maint: tuple[float, float] | None = None,
                                 notional: float = 0.0) -> float:
    """% move against an isolated short opened at `leverage` (isolated margin = notional / leverage) before Binance
    liquidates it, at its bracket's (maintenance rate, amount) when known, else at FALLBACK_MAINT_MARGIN_PCT:
    (1 + 1/L + cum/N) / (1 + MMR) - 1."""
    mmr, cum = maint or (FALLBACK_MAINT_MARGIN_PCT / 100, 0.0)
    n = notional if notional > 0 else 1.0
    return (liquidation_price(-1, n, 1.0, n / leverage, mmr, cum if notional > 0 else 0.0) - 1) * 100


def max_leverage_for_stop(dist_pct: float, buffer_pct: float, cap: int, maint: tuple[float, float] | None = None,
                          notional: float = 0.0) -> int:
    """Highest leverage in [1, cap] whose estimated liquidation stays buffer_pct beyond a stop dist_pct away: the
    liquidation rule check_open_short applies. 0 when not even 1x does."""
    return next((lev for lev in range(int(cap), 0, -1)
                 if dist_pct <= est_liquidation_distance_pct(lev, maint, notional) - buffer_pct), 0)


def max_notional_for_risk(equity: float, entry: float, stop: float, limits: RiskLimits) -> float:
    """Largest notional whose loss at the stop stays within risk_per_trade_pct of equity."""
    dist = stop_distance_pct(entry, stop)
    if dist <= 0:
        return 0.0
    return min(equity * limits.risk_per_trade_pct / dist, limits.max_position_notional)


# Liquidity tiers (learnings #13/#17/#18): below ~$5M 24h volume costs eat any edge; between tier A and B trade at
# half risk. (tier, min 24h quote volume, min open interest USD, risk multiplier)
LIQ_TIERS = (("A", 20e6, 30e6, 1.0), ("B", 5e6, 10e6, 0.5))


def liquidity_tier(quote_volume: float | None, oi_value: float | None) -> tuple[str | None, float]:
    """(tier, risk multiplier); (None, 0) = too thin to short. Unknown OI is judged on volume alone."""
    for name, vol, oi, mult in LIQ_TIERS:
        if (quote_volume or 0) >= vol and (oi_value is None or oi_value >= oi):
            return name, mult
    return None, 0.0



def funding_mult(rate: float | None, interval_h: int | None) -> float:
    """Shorts paying funding (negative rate or the 1h capped interval) trade at half size (learning #22)."""
    return 0.5 if (rate is not None and rate < 0) or interval_h == 1 else 1.0


def check_open_short(req: ShortRequest, acct: AccountState, limits: RiskLimits) -> None:
    """Raise RiskViolation listing every rule broken; return None if the short is allowed."""
    reasons: list[str] = []
    dist = stop_distance_pct(req.entry_price, req.stop_price)

    if req.stop_price <= req.entry_price:
        reasons.append("stop must be ABOVE entry for a short")
    elif not limits.min_stop_distance_pct <= dist <= limits.max_stop_distance_pct:
        reasons.append(
            f"stop distance {dist:.2f}% outside "
            f"[{limits.min_stop_distance_pct}, {limits.max_stop_distance_pct}]%"
        )
    if not 1 <= req.leverage <= limits.max_leverage:
        reasons.append(f"leverage {req.leverage} outside [1, {limits.max_leverage}]")
    elif dist > 0:
        liq = est_liquidation_distance_pct(req.leverage, req.maint, req.notional)
        if dist > liq - limits.liq_buffer_pct:
            reasons.append(
                f"at {req.leverage}x liquidation is ~{liq:.1f}% away, but the stop is {dist:.1f}% away: you "
                f"would be liquidated before the stop (need {limits.liq_buffer_pct}% buffer). "
                f"Lower leverage or tighten the stop"
            )
    if req.notional <= 0:
        reasons.append("notional must be positive")
    if req.notional > limits.max_position_notional:
        reasons.append(f"notional {req.notional:.2f} > per-coin max {limits.max_position_notional}")
    if acct.total_open_notional + req.notional > limits.max_total_notional:
        reasons.append(
            f"total notional would be {acct.total_open_notional + req.notional:.2f} "
            f"> max {limits.max_total_notional}"
        )
    if req.symbol in acct.open_symbols:
        reasons.append(f"already short {req.symbol}; manage the existing position instead")
    elif len(acct.open_symbols) >= limits.max_positions:
        reasons.append(f"already at max {limits.max_positions} positions")
    if req.quote_volume_24h < limits.min_quote_volume_24h:
        reasons.append(
            f"24h volume {req.quote_volume_24h:,.0f} < liquidity floor {limits.min_quote_volume_24h:,.0f}"
        )
    if acct.equity <= 0:
        reasons.append("no equity")
    else:
        if dist > 0:
            loss_at_stop_pct = req.notional * dist / 100 / acct.equity * 100
            if loss_at_stop_pct > limits.risk_per_trade_pct + 1e-9:
                reasons.append(
                    f"loss at stop {loss_at_stop_pct:.2f}% of equity > {limits.risk_per_trade_pct}% "
                    f"(max notional {max_notional_for_risk(acct.equity, req.entry_price, req.stop_price, limits):.2f})"
                )
        start = acct.equity - acct.day_pnl   # start-of-day equity: the kill switch's and the governor's basis
        if start > 0 and acct.day_pnl / start * 100 <= -limits.daily_loss_limit_pct:
            reasons.append(f"daily loss limit {limits.daily_loss_limit_pct}% hit; no new shorts today")

    if reasons:
        raise RiskViolation(reasons)


def check_add(req: AddRequest, acct: AccountState, limits: RiskLimits) -> None:
    """Raise RiskViolation listing every rule an add to a winner breaks: the caps check_open_short applies to the new
    exposure, then the pyramiding rules (docs/RISK_GOVERNANCE.md), research params like every threshold here. The
    loss at the stop vs the pinned 1R and the governor's room are the caller's (they need the trade and governor)."""
    reasons: list[str] = []
    after = req.qty + req.add_qty
    if req.add_qty <= 0:
        reasons.append("add size must be positive (the notional is below one lot)")
    if after * req.mark > limits.max_position_notional + 1e-9:
        reasons.append(f"position notional would be {after * req.mark:.2f} > per-coin max "
                       f"{limits.max_position_notional:g}")
    if acct.total_open_notional + req.add_qty * req.mark > limits.max_total_notional + 1e-9:
        reasons.append(f"total notional would be {acct.total_open_notional + req.add_qty * req.mark:.2f} "
                       f"> max {limits.max_total_notional:g}")
    dist = stop_distance_pct(req.mark, req.stop_price)
    if dist < limits.min_stop_distance_pct:
        reasons.append(f"stop {req.stop_price:g} is {dist:.2f}% above price {req.mark:g}: inside "
                       f"min_stop_distance_pct {limits.min_stop_distance_pct:g}%")
    avg = (req.entry * req.qty + req.mark * req.add_qty) / after if after > 0 else req.mark
    from_avg = stop_distance_pct(avg, req.stop_price)
    if req.leverage < 1:
        reasons.append("leverage unknown: the liquidation distance can't be checked")
    elif req.leverage > limits.max_leverage:
        reasons.append(f"leverage {req.leverage} outside [1, {limits.max_leverage}]")
    elif from_avg > (liq := est_liquidation_distance_pct(req.leverage, req.maint, after * avg)) - \
            limits.liq_buffer_pct:
        reasons.append(f"at {req.leverage}x liquidation is ~{liq:.1f}% above the new average {avg:.6g}, but the stop "
                       f"is {from_avg:.1f}% above it (need {limits.liq_buffer_pct:g}% buffer): tighten the stop")
    profit_r = (req.entry - req.mark) * req.qty / req.r1 if req.r1 > 0 else 0.0
    if profit_r < limits.min_add_r - 1e-9:
        reasons.append(f"open profit {profit_r:.2f}R < min_add_r {limits.min_add_r:g}R (research param)")
    if req.add_qty > limits.max_add_frac * req.initial_size + 1e-9:
        reasons.append(f"add of {req.add_qty:g} > max_add_frac {limits.max_add_frac:g} x the initial size "
                       f"{req.initial_size:g} (research param)")
    if req.adds_done >= limits.max_adds:
        reasons.append(f"{req.adds_done} adds already on this trade: max_adds is {limits.max_adds} (research param)")
    if reasons:
        raise RiskViolation(reasons)
