"""Research and journal MCP tools: review chart, bracket odds and calibration, unlocks, signal plug-ins, research risk
params, decisions, learnings, research report and sessions. Registered on the shared server by importing this module
from mcp_server."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import research
from .app import get_app
from .entries import EntryDesk
from .mcp_server import tool


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
