"""Terminal access: `shortbot scan`, `shortbot snapshot QNT`, `shortbot positions`, `shortbot brief`...
Arguments live in cli_args.py, `brief` (what the SessionStart hook prints) in cli_brief.py; dispatch is here.
Audit (#24): each command runs as actor cli:<cmd>; one not in READS leaves a `call` decision row (audit.py).
Every state-changing MCP tool has a CLI form (stale_guard.CLI): a stale MCP server's refusal names it (#68)."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import sys

from . import allocator, audit, audit_view, backup, killswitch_ops, news, research, scanner, setups
from .app import close_app, get_app
from .cli_args import build_parser
from .cli_brief import brief
from .exchange import to_symbol
from .entries import EntryDesk
from .positioning import PositionOps
from .redact import install_log_redaction

# Commands that leave no `call` row: they change nothing (or journal their own row, like `log`). Any other is logged.
READS = frozenset({"regime", "scan", "snapshot", "news", "account", "positions", "params", "setups", "research",
                   "journal", "brief", "map", "study", "read", "profile", "clock", "entries", "chart", "prob",
                   "unlocks", "calibration", "signals", "budget", "governor", "reassess", "book-plan", "log", "audit"})



async def _study(app, args) -> dict:
    """`shortbot study NAME`; every study's trials go into the trial register (#31). sweeps is descriptive (fake-out
    size quantiles for stop placement), not a test."""
    from . import trial_register as TR
    from .backtest import register_rows as RR
    if args.name == "ladder":
        from .shadow_eval import evaluate as ev_ladder
        res, rows = ev_ladder(app.journal), RR.ladder_rows
    elif args.name == "patterns":
        from . import patterns as pt, patterns_data as ptd
        fetched = await ptd.fetch_all(app.ex, app.journal, days=10) if args.fetch else None
        res, rows = {"fetch": fetched, **pt.report(pt.run(ptd.load(app.journal)))}, RR.patterns_rows
    elif args.name == "laggard":
        from .laggard import report as lag_report
        res, rows = lag_report(app.journal), RR.laggard_rows
    else:
        from .sweeps import report as sweep_report
        return sweep_report(app.journal)
    return {**res, "trial_register_rows": TR.record(args.name, rows(res))}

async def _run(args: argparse.Namespace) -> object:
    app = get_app()
    try:
        match args.cmd:
            case "regime":
                return await scanner.market_regime(app.ex)
            case "scan":
                return await scanner.scan_shorts(app.ex, min_change=args.min_change, top_n=args.top,
                                                 journal=app.journal if args.log else None,
                                                 direction=args.direction)
            case "snapshot":
                return await scanner.coin_snapshot(app.ex, args.symbol)
            case "news":
                return await news.coin_news(args.symbol.upper())
            case "account":
                return await app.trader.account()
            case "positions":
                return await app.trader.positions()
            case "params":
                return app.params.describe()
            case "setups":
                return setups.describe()
            case "research":
                return research.report(app.journal)
            case "journal":
                return {"stats": app.journal.stats(), "trades": app.journal.trades(args.limit),
                        "decisions": app.journal.decisions(limit=args.limit), "learnings": app.journal.learnings()}
            case "brief":
                return await brief(app)
            case "learn":
                return {"ok": True, "learning_id": app.journal.add_learning(
                    args.statement, args.evidence, args.setup, args.status, args.confidence,
                    args.provenance or ["cli"], kind=args.kind, n=args.n, days=args.days, sample=args.sample,
                    review_by=args.review_by)}
            case "map":
                from .market.market_map import build_map, cached, render
                m = (None if args.fresh else cached(app.journal, 900)) or await build_map(app.ex, app.journal)
                return render(m)
            case "sweeps":
                from . import sweeps
                if args.action == "run":
                    return await sweeps.run_study(app.ex, app.journal, args.days, args.top)
                return sweeps.report(app.journal)
            case "study":
                return await _study(app, args)
            case "ladder":
                from .ladder import build_ladder
                if args.enable or args.apply or args.dry_run:
                    from . import ladder_apply as la
                    if args.enable:
                        return await la.enable(app.trader, args.symbol)
                    return await la.apply(app.trader, args.symbol, dry_run=args.dry_run)
                pos = [p for p in await app.trader.positions() if p["symbol"] == to_symbol(args.symbol)]
                return await build_ladder(app.ex, app.journal, pos[0], log=False) if pos else {"error": "no position"}
            case "registry":
                from . import registry as R
                if args.action == "seed":
                    from . import patterns_data
                    from .registry_seed import feed_candidates, seed_all
                    return {**seed_all(app.journal, args.bt_dir, patterns_data.load(app.journal)),
                            "scan_episodes": feed_candidates(app.journal)}
                if args.action == "card":
                    return R.card(app.journal.db, args.arg) or {"error": "unknown pattern"}
                if args.action == "search":
                    return R.search(app.journal.db, args.arg or "", 10)
                if args.action == "forward":
                    from .registry_forward import report as forward_report
                    return forward_report(app.journal.db, args.arg)
                if args.action == "status":
                    if not (args.arg and args.status):
                        return {"error": "usage: shortbot registry status ID --status S --reason R"}
                    return R.set_status(app.journal.db, args.arg, args.status, args.reason or "")
                return {"tree": R.tree(app.journal.db), "decay_alarms": R.decay_alarms(app.journal.db)}
            case "read":
                from .read import read_coin
                return await read_coin(app.ex, app.journal, args.symbol)
            case "profile":
                from .profile import build_profile
                return await build_profile(app.ex, app.journal, args.symbol)
            case "clock":
                from .warmup import desk_clock
                return desk_clock(app.settings, app.journal)
            case "warmup":
                from .warmup import WarmUp
                wu = WarmUp(app.ex, app.journal)
                return await (wu.start(args.symbols or None) if args.action == "start" else wu.check())
            case "macro-add":
                from datetime import datetime
                from .market.calendar import MacroCalendar
                MacroCalendar(app.settings.db_path).add(int(datetime.fromisoformat(args.when).timestamp() * 1000),
                                                        args.name, args.kind, args.source)
                return {"ok": True}
            case "macro-refresh":
                from .market.calendar import MacroCalendar
                return await MacroCalendar(app.settings.db_path).refresh()
            # order actions: same code path as the MCP tools (risk limits, vetoes, plans, journal, alerts)
            case "open":
                return await app.trader.open_short(args.symbol, args.stop, args.setup, args.thesis,
                                                   json.loads(args.plan) if args.plan else None, args.notional,
                                                   args.leverage, args.veto_override, args.conviction,
                                                   args.budget_override)
            case "close":
                return await app.trader.close(args.symbol, args.reason, args.lesson, args.fraction,
                                              args.expect_contracts, args.of_original)
            case "book-plan":
                return await allocator.run_cli(app, args)
            case "kill-switch" | "flatten":
                return await killswitch_ops.run_cli(app, args)
            case "move-stop":
                return await app.trader.move_stop(args.symbol, args.price, args.reason)
            # MCP tools with logic of their own: the tool's body runs here (one copy of the code)
            case "update-plan":
                return await tool_body("mcp_trade", "update_plan")(args.symbol, json.loads(args.changes), args.reason)
            case "set-params":
                return await tool_body("mcp_research", "set_risk_params")(dict(args.changes), args.reason,
                                                                          args.experiment)
            case "reset-params":
                return await tool_body("mcp_research", "reset_risk_params")(args.reason)
            case "update-learning":
                return await tool_body("mcp_research", "update_learning")(
                    args.learning_id, status=args.status, evidence=args.evidence, confidence=args.confidence,
                    provenance=args.provenance, kind=args.kind, n=args.n, days=args.days, sample=args.sample,
                    review_by=args.review_by)
            case "start-session":
                return await tool_body("mcp_research", "start_session")(args.notes)
            case "reassess":
                return await PositionOps(app.trader).reassess(args.symbol)
            case "widen-stop":
                return await PositionOps(app.trader).widen_stop(args.symbol, args.price, args.reason)
            case "add":
                return await PositionOps(app.trader).add_to_winner(args.symbol, args.notional, args.stop, args.reason)
            case "budget":
                reg = (await scanner.market_regime(app.ex))["verdict_for_shorts"]
                return await app.trader.budget(app.trader, to_symbol(args.symbol), reg, args.conviction, args.setup)
            case "governor":
                from .risk_budget import governor_status
                return await governor_status(app.trader, (await scanner.market_regime(app.ex))["verdict_for_shorts"])
            case "propose":
                return await EntryDesk(app.trader).propose_group(json.loads(args.proposals), args.ttl_hours,
                                                                 not args.no_prearm, args.warmup_override)
            case "entries":
                return EntryDesk(app.trader).pending(all_orders=True)
            case "cancel-group":
                return await EntryDesk(app.trader).cancel_group(args.group_id, args.reason)
            case "amend":
                return await EntryDesk(app.trader).amend(args.entry_id, args.price, args.reason)
            case "chart":
                from .charts import render
                return await render(app.ex, args.symbol, args.tf, args.bars, trader=app.trader,
                                    entries=EntryDesk(app.trader).pending())
            case "prob":
                from .probability import ProbabilityEngine
                return await ProbabilityEngine(app.ex, app.journal).short_bracket(
                    args.symbol, args.stop, args.target, args.hours, entry=args.entry, fill_hours=args.fill_hours,
                    log_source="manual", analyst_p=args.analyst_p, context={"setup": args.setup})
            case "unlock-add":
                from .market.events import EventStore
                uid = EventStore(app.settings.db_path).add_unlock(args.base, args.date, args.pct, args.recipient,
                                                                  args.source, args.notes)
                return {"ok": True, "unlock_id": uid}
            case "unlocks":
                from .market.events import EventStore
                return EventStore(app.settings.db_path).upcoming(args.base, days=args.days)
            case "calibration":
                from .probability.evaluate import report as prob_report
                return prob_report(app.journal)
            case "signals":
                from .signals import describe
                return describe()
            case "log":
                sym = to_symbol(args.symbol) if args.symbol else None
                return {"decision_id": app.journal.log_decision(args.action, args.reasoning, sym)}
            case "audit":
                return audit_view.report(app.journal, args.since, args.actor, args.action, args.limit)
    finally:
        await close_app()


def tool_body(module: str, name: str):
    """The function under an MCP tool's wrapper, for a command whose tool holds logic of its own: one copy of the code
    (a fix to the tool reaches the CLI), run as cli:<cmd>. Importing the MCP modules builds the MCPServer, whose
    constructor runs logging.basicConfig (stderr, INFO); the CLI keeps its own root logger (#66), so it is restored."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        mod = importlib.import_module(f"{__package__}.{module}")
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
    return getattr(mod, name).__wrapped__


async def _audited(args: argparse.Namespace) -> object:
    with audit.acting(f"cli:{args.cmd}"):
        if args.cmd in READS:
            return await _run(args)
        j = get_app().journal   # the singleton _run uses; close_app leaves its journal open
        return await audit.called(lambda: j, args.cmd, {k: v for k, v in vars(args).items() if k != "cmd"},
                                  _run(args))


def main() -> None:
    install_log_redaction()   # no handlers here: records go through logging.lastResort (#66)
    args = build_parser().parse_args()
    if args.cmd == "backup":
        raise SystemExit(backup.run_cli(args))
    if args.cmd == "hook-session":   # no exchange app, no output: the hook's stdout would enter the context
        raise SystemExit(audit_view.hook_session(sys.stdin))
    audit.set_process("cli")
    out = asyncio.run(_audited(args))
    print(out if isinstance(out, str) else json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
