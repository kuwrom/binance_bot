"""CLI-only commands (commands.py declarations, no MCP tool): the session brief, studies, the pattern registry, the
macro calendar, exchange probes, the audit view, backups and the SessionStart hook."""

from __future__ import annotations

import sys
from typing import Any

from . import audit_view, backup, probes
from .app import get_app
from .cli_brief import brief as session_brief
from .commands import Arg, command
from .weight import PROTECTIVE, RESEARCH


@command(mcp=False, cli="brief")
async def brief() -> str:
    """What the SessionStart hook prints: compact markdown state of the desk."""
    return await session_brief(get_app())


@command(mcp=False, cli="study", weight=RESEARCH, help="shadow study results: ladder variants vs hold | laggard "
         "catch-up | sweeps | patterns (week event study: pumps, sweeps, squeezes, funding x liquidity)", args=(
    Arg("name", choices=("ladder", "laggard", "sweeps", "patterns")),
    Arg("fetch", "--fetch", "patterns: refresh 10 days of hourly data for every perp")))
async def study(name: str, fetch: bool = False) -> dict[str, Any]:
    """Every study's trials go into the trial register (#31). sweeps is descriptive (fake-out size quantiles for stop
    placement), not a test."""
    from . import trial_register as TR
    from .backtest import register_rows as RR
    app = get_app()
    if name == "ladder":
        from .shadow_eval import evaluate as ev_ladder
        res, rows = ev_ladder(app.journal), RR.ladder_rows
    elif name == "patterns":
        from . import patterns as pt
        from . import patterns_data as ptd
        fetched = await ptd.fetch_all(app.ex, app.journal, days=10) if fetch else None
        res, rows = {"fetch": fetched, **pt.report(pt.run(ptd.load(app.journal)))}, RR.patterns_rows
    elif name == "laggard":
        from .laggard import report as lag_report
        res, rows = lag_report(app.journal), RR.laggard_rows
    else:
        from .sweeps import report as sweep_report
        return sweep_report(app.journal)
    return {**res, "trial_register_rows": TR.record(name, rows(res))}


@command(mcp=False, cli="sweeps", write=True, weight=RESEARCH,
         help="level-sweep study: how far price pokes past round numbers / 24h highs", args=(
    Arg("action", choices=("run", "report")), Arg("days", "--days"), Arg("top", "--top", "most liquid N perps")))
async def sweeps(action: str, days: float = 3, top: int = 60) -> dict[str, Any]:
    from . import sweeps as sw
    app = get_app()
    if action == "run":
        return await sw.run_study(app.ex, app.journal, days, top)
    return sw.report(app.journal)


@command(mcp=False, cli="registry", write=True, help="pattern registry: tree | card ID | search TEXT | seed | status "
         "ID --status S | forward [ID] (the pre-registered forward test of every non-draft node)", args=(
    Arg("action", choices=("tree", "card", "search", "seed", "status", "forward")), Arg("arg", nargs="?"),
    Arg("status", "--status", choices=("draft", "shadow", "live", "probation", "retired")), Arg("reason", "--reason"),
    Arg("bt_dir", "--bt-dir", "seed: backtest output dir (results.json + trades.json)")))
async def registry(action: str, arg: str | None = None, status: str | None = None, reason: str | None = None,
                   bt_dir: str | None = None) -> Any:
    from . import registry as R
    j = get_app().journal
    if action == "seed":
        from . import patterns_data
        from .registry_seed import feed_candidates, seed_all
        return {**seed_all(j, bt_dir, patterns_data.load(j)), "scan_episodes": feed_candidates(j)}
    if action == "card":
        return (R.card(j.db, arg) if arg is not None else None) or {"error": "unknown pattern"}
    if action == "search":   # learnings in full: the `learnings` command
        from .learnings_view import text
        return {"patterns": R.search(j.db, arg or "", 10),
                "learnings": [{"id": r["id"], "status": r["status"], "setup": r["setup"], "text": text(r)}
                              for r in j.search_learnings(arg or "", limit=10)]}
    if action == "forward":
        from .registry_forward import report as forward_report
        return forward_report(j.db, arg)
    if action == "status":
        if not (arg and status):
            return {"error": "usage: shortbot registry status ID --status S --reason R"}
        return R.set_status(j.db, arg, status, reason or "")
    return {"tree": R.tree(j.db), "decay_alarms": R.decay_alarms(j.db)}


@command(mcp=False, cli="macro-add", write=True, help="record a scheduled macro event (source required)", args=(
    Arg("name"), Arg("when", "--when", "ISO time WITH offset, e.g. 2026-09-30T08:30:00-04:00"),
    Arg("kind", "--kind", choices=("tier1", "tier2", "fomc", "maintenance")), Arg("source", "--source")))
async def macro_add(name: str, when: str, kind: str, source: str) -> dict[str, Any]:
    from datetime import datetime

    from .market.calendar import MacroCalendar
    MacroCalendar(get_app().settings.db_path).add(int(datetime.fromisoformat(when).timestamp() * 1000), name, kind,
                                                  source)
    return {"ok": True}


@command(mcp=False, cli="macro-refresh", write=True,
         help="pull BLS + ForexFactory + FOMC dates into the macro calendar")
async def macro_refresh() -> Any:
    from .market.calendar import MacroCalendar
    return await MacroCalendar(get_app().settings.db_path).refresh()


@command(mcp=False, cli="probe", write=True, weight=PROTECTIVE, help="DEMO only: force one exchange behaviour the desk "
         "assumes, at minimum size, and journal what Binance answered (probe_result)", args=(
    Arg("name", nargs="?", choices=tuple(probes.ORDER), help="the probe (or --all)"),
    Arg("symbol", "--symbol", "a liquid perp the desk isn't using"),
    Arg("all", "--all", "every probe in a safe order (about 3-4 minutes)")))
async def probe(name: str | None = None, symbol: str = probes.DEFAULT_SYMBOL, all: bool = False) -> dict[str, Any]:
    """One probe prints in full; --all prints each probe without its call log (that is in its journal row)."""
    tr = get_app().trader
    if all:
        out = await probes.run_all(tr, symbol)
        return {**out, "results": [{**r, "calls": f"{len(r['calls'])} calls: decision #{r['decision_id']}"}
                                   if "decision_id" in r else r for r in out["results"]]}
    if not name:
        return {"ok": False, "error": f"name a probe ({', '.join(probes.ORDER)}) or pass --all"}
    return await probes.run(tr, name, symbol)


@command(mcp=False, cli="audit", help="who did what: decisions and tool/CLI calls with actor, Claude session, code",
         args=(
    Arg("since", "--since", "24h, 7d, 30m or an ISO time (UTC)"),
    Arg("actor", "--actor", "prefix: mcp, cli, guardian, admin, hook, or e.g. mcp:open_short"),
    Arg("action", "--action", "one action, e.g. call, rejected, open"), Arg("limit", "--limit")))
async def audit(since: str | None = None, actor: str | None = None, action: str | None = None,
                limit: int = 50) -> dict[str, Any]:
    return audit_view.report(get_app().journal, since, actor, action, limit)


command(mcp=False, cli="backup", raw=True, args=backup.ARGS,
        help="verified journal backup (no exchange) | restore FILE --to NEW_PATH")(backup.run_cli)


@command(mcp=False, cli="hook-session", raw=True,
         help="SessionStart hook: record the Claude session's model (reads hook JSON on stdin)")
def hook_session(_args) -> int:
    """No exchange app and no output: the hook's stdout would enter the context."""
    return audit_view.hook_session(sys.stdin)
