"""MCP tools for Claude Code: the ACTION + STATE layer of the desk.

Design (see CLAUDE.md): MCP holds the small, safety-critical surface (orders, stops, plans, params,
journal, learnings). Ad-hoc analysis goes through `shortbot.lab` via Bash (code mode), and setup
playbooks live in .claude/skills/. Tools that change money-at-risk carry
_meta["anthropic/requiresUserInteraction"], so Claude Code asks the human on EVERY call, in every
permission mode (auto / bypass included)."""

from __future__ import annotations

import functools
import logging
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import news, research, scanner, setups
from .app import close_app, get_app
from .exchange import to_symbol
from .plans import build_plan, validate_plan
from .entries import EntryDesk
from .positioning import PositionOps

INSTRUCTIONS = """Short-only Binance USDⓈ-M futures research desk. Start with the `trading-session` skill.
Flow: journal+learnings -> market_regime -> positions (manage first, per each trade's plan) -> scan_shorts ->
coin_snapshot on 5-10 -> load the setup's skill -> propose trades WITH a plan -> human approves -> open_short.
Every scan is logged for research; review research_report regularly and record conclusions with add_learning.
Risk limits are research parameters (set_risk_params). Vetoes are hypotheses too: override only with a
written veto_override reason. Never dodge a rejection silently. The AWS guardian keeps the .env baseline."""

REQUIRES_HUMAN = {"anthropic/requiresUserInteraction": True}


@asynccontextmanager
async def lifespan(_server):
    try:
        yield None
    finally:
        await close_app()


mcp = MCPServer("shortbot", instructions=INSTRUCTIONS, lifespan=lifespan)


def tool(fn=None, *, human: bool = False):
    """Register an MCP tool; exceptions reach Claude as readable ToolErrors. human=True forces approval."""
    def register(f):
        @functools.wraps(f)
        async def wrapper(*args, **kwargs):
            try:
                return await f(*args, **kwargs)
            except ToolError:
                raise
            except Exception as e:  # noqa: BLE001
                raise ToolError(f"{type(e).__name__}: {e}") from e
        return mcp.tool(meta=REQUIRES_HUMAN if human else None)(wrapper)
    return register(fn) if fn else register


# ------------------------------------------------------------------ market
@tool
async def market_regime() -> dict[str, Any]:
    """BTC trend, alt breadth, Fear & Greed -> 'favorable' / 'caution' / 'hostile' / 'unknown' (data missing)."""
    return await scanner.market_regime(get_app().ex)


@tool
async def scan_shorts(min_change_24h: float = 10.0, top_n: int = 10, direction: str = "gainers") -> dict[str, Any]:
    """Sort Binance futures by 24h % change (direction: gainers = fade pumps, losers = breakdown continuation,
    both), deep-score the top movers. Returns tradable candidates (score,
    signals, detected setups), vetoed coins with reasons, perps listed <30d. Every scored coin is logged as a
    research candidate and labelled with its real outcome 72h later."""
    app = get_app()
    reg = await scanner.market_regime(app.ex)
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
    lists sources that failed (then absence of news is not confirmed)."""
    return await news.news_for(symbol.upper().replace("USDT", "").split("/")[0])


@tool
async def latest_announcements(limit: int = 20) -> list[dict[str, Any]]:
    """Latest Binance announcements: new listings, delistings, news, activities."""
    return (await news.binance_announcements())[:limit]


@tool
async def list_setups() -> list[dict[str, Any]]:
    """Registered setups with description, status, default plan, skill name and evidence. New ideas can be
    traded as 'exp_<name>' before being registered."""
    return setups.describe()


# ------------------------------------------------------------------ account / orders
@tool
async def account() -> dict[str, Any]:
    """Equity, available margin, realized+unrealized PnL today (from Binance income), mode, effective limits."""
    return await get_app().trader.account()


@tool
async def positions() -> list[dict[str, Any]]:
    """Open shorts: PnL, exchange stop (UNPROTECTED if none), journal trade id, setup, thesis, PLAN,
    MAE/MFE, targets hit."""
    return await get_app().trader.positions()


@tool(human=True)
async def open_short(symbol: str, stop_price: float, setup: str, thesis: str, plan: dict[str, Any] | None = None,
                     notional_usdt: float | None = None, leverage: int = 2,
                     veto_override: str | None = None, conviction: str = "B") -> dict[str, Any]:
    """Open a market short with a mandatory exchange stop. REQUIRES HUMAN APPROVAL.
    setup: a registered setup (list_setups) or 'exp_<name>'. thesis: why now, what confirmed it.
    plan (optional, merged over the setup's defaults): trigger, invalidation, targets [{price, fraction}],
    time_stop_hours, breakeven_at_pct, trail_activate_pct, trail_pct, review_every_hours, kill_conditions [..],
    auto {breakeven, trail, targets, time_stop} = what the guardian may execute without asking.
    Omit notional_usdt to auto-size to the per-trade risk cap. Scanner vetoes and a hostile/unknown regime
    block the trade unless veto_override gives a research reason (stored on the trade).
    conviction: C 0.5x | B 1x | A 1.5x (downgraded to B unless the setup has >= 30 trades with R's 90% lower bound
    > 0) | M = moonshot (25% of a ring-fenced bucket). The risk governor can block or halve any tier."""
    return await get_app().trader.open_short(symbol, stop_price, setup, thesis, plan, notional_usdt, leverage,
                                             veto_override, conviction)


@tool(human=True)
async def close_position(symbol: str, reason: str, lesson: str = "", fraction: float = 1.0) -> dict[str, Any]:
    """Buy back all or a fraction of a short (reduce-only). REQUIRES HUMAN APPROVAL. A full close records
    exchange-truth PnL (fees + funding included) and your lesson in the journal."""
    return await get_app().trader.close(symbol, reason, lesson, fraction)


@tool
async def move_stop(symbol: str, new_stop: float, reason: str) -> dict[str, Any]:
    """Tighten a short's exchange stop (move DOWN only). Widening is refused; a failed swap restores the old stop."""
    return await get_app().trader.move_stop(symbol, new_stop, reason)


@tool(human=True)
async def update_plan(symbol: str, changes: dict[str, Any], reason: str) -> dict[str, Any]:
    """Change an open trade's plan (targets, time stop, trailing, auto flags...). REQUIRES HUMAN APPROVAL,
    because auto flags let the guardian act on its own."""
    app = get_app()
    tr = app.journal.active_trade(to_symbol(symbol))
    if not tr or tr["status"] != "open":
        raise ToolError(f"no open journal trade for {symbol}")
    plan = build_plan(tr.get("plan") or {}, changes)
    errs = validate_plan(plan, tr["entry_price"], tr["stop_price"])
    if errs:
        return {"ok": False, "plan_errors": errs}
    app.journal.update_trade(tr["id"], plan=plan.to_dict())
    app.journal.log_decision("update_plan", reason, tr["symbol"], snapshot={"trade_id": tr["id"], "changes": changes})
    return {"ok": True, "trade_id": tr["id"], "plan": plan.to_dict()}


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
    reg = (await scanner.market_regime(app.ex))["verdict_for_shorts"]
    return await app.trader.budget(app.trader, to_symbol(symbol), reg, conviction, setup)


@tool(human=True)
async def widen_stop(symbol: str, new_stop: float, reason: str) -> dict[str, Any]:
    """Widen a stop ONCE per trade; size is cut automatically so the loss at the new stop stays ≤ the initial 1R."""
    return await PositionOps(get_app().trader).widen_stop(symbol, new_stop, reason)


@tool(human=True)
async def add_to_winner(symbol: str, notional_usdt: float, new_stop: float, reason: str) -> dict[str, Any]:
    """Pyramid into a profitable short: the stop is tightened first so total worst case after the add ≤ 1R."""
    return await PositionOps(get_app().trader).add_to_winner(symbol, notional_usdt, new_stop, reason)


@tool(human=True)
async def propose_entries(proposals: list[dict[str, Any]], ttl_hours: float = 6.0,
                          warmup_override: str | None = None) -> dict[str, Any]:
    """Limit-entry GROUP (3-5 shorts at better levels). Each: {symbol, limit_price (ABOVE market), stop_price, setup,
    thesis, plan?, leverage?, level_type (round_number|prior_high|sweep|vwap_band|ema_retest), level_reason,
    cancel_below?, conviction? (C|B|A|M, default B)}. GTD expiry on the exchange; pre-armed reduce-only stop; the guardian protects fills within 5 s
    and cancels the siblings of the first fill. Sized from the current risk budget. A send whose outcome is unknown
    (timeout, 5xx) is listed under `unknown`: it may rest on the book, so don't re-propose it."""
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


@tool(human=True)
async def amend_entry(entry_id: int, new_limit: float, reason: str) -> dict[str, Any]:
    """Move a pending limit ONCE (don't chase)."""
    return await EntryDesk(get_app().trader).amend(entry_id, new_limit, reason)


@tool
async def chart(symbol: str, tf: str = "15m", bars: int = 150) -> dict[str, Any]:
    """Render a review chart PNG (candles, VWAP ±2σ, liquidity levels, position/entries, failed-sweep marker,
    liquidations, OI, CVD) and return its path + the tape state. Read the PNG to look at it."""
    from .charts import render
    app = get_app()
    return await render(app.ex, symbol, tf, bars, trader=app.trader, entries=EntryDesk(app.trader).pending())


@tool
async def probability(symbol: str, stop: float, targets: list[float], hours: float = 24.0, entry: float | None = None,
                      fill_hours: float | None = None, analyst_p: float | None = None,
                      setup: str = "manual") -> dict[str, Any]:
    """NO-EDGE BASELINE odds for a short bracket: P(target before stop), P(stop first), P(neither) within `hours`
    (closed form + fat-tailed Monte Carlo on the coin's own returns), stop-touch risk, and fill odds when `entry`
    is a limit above the market. EV from R:R alone is ≈ 0: our thesis must beat these odds. Pass analyst_p (your
    own probability) so calibration of model vs analyst is measured. Logged; labelled after `hours`."""
    from .probability import ProbabilityEngine
    app = get_app()
    return await ProbabilityEngine(app.ex, app.journal).short_bracket(
        symbol, stop, targets, hours, entry=entry, fill_hours=fill_hours, log_source="manual", analyst_p=analyst_p,
        context={"setup": setup})


@tool
async def calibration_report() -> dict[str, Any]:
    """Brier calibration of the model and the analyst, and the MEASURED edge (observed P(target first) − no-edge
    baseline, with 95% CI) by setup, tape state, regime and source. Sizing may use p only after the gate in
    docs/PROBABILITY.md."""
    from .probability.evaluate import report as prob_report
    return prob_report(get_app().journal)


@tool
async def add_unlock(base: str, date: str, recipient: str, source: str, pct_supply: float | None = None,
                     notes: str = "") -> dict[str, Any]:
    """Record a token unlock (strategy #1: supply-shock shorts). recipient: team|investor|cliff|ecosystem|community|
    other. source: the URL/document the date came from (required: no unsourced events)."""
    from .market.events import EventStore
    return {"ok": True, "unlock_id": EventStore(get_app().settings.db_path).add_unlock(
        base, date, pct_supply, recipient, source, notes)}


@tool
async def upcoming_unlocks(base: str | None = None, days: float = 60) -> list[dict[str, Any]]:
    """Recorded unlocks in the next `days` (and the past 7): days_to_unlock, % supply, recipient, source."""
    from .market.events import EventStore
    return EventStore(get_app().settings.db_path).upcoming(base, days=days)


@tool
async def list_signals() -> list[dict[str, Any]]:
    """Registered signal plug-ins (name, group, doc). Features of all of them are logged for research."""
    from .signals import describe
    return describe()


# ------------------------------------------------------------------ research params
@tool
async def risk_params() -> dict[str, Any]:
    """Effective risk limits, fixed .env baseline, AI overrides, research bounds and active experiment."""
    return _params().describe()


@tool(human=True)
async def set_risk_params(changes: dict[str, float], reasoning: str, experiment: str) -> dict[str, Any]:
    """Research: change risk limits within the human-set bounds. REQUIRES HUMAN APPROVAL. Tunable: max_leverage,
    max_position_notional, max_total_notional, max_positions, risk_per_trade_pct, daily_loss_limit_pct,
    min_stop_distance_pct, max_stop_distance_pct, min_quote_volume_24h, liq_buffer_pct. experiment = tag stored
    on every trade opened while active; compare via journal stats by_experiment."""
    return _params().set(changes, reasoning, experiment)


@tool
async def reset_risk_params(reasoning: str) -> dict[str, Any]:
    """Drop all research overrides and return to the .env baseline (always allowed: it only reduces risk)."""
    return _params().reset(reasoning)


def _params():
    p = get_app().params
    if p is None:
        raise ToolError("research params unavailable in this process")
    return p


# ------------------------------------------------------------------ journal / learning
@tool
async def log_decision(action: str, reasoning: str, symbol: str | None = None) -> dict[str, Any]:
    """Record a non-order decision (skip, watch, hold, note, session_summary) with reasoning."""
    return {"ok": True, "decision_id": get_app().journal.log_decision(action, reasoning, symbol)}


@tool
async def journal(limit: int = 20) -> dict[str, Any]:
    """Stats (by setup/experiment: win rate, avg R, MAE/MFE), open trades, recent trades/decisions/alerts,
    and active learnings. Review before trading."""
    j = get_app().journal
    return {"stats": j.stats(), "open_trades": j.open_trades(include_opening=True), "recent_trades": j.trades(limit),
            "recent_decisions": j.decisions(limit=limit), "recent_alerts": j.alerts(limit),
            "learnings": j.learnings()}


@tool
async def add_learning(statement: str, evidence: str, setup: str | None = None, status: str = "hypothesis",
                       confidence: str = "low") -> dict[str, Any]:
    """Record a research finding so future sessions start from it. status: hypothesis|supported|rejected|adopted;
    confidence: low|medium|high. Evidence should cite trade ids, research_report numbers or sources."""
    return {"ok": True, "learning_id": get_app().journal.add_learning(statement, evidence, setup, status, confidence)}


@tool
async def update_learning(learning_id: int, status: str | None = None, evidence: str | None = None,
                          confidence: str | None = None) -> dict[str, Any]:
    """Update a learning as evidence accumulates (evidence is appended, never overwritten)."""
    get_app().journal.update_learning(learning_id, status, evidence, confidence)
    return {"ok": True}


@tool
async def research_report(min_n: int = 5) -> dict[str, Any]:
    """What predicted drops? Labelled scan candidates grouped by tradable/vetoed, veto reason, setup, score bucket
    and feature tertiles: avg 24h short return, hit rate, squeeze rate. For custom analysis use shortbot.lab."""
    return research.report(get_app().journal, min_n)


@tool
async def start_session(notes: str = "") -> dict[str, Any]:
    """Start a trading session in the journal (call once at the beginning)."""
    app = get_app()
    return {"session_id": app.journal.start_session(app.settings.mode, notes), "mode": app.settings.mode,
            "experiment": app.ex.experiment}


from . import mcp_book, mcp_desk, mcp_kill  # noqa: E402,F401  (registers book plan / governor / clock / warm-up / kill-switch tools)


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    mcp.run()


if __name__ == "__main__":
    main()

