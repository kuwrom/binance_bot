"""`shortbot` argument tree: every subcommand and its flags (dispatch: cli.py). kill-switch / flatten, book-plan, probe
and backup add their own parsers (killswitch_ops.add_cli, allocator.add_cli, probes.add_cli, backup.add_args)."""

from __future__ import annotations

import argparse

from . import backup, commands, killswitch_ops, probes
from .journal_research import LEARNING_KINDS
from .tilts import TIERS


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shortbot")
    sub = p.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open", help="open a short (all risk checks apply)")
    o.add_argument("symbol")
    o.add_argument("--stop", type=float, required=True)
    o.add_argument("--setup", required=True)
    o.add_argument("--thesis", required=True)
    o.add_argument("--plan", help="JSON trade plan (merged over the setup defaults)")
    o.add_argument("--notional", type=float, help="USDT size; omit to auto-size to the risk cap")
    o.add_argument("--leverage", type=int, default=2)
    o.add_argument("--veto-override", help="research reason to trade despite vetoes/regime/cold desk")
    o.add_argument("--budget-override", help="research reason for a --notional that risks more than the risk budget")
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
    probes.add_cli(sub)
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
    pr.add_argument("--ttl-hours", type=float, default=6.0, help="at most 24")
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
    le.add_argument("--provenance", action="append", default=None,
                    help="where the evidence came from (repeatable): trade:12, research_report, coin_news:QNT")
    le.add_argument("--kind", choices=LEARNING_KINDS,
                    help="engineering (a fix, backed by a test) | statistical (a claim about outcomes) | process")
    le.add_argument("--n", type=float, help="independent observations (episodes, trades, events), never scan rows")
    le.add_argument("--days", type=int, help="distinct days the observations span")
    le.add_argument("--sample", help="where the sample lives: research_report:2026-10-02, registry:lib.x, data/...")
    le.add_argument("--review-by", help="YYYY-MM-DD the brief resurfaces it (default: 14 days for a hypothesis, 30 "
                    "for a supported statistical learning)")
    sw = sub.add_parser("sweeps", help="level-sweep study: how far price pokes past round numbers / 24h highs")
    sw.add_argument("action", choices=("run", "report"))
    sw.add_argument("--days", type=float, default=3)
    sw.add_argument("--top", type=int, default=60, help="most liquid N perps")
    st = sub.add_parser("study", help="shadow study results: ladder variants vs hold | laggard catch-up | sweeps | "
                                      "patterns (week event study: pumps, sweeps, squeezes, funding x liquidity)")
    st.add_argument("name", choices=("ladder", "laggard", "sweeps", "patterns"))
    st.add_argument("--fetch", action="store_true", help="patterns: refresh 10 days of hourly data for every perp")
    rg = sub.add_parser("registry", help="pattern registry: tree | card ID | search TEXT | seed | status ID --status S"
                                         " | forward [ID] (the pre-registered forward test of every non-draft node)")
    rg.add_argument("action", choices=("tree", "card", "search", "seed", "status", "forward"))
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
    ma = sub.add_parser("macro-add", help="record a scheduled macro event (source required)")
    ma.add_argument("name")
    ma.add_argument("--when", required=True, help="ISO time WITH offset, e.g. 2026-09-30T08:30:00-04:00")
    ma.add_argument("--kind", required=True, choices=("tier1", "tier2", "fomc", "maintenance"))
    ma.add_argument("--source", required=True)
    sub.add_parser("macro-refresh", help="pull BLS + ForexFactory + FOMC dates into the macro calendar")
    cg = sub.add_parser("cancel-group")
    cg.add_argument("group_id")
    cg.add_argument("--reason", required=True)
    am = sub.add_parser("amend", help="move a pending limit once")
    am.add_argument("entry_id", type=int)
    am.add_argument("price", type=float)
    am.add_argument("--reason", required=True)
    ua = sub.add_parser("unlock-add", help="record a token unlock (source required)")
    ua.add_argument("base")
    ua.add_argument("--date", required=True, help="ISO date/time, UTC (e.g. 2026-10-20 or 2026-10-20T12:00)")
    ua.add_argument("--pct", type=float, help="% of circulating supply unlocking")
    ua.add_argument("--recipient", required=True, choices=("team", "investor", "cliff", "ecosystem", "community", "other"))
    ua.add_argument("--source", required=True, help="URL or document the date came from")
    ua.add_argument("--notes", default="")
    m = sub.add_parser("move-stop", help="tighten a stop (down only)")
    m.add_argument("symbol")
    m.add_argument("price", type=float)
    m.add_argument("--reason", required=True)
    _tool_forms(sub)
    backup.add_args(sub.add_parser("backup", help="verified journal backup (no exchange) | restore FILE --to NEW_PATH"))
    au = sub.add_parser("audit", help="who did what: decisions and tool/CLI calls with actor, Claude session, code")
    au.add_argument("--since", help="24h, 7d, 30m or an ISO time (UTC)")
    au.add_argument("--actor", help="prefix: mcp, cli, guardian, admin, hook, or e.g. mcp:open_short")
    au.add_argument("--action", help="one action, e.g. call, rejected, open")
    au.add_argument("--limit", type=int, default=50)
    sub.add_parser("hook-session", help="SessionStart hook: record the Claude session's model (reads hook JSON on stdin)")
    sub.add_parser("brief")
    for cmd in commands.load():
        if cmd.cli:
            commands.add_arguments(sub.add_parser(cmd.cli, **({"help": cmd.help} if cmd.help else {})), cmd)
    return p


def _pair(s: str) -> tuple[str, float]:
    k, _, v = s.partition("=")
    try:
        if k.strip():
            return k.strip(), float(v)
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(f"{s!r}: expected KEY=VALUE with a number, e.g. max_leverage=3")


def _tool_forms(sub) -> None:
    """MCP tools whose CLI form runs the tool's own body (cli.tool_body): a stale MCP server's way out (#68)."""
    up = sub.add_parser("update-plan", help="change an open trade's plan (the update_plan MCP tool)")
    up.add_argument("symbol")
    up.add_argument("--changes", required=True, help='JSON plan fields, e.g. {"time_stop_hours": 30}')
    up.add_argument("--reason", required=True)
    sp = sub.add_parser("set-params", help="research: change risk limits within the human-set bounds "
                                           "(set_risk_params); `params` shows them")
    sp.add_argument("changes", nargs="+", type=_pair, metavar="KEY=VALUE")
    sp.add_argument("--experiment", required=True, help="tag that owns the params it sets")
    sp.add_argument("--reason", required=True)
    sub.add_parser("reset-params", help="drop every research override: back to the .env baseline").add_argument(
        "--reason", required=True)
    ul = sub.add_parser("update-learning", help="update a learning: evidence and provenance are appended, the rest "
                                                "replaced (update_learning)")
    ul.add_argument("learning_id", type=int)
    ul.add_argument("--status", choices=("hypothesis", "supported", "rejected", "adopted"))
    ul.add_argument("--evidence")
    ul.add_argument("--confidence", choices=("low", "medium", "high"))
    ul.add_argument("--provenance", action="append", help="repeatable")
    ul.add_argument("--kind", choices=LEARNING_KINDS)
    ul.add_argument("--n", type=float)
    ul.add_argument("--days", type=int)
    ul.add_argument("--sample")
    ul.add_argument("--review-by")
    sub.add_parser("start-session", help="start a trading session; acknowledges the alerts the brief listed "
                                         "(start_session)").add_argument("--notes", default="")
