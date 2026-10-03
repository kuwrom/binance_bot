"""Research and journal commands (commands.py: each one is an MCP tool and a `shortbot` subcommand): review chart,
bracket odds and calibration, unlocks, signal plug-ins, research risk params, decisions, learnings, research report and
sessions."""

from __future__ import annotations

from typing import Any

from . import alerts_view, learnings_view, research
from .app import get_app
from .commands import Arg, CommandError, command
from .entries import EntryDesk
from .exchange import to_symbol
from .weight import PROTECTIVE


@command(cli="chart", help="render a PNG (candles, levels, sweeps, liquidations, OI, CVD)", args=(
    Arg("symbol"), Arg("tf", "--tf", choices=("5m", "15m", "1h", "4h")), Arg("bars", "--bars")))
async def chart(symbol: str, tf: str = "15m", bars: int = 150) -> dict[str, Any]:
    """Render a review chart PNG (candles, VWAP ±2σ, liquidity levels, position/entries, failed-sweep marker,
    liquidations, OI, CVD) and return its path + the tape state. Read the PNG to look at it."""
    from .charts import render
    app = get_app()
    return await render(app.ex, symbol, tf, bars, trader=app.trader, entries=EntryDesk(app.trader).pending())


@command(cli="prob", help="no-edge odds for a short bracket (closed form + fat-tail MC); logged", args=(
    Arg("symbol"), Arg("stop", "--stop"), Arg("targets", "--target", type=float, nargs="+"), Arg("hours", "--hours"),
    Arg("entry", "--entry", "limit entry price (default: current price)"),
    Arg("fill_hours", "--fill-hours", "TTL for fill odds when entry is above the market"),
    Arg("analyst_p", "--analyst-p", "your own P(target first), logged for calibration"), Arg("setup", "--setup")))
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


@command(cli="calibration", help="calibration (Brier) + measured edge by setup/tape/regime")
async def calibration_report() -> dict[str, Any]:
    """Brier calibration of the model and the analyst, and the MEASURED edge (observed P(target first) − no-edge
    baseline, with 95% CI) by setup, tape state, regime and source. Sizing may use p only after the gate in
    docs/PROBABILITY.md."""
    from .probability.evaluate import report as prob_report
    return prob_report(get_app().journal)


@command(write=True)
async def add_unlock(base: str, date: str, recipient: str, source: str, pct_supply: float | None = None,
                     notes: str = "") -> dict[str, Any]:
    """Record a token unlock (strategy #1: supply-shock shorts). recipient: team|investor|cliff|ecosystem|community|
    other. source: the URL/document the date came from (required: no unsourced events)."""
    from .market.events import EventStore
    return {"ok": True, "unlock_id": EventStore(get_app().settings.db_path).add_unlock(
        base, date, pct_supply, recipient, source, notes)}


@command(cli="unlocks", help="upcoming recorded unlocks", args=(Arg("base", "--base"), Arg("days", "--days")))
async def upcoming_unlocks(base: str | None = None, days: float = 60) -> list[dict[str, Any]]:
    """Recorded unlocks in the next `days` (and the past 7): days_to_unlock, % supply, recipient, source."""
    from .market.events import EventStore
    return EventStore(get_app().settings.db_path).upcoming(base, days=days)


@command(cli="signals", help="list registered signal plug-ins")
async def list_signals() -> list[dict[str, Any]]:
    """Registered signal plug-ins (name, group, doc). Features of all of them are logged for research."""
    from .signals import describe
    return describe()


# ------------------------------------------------------------------ research params
@command(cli="params")
async def risk_params() -> dict[str, Any]:
    """Effective risk limits, fixed .env baseline, AI overrides, research bounds and active experiment."""
    return _params().describe()


@command(write=True, weight=PROTECTIVE)
async def set_risk_params(changes: dict[str, float], reasoning: str, experiment: str) -> dict[str, Any]:
    """Research: change risk limits within the human-set bounds; tell the human. Tunable: max_leverage,
    max_position_notional, max_total_notional, max_positions, risk_per_trade_pct, daily_loss_limit_pct,
    min_stop_distance_pct, max_stop_distance_pct, min_quote_volume_24h, liq_buffer_pct. experiment = tag that owns
    the params it sets; earlier experiments keep the params it doesn't touch. Trades carry every active experiment
    (sorted, joined by ' + ') plus the limits they were sized under; compare via journal stats by_experiment.
    Overrides outside bounds the human tightened later are dropped back to .env (alert params_dropped)."""
    return _params().set(changes, reasoning, experiment)


@command(write=True)
async def reset_risk_params(reasoning: str) -> dict[str, Any]:
    """Drop all research overrides and return to the .env baseline (always allowed: it only reduces risk)."""
    return _params().reset(reasoning)


def _params():
    p = get_app().params
    if p is None:
        raise CommandError("research params unavailable in this process")
    return p


# ------------------------------------------------------------------ journal / learning
@command(cli="log", help="journal a decision (hold, skip, watch, note, session_summary)",
         args=(Arg("action"), Arg("reasoning"), Arg("symbol", "--symbol")))
async def log_decision(action: str, reasoning: str, symbol: str | None = None) -> dict[str, Any]:
    """Record a non-order decision (skip, watch, hold, note, session_summary) with reasoning."""
    sym = to_symbol(symbol) if symbol else None
    return {"ok": True, "decision_id": get_app().journal.log_decision(action, reasoning, sym)}


@command(cli="journal", args=(Arg("limit", "--limit"),))
async def journal(limit: int = 20) -> dict[str, Any]:
    """Stats (by setup/experiment: win rate, avg R, MAE/MFE), open trades, recent trades/decisions/alerts,
    and active learnings (each with an `audit`: kind, n, days, review date, flags; '?' fields are inferred).
    Review before trading."""
    j = get_app().journal
    return {"stats": j.stats(), "open_trades": j.open_trades(include_opening=True), "recent_trades": j.trades(limit),
            "recent_decisions": j.decisions(limit=limit),
            "recent_alerts": [alerts_view.for_agent(a) for a in j.alerts(limit)],
            "learnings": learnings_view.annotate(j, j.learnings())}


@command(write=True)
async def add_learning(statement: str, evidence: str, provenance: list[str], kind: str, setup: str | None = None,
                       status: str = "hypothesis", confidence: str = "low", n: float | None = None,
                       days: int | None = None, sample: str | None = None,
                       review_by: str | None = None) -> dict[str, Any]:
    """Record a research finding so future sessions start from it. status: hypothesis|supported|rejected|adopted;
    confidence: low|medium|high. Evidence should cite trade ids, research_report numbers or sources.
    provenance (required): where the evidence came from, e.g. ["trade:12", "research_report", "coin_news:QNT",
    "alert:345"]. Learnings resting on third-party text (news, announcements) are flagged in every brief.
    kind (required): engineering (a fix, backed by a test) | statistical (a claim about outcomes) | process (how the
    desk works). For statistical claims give n = independent observations (research_report n = symbol-episodes,
    trades or events, never scan rows), days = distinct days, sample = where it lives ("research_report:2026-10-02",
    "registry:lib.x", "data/research/<dir>"). review_by (YYYY-MM-DD; default: 14 days for a hypothesis, 30 for a
    supported statistical learning): the brief resurfaces it after that date. n < 30 is flagged as an anecdote."""
    if not provenance:
        raise CommandError("provenance is required: name where the evidence came from, e.g. ['trade:12']")
    return {"ok": True, "learning_id": get_app().journal.add_learning(
        statement, evidence, setup, status, confidence, provenance, kind=kind, n=n, days=days, sample=sample,
        review_by=review_by)}


@command(write=True)
async def update_learning(learning_id: int, status: str | None = None, evidence: str | None = None,
                          confidence: str | None = None, provenance: list[str] | None = None, kind: str | None = None,
                          n: float | None = None, days: int | None = None, sample: str | None = None,
                          review_by: str | None = None) -> dict[str, Any]:
    """Update a learning as evidence accumulates (evidence and provenance are appended, never overwritten; kind, n,
    days, sample and review_by replace the old value). Setting n on a learning recorded before 2026-10-01 marks its
    sample as re-checked on the episode basis."""
    get_app().journal.update_learning(learning_id, status, evidence, confidence, provenance, kind=kind, n=n,
                                      days=days, sample=sample, review_by=review_by)
    return {"ok": True}


@command(cli="research", args=(Arg("min_n", "--min-n", "smallest group reported"),))
async def research_report(min_n: int = 5) -> dict[str, Any]:
    """What predicted drops? Labelled scan candidates grouped by tradable/vetoed, veto reason, setup, score bucket
    and feature tertiles: avg 24h short return, hit rate, squeeze rate. `learnings`: statistical learnings with their
    n, days and sample, and what to re-check (review due, n < 30, pre-episode statistics). `forward`: each registry
    node's pre-registered forward test (the only evidence that promotes; plans on its card). Custom: shortbot.lab."""
    from .registry_forward import summary
    j = get_app().journal
    return {**research.report(j, min_n), "forward": summary(j.db)}


@command(write=True)
async def start_session(notes: str = "") -> dict[str, Any]:
    """Start a trading session in the journal: call it FIRST, once per session. Acknowledges the alerts the brief listed
    as unacknowledged and returns them (count by kind + the urgent ones), so none is acknowledged unseen. Every row
    written after it (this server's tools, and `shortbot` CLI calls from the same Claude session) joins this session
    in the audit trail (`shortbot audit`)."""
    app = get_app()
    return {"session_id": app.journal.start_session(app.settings.mode, notes), "mode": app.settings.mode,
            "experiment": app.ex.experiment, "alerts_acknowledged": alerts_view.acknowledge(app.journal)}
