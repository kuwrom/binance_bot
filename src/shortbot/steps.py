"""Order sizes on a symbol's step grid (#65, #71). ccxt 4.5.84 base/exchange.py amount_to_precision truncates to the
step and raises InvalidOrder when the result is '0'; these answer instead, so a size under one step is the caller's own
refusal (closes: close_intent; targets: targets_sync; entries and adds: trading_open, entries_place, positioning) and
nothing is sent. min_size: the smallest new position a symbol takes (the demo probes and drills, #55 and #46)."""

from __future__ import annotations

import math

import ccxt.async_support as ccxt

MIN_MULT = 1.05   # min_size's margin over MIN_NOTIONAL: a small price move before the send keeps it above


def floor_amount(t: ccxt.Exchange, symbol: str, x: float) -> float:
    """x truncated to the symbol's step size; 0.0 below one step (where ccxt raises InvalidOrder)."""
    if x <= 0:
        return 0.0
    try:
        return float(t.amount_to_precision(symbol, x))
    except ccxt.InvalidOrder:
        return 0.0


def too_small(t: ccxt.Exchange, symbol: str, amount: float, price: float) -> str | None:
    """Why a new position's order of `amount` (floor_amount) at `price` can't be sent, or None: under one step, or under
    the symbol's MIN_NOTIONAL (Binance -4164; reduce-only orders are exempt, so not for closes)."""
    m = t.market(symbol)
    if amount <= 0:
        step = (m.get("precision") or {}).get("amount")
        return f"size too small: below one step{f' ({float(step):g})' if step else ''} of {symbol}"
    min_cost = m["limits"]["cost"]["min"] or 0
    if amount * price < min_cost:
        return f"size {amount * price:.2f} USDT below exchange minimum {min_cost}"
    return None


def min_size(t: ccxt.Exchange, symbol: str, price: float, mult: float = MIN_MULT, min_steps: int = 1) -> float:
    """The smallest new position on `symbol` at `price`: MIN_NOTIONAL (5 USDT when the market lists none) x `mult`,
    rounded UP to the step grid, at least `min_steps` steps (a plan whose targets must each rest one step)."""
    m = t.market(symbol)
    step = float(m["precision"]["amount"])
    min_cost = float(((m.get("limits") or {}).get("cost") or {}).get("min") or 5.0)
    steps = max(min_steps, math.ceil(round(min_cost * mult / price / step, 6)))
    return float(t.amount_to_precision(symbol, steps * step))
