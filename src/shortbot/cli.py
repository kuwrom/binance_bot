"""Terminal access: `shortbot scan`, `shortbot snapshot QNT`, `shortbot positions`, `shortbot brief`...
`brief` is what the SessionStart hook prints into Claude's context (fast, never fails hard)."""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from . import allocator, backup, killswitch, killswitch_ops, news, research, scanner, setups
from .app import close_app, get_app
from .exchange import to_symbol
from .entries import EntryDesk
from .positioning import PositionOps
from .tilts import TIERS

BRIEF_TIMEOUT_S = 20


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
                return await news.news_for(args.symbol.upper())
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
                    args.statement, args.evidence, args.setup, args.status, args.confidence)}
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
                if args.name == "ladder":
                    from .shadow_eval import evaluate as ev_ladder
                    return ev_ladder(app.journal)
                if args.name == "patterns":
                    from . import patterns as pt, patterns_data as ptd
                    fetched = await ptd.fetch_all(app.ex, app.journal, days=10) if args.fetch else None
                    return {"fetch": fetched, **pt.report(pt.run(ptd.load(app.journal)))}
                if args.name == "laggard":
                    from .laggard import report as lag_report
                    return lag_report(app.journal)
                from .sweeps import report as sweep_report
                return sweep_report(app.journal)
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
                                                   args.leverage, args.veto_override, args.conviction)
            case "close":
                return await app.trader.close(args.symbol, args.reason, args.lesson, args.fraction,
                                              args.expect_contracts, args.of_original)
            case "book-plan":
                return await allocator.run_cli(app, args)
            case "kill-switch" | "flatten":
                return await killswitch_ops.run_cli(app, args)
            case "move-stop":
                return await app.trader.move_stop(args.symbol, args.price, args.reason)
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
    finally:
        await close_app()


async def brief(app) -> str:
    """Compact markdown state for the start of a Claude session."""
    j, lines = app.journal, []
    last = j.get("last_brief_ts")
    lines.append(f"## shortbot state ({app.settings.mode} mode, experiment: {app.ex.experiment or 'baseline'})")
    try:   # the watchdog may trip HALTED here: the guardian can't report its own death
        await asyncio.wait_for(killswitch.watch_guardian(app.trader), BRIEF_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        lines.append(f"- ⚠️ guardian watchdog failed: {type(e).__name__}: {e}")
    lines.append(killswitch.brief_line(j))
    live: list[dict] = []
    if app.ex.trade:
        try:
            live = await asyncio.wait_for(app.trader.positions(), BRIEF_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            lines.append(f"- ⚠️ could not load live positions: {type(e).__name__}: {e}")
    else:
        lines.append("- no trading keys configured (scan/research only)")
    for p in live:
        flag = " **UNPROTECTED**" if p["UNPROTECTED"] else ""
        plan = p.get("plan") or {}
        lines.append(f"- {p['symbol']} #{p['trade_id']} {p['setup']}: PnL {p['pnl_pct']}%, stop {p['exchange_stop']}"
                     f"{flag}, MFE {p['mfe_pct']}% / MAE {p['mae_pct']}%, time stop {plan.get('time_stop_hours')}h, "
                     f"opened {p['opened_at']}")
    if not live and app.ex.trade:
        lines.append("- no open positions")
    try:   # resting entries, sends of unknown outcome and orders the desk didn't place
        orders = EntryDesk(app.trader).pending(all_orders=True)
    except Exception as e:  # noqa: BLE001
        orders = []
        lines.append(f"- ⚠️ could not load entry orders: {type(e).__name__}: {e}")
    if orders:
        lines.append(f"### entry orders on the book ({len(orders)})")
        lines += [f"- #{e['id']} {e['symbol']} {e['status']}: {e['amount']} @ {e['limit_price']}, stop "
                  f"{e['stop_price'] or 'NONE'}" + (f" ⚠️ {e['hint']}" if e.get("hint") else "") for e in orders]
    alerts = j.alerts(15, since=last)
    if alerts:
        lines.append(f"### alerts since last session ({len(alerts)})")
        lines += [f"- {a['ts']} {a['kind']} {a['symbol'] or ''}: {a['message'][:140]}" for a in alerts]
    ls = j.learnings()
    if ls:
        lines.append("### active learnings")
        lines += [f"- #{x['id']} [{x['status']}/{x['confidence']}] {x['setup'] or 'all'}: {x['statement']}"
                  for x in ls[:10]]
    from .warmup import desk_clock
    dc = desk_clock(app.settings, j)
    c = dc["clock"]
    lines.append(f"### clock: {c['utc']} {c['weekday']} "
                 f"{c['session']} session, funding in {c['min_to_funding']:.0f} min; desk "
                 f"{'COLD (' + dc['desk']['why'] + '): warm-up before entries' if dc['desk']['cold'] else 'warm'}")
    lines += [f"- macro: {e['name']} ({e['kind']}) {e['utc']}" for e in dc["macro_week"] if e["minutes_to"] >= 0][:4]
    stats = j.stats()
    lines.append(f"### record: {stats['closed_trades']} closed trades, net {stats['total_pnl']} USDT; "
                 f"{len(j.candidates(labelled_only=True, limit=100000))} labelled research candidates")
    j.set("last_brief_ts", time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(prog="shortbot")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("regime")
    s = sub.add_parser("scan")
    s.add_argument("--min-change", type=float, default=10.0)
    s.add_argument("--top", type=int, default=10)
    s.add_argument("--log", action="store_true", help="log candidates for research")
    s.add_argument("--direction", choices=("gainers", "losers", "both", "now"), default="gainers",
                   help="now = biggest 4h movers (needs --log for the market-map cache)")
    for name in ("snapshot", "news"):
        sub.add_parser(name).add_argument("symbol")
    for name in ("account", "positions", "params", "setups", "research", "brief"):
        sub.add_parser(name)
    sub.add_parser("journal").add_argument("--limit", type=int, default=20)
    o = sub.add_parser("open", help="open a short (all risk checks apply)")
    o.add_argument("symbol")
    o.add_argument("--stop", type=float, required=True)
    o.add_argument("--setup", required=True)
    o.add_argument("--thesis", required=True)
    o.add_argument("--plan", help="JSON trade plan (merged over the setup defaults)")
    o.add_argument("--notional", type=float, help="USDT size; omit to auto-size to the risk cap")
    o.add_argument("--leverage", type=int, default=2)
    o.add_argument("--veto-override", help="research reason to trade despite vetoes/regime")
    o.add_argument("--conviction", choices=TIERS, default="B",
                   help="C 0.5x | B 1x | A 1.5x (needs a proven setup record) | M moonshot bucket")
    c = sub.add_parser("close", help="buy back all or part of a short")
    c.add_argument("symbol")
    c.add_argument("--reason", required=True)
    c.add_argument("--lesson", default="")
    c.add_argument("--fraction", type=float, default=1.0, help="of the CURRENT size (see --of-original)")
    c.add_argument("--expect-contracts", type=float, help="the size you saw: refused if it changed since")
    c.add_argument("--of-original", action="store_true", help="fraction = share of the ORIGINAL size gone by the end")
    killswitch_ops.add_cli(sub)
    allocator.add_cli(sub)
    sub.add_parser("reassess", help="re-plan a position from its current state").add_argument("symbol")
    bu = sub.add_parser("budget", help="current dynamic risk budget for a new short")
    bu.add_argument("symbol")
    bu.add_argument("--conviction", choices=TIERS, default="B")
    bu.add_argument("--setup")
    sub.add_parser("governor", help="account breakers: daily/weekly cap, drawdown, streak, give-back")
    w = sub.add_parser("widen-stop", help="widen once, with automatic size cut to keep <= 1R")
    w.add_argument("symbol")
    w.add_argument("price", type=float)
    w.add_argument("--reason", required=True)
    a = sub.add_parser("add", help="pyramid into a winner; stop tightened first, total worst case <= 1R")
    a.add_argument("symbol")
    a.add_argument("--notional", type=float, required=True)
    a.add_argument("--stop", type=float, required=True)
    a.add_argument("--reason", required=True)
    pr = sub.add_parser("propose", help="limit-entry group: first fill cancels the rest")
    pr.add_argument("proposals", help='JSON list of {symbol, limit_price, stop_price, setup, thesis, plan?, '
                    'leverage?, level_type, level_reason, cancel_below?, conviction?, size_mult? (0.1-1, only shrinks: pass '
                    "the read's size_mult), tranches? [{limit_price, share}] = split entry (all-or-nothing), veto_override?}")
    pr.add_argument("--ttl-hours", type=float, default=6.0)
    pr.add_argument("--no-prearm", action="store_true")
    pr.add_argument("--warmup-override", help="reason to skip the cold-desk warm-up")
    wu = sub.add_parser("warmup", help="cold-desk 15-min study: start = t0 snapshot, check = t+15 verdicts")
    wu.add_argument("action", choices=("start", "check"))
    wu.add_argument("symbols", nargs="*", help="default: pending limit entries + top 5 gainers")
    le = sub.add_parser("learn", help="record a research finding (same as the add_learning MCP tool)")
    le.add_argument("statement")
    le.add_argument("--evidence", required=True, help="trade ids, data, sources")
    le.add_argument("--setup")
    le.add_argument("--status", default="hypothesis", choices=("hypothesis", "supported", "rejected", "adopted"))
    le.add_argument("--confidence", default="low", choices=("low", "medium", "high"))
    sub.add_parser("map", help="market map: regime, breadth, sectors vs BTC, rotation, vetoes").add_argument(
        "--fresh", action="store_true", help="rebuild now instead of using the guardian's cached map")
    sw = sub.add_parser("sweeps", help="level-sweep study: how far price pokes past round numbers / 24h highs")
    sw.add_argument("action", choices=("run", "report"))
    sw.add_argument("--days", type=float, default=3)
    sw.add_argument("--top", type=int, default=60, help="most liquid N perps")
    st = sub.add_parser("study", help="shadow study results: ladder variants vs hold | laggard catch-up | sweeps | "
                                      "patterns (week event study: pumps, sweeps, squeezes, funding x liquidity)")
    st.add_argument("name", choices=("ladder", "laggard", "sweeps", "patterns"))
    st.add_argument("--fetch", action="store_true", help="patterns: refresh 10 days of hourly data for every perp")
    rg = sub.add_parser("registry", help="pattern registry: tree | card ID | search TEXT | seed | status ID --status S")
    rg.add_argument("action", choices=("tree", "card", "search", "seed", "status"))
    rg.add_argument("arg", nargs="?")
    rg.add_argument("--status", choices=("draft", "shadow", "live", "probation", "retired"))
    rg.add_argument("--reason")
    rg.add_argument("--bt-dir", help="seed: backtest output dir (results.json + trades.json)")
    ld = sub.add_parser("ladder", help="3-level ladder: per-level odds, edge score, exposure the rule wants (shadow); "
                                       "--enable opts a trade into ladder_live_v1, --apply executes its decision")
    ld.add_argument("symbol")
    ld.add_argument("--enable", action="store_true", help="opt this trade into the live ladder (pins 1R, full size)")
    ld.add_argument("--apply", action="store_true", help="execute the decision: cut / add (<= 1R) / stop re-level")
    ld.add_argument("--dry-run", action="store_true", help="show what --apply would do, without orders")
    sub.add_parser("read", help="what game is this coin playing: lifecycle x tape x controller -> playbook").add_argument(
        "symbol")
    sub.add_parser("profile", help="per-coin behaviour: levels, sessions, context, our trades (shrunk)").add_argument(
        "symbol")
    sub.add_parser("clock", help="UTC session, funding, US open, macro calendar, cold/warm desk")
    ma = sub.add_parser("macro-add", help="record a scheduled macro event (source required)")
    ma.add_argument("name")
    ma.add_argument("--when", required=True, help="ISO time WITH offset, e.g. 2026-09-30T08:30:00-04:00")
    ma.add_argument("--kind", required=True, choices=("tier1", "tier2", "fomc", "maintenance"))
    ma.add_argument("--source", required=True)
    sub.add_parser("macro-refresh", help="pull BLS + ForexFactory + FOMC dates into the macro calendar")
    sub.add_parser("entries", help="pending limit entries")
    cg = sub.add_parser("cancel-group")
    cg.add_argument("group_id")
    cg.add_argument("--reason", required=True)
    am = sub.add_parser("amend", help="move a pending limit once")
    am.add_argument("entry_id", type=int)
    am.add_argument("price", type=float)
    am.add_argument("--reason", required=True)
    ch = sub.add_parser("chart", help="render a PNG (candles, levels, sweeps, liquidations, OI, CVD)")
    ch.add_argument("symbol")
    ch.add_argument("--tf", default="15m", choices=("5m", "15m", "1h", "4h"))
    ch.add_argument("--bars", type=int, default=150)
    sub.add_parser("signals", help="list registered signal plug-ins")
    pb = sub.add_parser("prob", help="no-edge odds for a short bracket (closed form + fat-tail MC); logged")
    pb.add_argument("symbol")
    pb.add_argument("--stop", type=float, required=True)
    pb.add_argument("--target", type=float, nargs="+", required=True)
    pb.add_argument("--hours", type=float, default=24.0)
    pb.add_argument("--entry", type=float, help="limit entry price (default: current price)")
    pb.add_argument("--fill-hours", type=float, help="TTL for fill odds when entry is above the market")
    pb.add_argument("--analyst-p", type=float, help="your own P(target first), logged for calibration")
    pb.add_argument("--setup", default="manual")
    sub.add_parser("calibration", help="calibration (Brier) + measured edge by setup/tape/regime")
    ua = sub.add_parser("unlock-add", help="record a token unlock (source required)")
    ua.add_argument("base")
    ua.add_argument("--date", required=True, help="ISO date/time, UTC (e.g. 2026-10-20 or 2026-10-20T12:00)")
    ua.add_argument("--pct", type=float, help="% of circulating supply unlocking")
    ua.add_argument("--recipient", required=True, choices=("team", "investor", "cliff", "ecosystem", "community", "other"))
    ua.add_argument("--source", required=True, help="URL or document the date came from")
    ua.add_argument("--notes", default="")
    ul = sub.add_parser("unlocks", help="upcoming recorded unlocks")
    ul.add_argument("--base")
    ul.add_argument("--days", type=float, default=60)
    lg = sub.add_parser("log", help="journal a decision (hold, skip, watch, note, session_summary)")
    lg.add_argument("action")
    lg.add_argument("reasoning")
    lg.add_argument("--symbol")
    m = sub.add_parser("move-stop", help="tighten a stop (down only)")
    m.add_argument("symbol")
    m.add_argument("price", type=float)
    m.add_argument("--reason", required=True)
    backup.add_args(sub.add_parser("backup", help="verified journal backup (no exchange) | restore FILE --to NEW_PATH"))
    args = p.parse_args()
    if args.cmd == "backup":
        raise SystemExit(backup.run_cli(args))
    out = asyncio.run(_run(args))
    print(out if isinstance(out, str) else json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
