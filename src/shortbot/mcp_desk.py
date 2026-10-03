"""Desk-state commands (commands.py: MCP tools and `shortbot` subcommands): risk governor, session clock, cold-desk
warm-up, market map, coin profile and read, the position ladder."""

from __future__ import annotations

from typing import Any

from .app import get_app
from .commands import Arg, command
from .market import regime
from .weight import RESEARCH


@command(cli="governor", help="account breakers: daily/weekly cap, drawdown, streak, give-back")
async def governor_status() -> dict[str, Any]:
    """Account breakers (docs/RISK_GOVERNANCE.md): daily cap (balance x regime x BTC vol), weekly cap, drawdown
    tiers, loss streak, give-back stop, plus the hot-hand test over closed trades. Check before sizing up."""
    from .risk_budget import governor_status as gs
    app = get_app()
    return await gs(app.trader, (await regime.market_regime(app.ex))["verdict_for_shorts"])


@command(cli="clock", help="UTC session, funding, US open, macro calendar, cold/warm desk")
async def session_clock() -> dict[str, Any]:
    """UTC session, minutes to funding / day and week close / US open, soft volatility warnings, scheduled macro
    events (48h) and whether the desk is COLD (idle >= 6h or first session of the UTC day: warm up first)."""
    from .warmup import desk_clock
    app = get_app()
    return desk_clock(app.settings, app.journal)


@command(write=True)
async def warmup_start(symbols: list[str] | None = None) -> dict[str, Any]:
    """Cold-desk warm-up, step 1: t0 snapshot (price, mark/index/basis, funding + interval, OI, spread, depth
    ±1%, 1m vol, BTC 15m) of the candidates (default: pending limit entries + top 5 gainers). Study the tape for
    15 minutes, then call warmup_check."""
    from .warmup import WarmUp
    app = get_app()
    return await WarmUp(app.ex, app.journal).start(symbols)


@command()
async def warmup_check() -> dict[str, Any]:
    """Cold-desk warm-up, step 2 (>= 15 min after start): t+15 snapshot and a verdict per coin (persisted, or
    dropped with reasons). Completing it warms the desk; both snapshots are kept for the persistence study."""
    from .warmup import WarmUp
    app = get_app()
    return await WarmUp(app.ex, app.journal).check()


@command(mcp=False, cli="warmup", write=True, help="cold-desk 15-min study: start = t0 snapshot, check = t+15 verdicts",
         args=(Arg("action", choices=("start", "check")),
               Arg("symbols", nargs="*", help="default: pending limit entries + top 5 gainers")))
async def warmup(action: str, symbols: list[str] | None = None) -> dict[str, Any]:
    """Both warm-up steps in one subcommand (logged as one write, as warmup_start is)."""
    return await (warmup_start(symbols or None) if action == "start" else warmup_check())


@command(cli="map", weight=RESEARCH, help="market map: regime, breadth, sectors vs BTC, rotation, vetoes", args=(
    Arg("fresh", "--fresh", "rebuild now instead of using the guardian's cached map"),))
async def market_map(fresh: bool = False) -> str:
    """The whole tape at once: regime, breadth, dispersion, BTC/ETH, and every sector ranked by its move net of BTC
    (median returns, breadth, volume surge, funding tilt, leader/laggard, rotation in/out), plus market pauses and
    sector vetoes. Read it before judging any single coin (docs/MARKET_MAP.md)."""
    from .market.market_map import build_map, cached, render
    app = get_app()
    m = (None if fresh else cached(app.journal, 900)) or await build_map(app.ex, app.journal)
    return render(m)


@command(cli="profile", help="per-coin behaviour: levels, sessions, context, our trades (shrunk)",
         args=(Arg("symbol"),))
async def coin_profile(symbol: str) -> dict[str, Any]:
    """Per-coin behaviour: how it acts at round numbers / 24h highs (sweep study), where its moves and volume happen
    by session (30d), sector/beta context, our past trades on it. Coin numbers are shrunk toward the all-coin value
    (w = n/(n+30)): treat low-w numbers as anecdotes."""
    from .profile import build_profile
    app = get_app()
    return await build_profile(app.ex, app.journal, symbol)


@command(cli="read", help="what game is this coin playing: lifecycle x tape x controller -> playbook",
         args=(Arg("symbol"),))
async def read_coin(symbol: str) -> dict[str, Any]:
    """What game is this coin playing right now and how do we profit? Lifecycle stage (new listing, pumping,
    post-pump, bleeding, unlock ahead, delisting risk) x the game on the tape x controller label (thin float,
    insider overhang, MM range, organic) x session -> playbook, size note, stops, and layered evidence
    (universe -> archetype -> coin). 'observe' = no known script fits: log it, don't force a trade."""
    from .read import read_coin as rc
    app = get_app()
    return await rc(app.ex, app.journal, symbol)


@command()
async def position_ladder(symbol: str) -> dict[str, Any]:
    """3-level ladder for an open short (docs/LADDER.md): T1-T3 with no-edge p0 and edge-adjusted odds, EV in R
    net of costs, the state score (squeeze / tape / breadth / time parts) and the exposure the rule would hold.
    Read-only. For most trades it is shadow, validated against hold-to-plan and a price-only placebo. A trade in the
    ladder_live_v1 experiment (plan.ladder_live == "v1") acts on it via `shortbot ladder SYM --apply` (preview with
    --dry-run)."""
    from .exchange import to_symbol
    from .ladder import build_ladder
    app = get_app()
    pos = [p for p in await app.trader.positions() if p["symbol"] == to_symbol(symbol)]
    return await build_ladder(app.ex, app.journal, pos[0], log=False) if pos else {"error": "no open position"}


@command(mcp=False, cli="ladder", write=True, help="3-level ladder: per-level odds, edge score, exposure the rule wants "
         "(shadow); --enable opts a trade into ladder_live_v1, --apply executes its decision", args=(
    Arg("symbol"), Arg("enable", "--enable", "opt this trade into the live ladder (pins 1R, full size)"),
    Arg("apply", "--apply", "execute the decision: cut / add (<= 1R) / stop re-level"),
    Arg("dry_run", "--dry-run", "show what --apply would do, without orders")))
async def ladder(symbol: str, enable: bool = False, apply: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """position_ladder, plus the live-ladder actions (CLI only: the agent runs them at reviews, never the guardian)."""
    if not (enable or apply or dry_run):
        return await position_ladder(symbol)
    from . import ladder_apply as la
    tr = get_app().trader
    return await (la.enable(tr, symbol) if enable else la.apply(tr, symbol, dry_run=dry_run))
