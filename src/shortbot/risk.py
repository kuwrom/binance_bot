"""Pure pre-trade risk checks. Every order that opens exposure must pass `check_open_short`.

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


@dataclass(frozen=True)
class AccountState:
    equity: float                  # USDT wallet balance + unrealized PnL
    open_symbols: tuple[str, ...]
    total_open_notional: float
    day_pnl: float                 # realized + unrealized since 00:00 UTC


class RiskViolation(Exception):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


# Conservative maintenance-margin assumption for the liquidation estimate (Binance tiers start ~0.4-1%).
ASSUMED_MAINT_MARGIN_PCT = 1.0


def stop_distance_pct(entry: float, stop: float) -> float:
    return (stop - entry) / entry * 100


def est_liquidation_distance_pct(leverage: int) -> float:
    """Approx. % move against an isolated short before liquidation: 1/leverage minus maintenance margin."""
    return 100.0 / leverage - ASSUMED_MAINT_MARGIN_PCT


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
        liq = est_liquidation_distance_pct(req.leverage)
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
        if acct.day_pnl / acct.equity * 100 <= -limits.daily_loss_limit_pct:
            reasons.append(f"daily loss limit {limits.daily_loss_limit_pct}% hit; no new shorts today")

    if reasons:
        raise RiskViolation(reasons)
