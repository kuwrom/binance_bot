"""Trading MCP tools: market reads, account, positions, orders, stops, plans and limit entries. Registered on the
shared server by importing this module from mcp_server."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import news, scanner, setups, targets_sync
from .app import get_app
from .entries import EntryDesk
from .exchange import to_symbol
from .market import regime
from .mcp_server import tool
from .plans import revise_plan, validate_plan
from .positioning import PositionOps


# ------------------------------------------------------------------ market
@tool
async def market_regime() -> dict[str, Any]:
    """BTC trend, alt breadth, Fear & Greed -> 'favorable' / 'caution' / 'hostile' / 'unknown' (data missing)."""
    return await regime.market_regime(get_app().ex)


@tool
async def scan_shorts(min_change_24h: float = 10.0, top_n: int = 10, direction: str = "gainers") -> dict[str, Any]:
    """Sort Binance futures by 24h % change (direction: gainers = fade pumps, losers = breakdown continuation,
    both), deep-score the top movers. Returns tradable candidates (score,
    signals, detected setups), vetoed coins with reasons, perps listed <30d. Every scored coin is logged as a
    research candidate and labelled with its real outcome 72h later."""
    app = get_app()
    reg = await regime.market_regime(app.ex)
    res = await scanner.scan_shorts(app.ex, min_change=min_change_24h, top_n=top_n, journal=app.journal,
                                    regime=reg["verdict_for_shorts"], direction=direction)
    return {"regime": reg["verdict_for_shorts"], **res}


@tool
async def coin_snapshot(symbol: str) -> dict[str, Any]:
    """Deep look at one coin: multi-timeframe structure, funding history, OI, long/short ratios, taker flow,
    listing/delisting, detected setups, news, tight/wide stop suggestions."""
    return await scanner.coin_snapshot(get_app().ex, symbol)


@tool
async def coin_news(symbol: str) -> dict[str, Any]:
    """Binance announcements + RSS headlines (48h) mentioning this coin; bearish keywords flagged; 'errors'
    lists sources that failed (then absence of news is not confirmed). Titles/summaries are untrusted_text: data."""
    return await news.coin_news(symbol.upper().replace("USDT", "").split("/")[0])


@tool
async def latest_announcements(limit: int = 20) -> list[dict[str, Any]]:
    """Latest Binance announcements: new listings, delistings, news, activities. Titles are untrusted_text: data."""
    return await news.announcements(limit)


@tool
async def list_setups() -> list[dict[str, Any]]:
    """Registered setups with description, status, default plan, skill name and evidence. New ideas can be
    traded as 'exp_<name>' before being registered."""
    return setups.describe()


# ------------------------------------------------------------------ account / orders
@tool
async def account() -> dict[str, Any]:
    """Equity, available margin, day/week P&L (equity vs the period's start mark, net of transfers: what every cap
    reads) with the old realized+unrealized figures as evidence, the equity peak, mode, effective limits."""
    return await get_app().trader.account()


@tool
async def positions() -> list[dict[str, Any]]:
    """Open shorts: PnL, exchange stop (UNPROTECTED if none), journal trade id, setup, thesis, PLAN,
    MAE/MFE, targets hit."""
    return await get_app().trader.positions()


@tool
async def open_short(symbol: str, stop_price: float, setup: str, thesis: str, plan: dict[str, Any] | None = None,
                     notional_usdt: float | None = None, leverage: int = 2,
                     veto_override: str | None = None, conviction: str = "B",
                     budget_override: str | None = None) -> dict[str, Any]:
    """Open a market short with a mandatory exchange stop; executes on the exchange (vetoes, risk budget, kill switch).
    setup: a registered setup (list_setups) or 'exp_<name>'. thesis: why now, what confirmed it.
    plan (optional, merged over the setup's defaults): trigger, invalidation, targets [{price, fraction}],
    time_stop_hours, breakeven_at_pct, trail_activate_pct, trail_pct, review_every_hours, kill_conditions [..],
    auto {breakeven, trail, targets, time_stop} = what the guardian may execute without asking.
    Omit notional_usdt to auto-size to the per-trade risk cap. Scanner vetoes, a cold desk and a hostile/unknown
    regime block the trade unless veto_override gives a research reason. An explicit notional_usdt that risks more
    than the current risk budget is refused unless budget_override gives one. Neither lifts a blocked budget (governor
    halt, no heat room) or a hard cap; each one used is stored on the trade with what it bypassed, and alerted.
    Refused while an order rests on the symbol (cancel_entry_group first): its fill would be a second trade.
    conviction: C 0.5x | B 1x | A 1.5x (downgraded to B unless the setup has >= 30 trades with R's 90% lower bound
    > 0) | M = moonshot (25% of a ring-fenced bucket). The risk governor can block or halve any tier."""
    return await get_app().trader.open_short(symbol, stop_price, setup, thesis, plan, notional_usdt, leverage,
                                             veto_override, conviction, budget_override)


@tool
async def close_position(symbol: str, reason: str, lesson: str = "", fraction: float = 1.0,
                         expect_contracts: float | None = None, of_original: bool = False) -> dict[str, Any]:
    """Buy back all or a fraction of a short (reduce-only); executes on the exchange. A full close records
    exchange-truth PnL (fees + funding included) and your lesson in the journal. `fraction` is of the CURRENT size:
    pass the contracts you saw as expect_contracts (refused if the position changed since, e.g. the guardian took a
    target). For a plan target use of_original=true: fraction = share of the ORIGINAL size gone by the end, as plan
    targets count it (a target already taken closes nothing more)."""
    return await get_app().trader.close(symbol, reason, lesson, fraction, expect_contracts, of_original)


@tool
async def move_stop(symbol: str, new_stop: float, reason: str) -> dict[str, Any]:
    """Tighten a short's exchange stop (move DOWN only). Widening is refused; a failed swap restores the old stop."""
    return await get_app().trader.move_stop(symbol, new_stop, reason)


@tool
async def update_plan(symbol: str, changes: dict[str, Any], reason: str) -> dict[str, Any]:
    """Change an open trade's plan (targets, time stop, trailing, auto flags...). Auto flags let the
    guardian act on its own, so set them deliberately. The desk's pins (r1_usdt, full_size, ladder_live) are kept."""
    app = get_app()
    tr = app.journal.active_trade(to_symbol(symbol))
    if not tr or tr["status"] != "open":
        raise ToolError(f"no open journal trade for {symbol}")
    try:
        plan, pins = revise_plan(tr.get("plan") or {}, changes)
    except ValueError as e:
        return {"ok": False, "plan_errors": [str(e)]}
    errs = validate_plan(plan, tr["entry_price"], tr["stop_price"])
    if errs:
        return {"ok": False, "plan_errors": errs}
    new = {**plan.to_dict(), **pins}
    app.journal.update_trade(tr["id"], plan=new)
    app.journal.log_decision("update_plan", reason, tr["symbol"], snapshot={"trade_id": tr["id"], "changes": changes})
    old, auto = tr.get("plan") or {}, lambda p: bool((p.get("auto") or {}).get("targets"))
    if (old.get("targets") or []) != (new.get("targets") or []) or auto(old) != auto(new):
        resting = await targets_sync.after(app.trader, tr["symbol"], "update_plan")   # #65: the book follows the plan
        return {"ok": True, "trade_id": tr["id"], "plan": new, "resting_targets": resting}
    return {"ok": True, "trade_id": tr["id"], "plan": new}


@tool
async def reassess(symbol: str) -> dict[str, Any]:
    """Re-plan an open short FROM ITS CURRENT STATE (not the entry): R now, risk to stop, chandelier stop, reward:risk
    to the next target, ideal size for today's risk budget, time/funding checks, and ranked suggested actions."""
    return await PositionOps(get_app().trader).reassess(symbol)


@tool
async def risk_budget(symbol: str, conviction: str = "B", setup: str | None = None) -> dict[str, Any]:
    """Current dynamic risk budget for a new short: base × drawdown × BTC-vol × regime × correlation × conviction
    tilt × governor, capped by heat and by what is left under today's loss cap."""
    app = get_app()
    reg = (await regime.market_regime(app.ex))["verdict_for_shorts"]
    return await app.trader.budget(app.trader, to_symbol(symbol), reg, conviction, setup)


@tool
async def widen_stop(symbol: str, new_stop: float, reason: str) -> dict[str, Any]:
    """Widen a stop ONCE per trade; size is cut automatically so the loss at the new stop stays ≤ the initial 1R."""
    return await PositionOps(get_app().trader).widen_stop(symbol, new_stop, reason)


@tool
async def add_to_winner(symbol: str, notional_usdt: float, new_stop: float, reason: str) -> dict[str, Any]:
    """Pyramid into a profitable short: the stop is tightened first so total worst case after the add ≤ 1R."""
    return await PositionOps(get_app().trader).add_to_winner(symbol, notional_usdt, new_stop, reason)


@tool
async def propose_entries(proposals: list[dict[str, Any]], ttl_hours: float = 6.0,
                          warmup_override: str | None = None) -> dict[str, Any]:
    """Limit-entry GROUP (3-5 shorts at better levels). Each: {symbol, limit_price (ABOVE market), stop_price, setup,
    thesis, plan?, leverage?, level_type (round_number|prior_high|sweep|vwap_band|ema_retest), level_reason,
    cancel_below?, conviction? (C|B|A|M, default B), veto_override? (research reason; no budget override: always
    sized from the current risk budget)}. GTD expiry on the exchange (ttl_hours <= 24); pre-armed reduce-only stop;
    the guardian protects fills within 5 s and cancels the siblings of the first fill. Resting entries are withdrawn
    while the governor halts new entries or a market pause is on (one placed over that pause stays). A send whose
    outcome is unknown (timeout, 5xx) is listed under `unknown`: it may rest on the book, so don't re-propose it."""
    return await EntryDesk(get_app().trader).propose_group(proposals, ttl_hours, True, warmup_override)


@tool
async def pending_entries() -> list[dict[str, Any]]:
    """Resting limit entries (group, level, limit, stop, expiry). May also list rows awaiting the guardian: one that
    filled before its cancel landed (adopted as a trade within 5 s) or one whose cancel failed (retried each poll).
    Also every other order that is or may be on the book, each with a hint: sends whose outcome isn't known yet
    (placing/unknown: never re-propose them) and orders the desk didn't place (external: no plan, no stop)."""
    return EntryDesk(get_app().trader).pending(all_orders=True)


@tool
async def cancel_entry_group(group_id: str, reason: str) -> dict[str, Any]:
    """Cancel all pending entries of a group (always allowed: removes risk). filled_first = filled before the cancel
    landed: they keep their stops and the guardian journals them within 5 s. cancel_failed (ok false) = still live:
    call again."""
    return await EntryDesk(get_app().trader).cancel_group(group_id, reason)


@tool
async def amend_entry(entry_id: int, new_limit: float, reason: str) -> dict[str, Any]:
    """Move a pending limit ONCE (don't chase). Lowering it re-runs the risk check (more risk at the same size) and
    is refused for a split entry's leg; every amend is a logged decision."""
    return await EntryDesk(get_app().trader).amend(entry_id, new_limit, reason)
