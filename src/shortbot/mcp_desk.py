"""Desk-state MCP tools (read-only): risk governor, session clock, cold-desk warm-up. Registered on the
shared server by importing this module from mcp_server."""

from __future__ import annotations

from typing import Any

from .market import regime
from .app import get_app
from .mcp_server import tool


@tool
async def governor_status() -> dict[str, Any]:
    """Account breakers (docs/RISK_GOVERNANCE.md): daily cap (balance x regime x BTC vol), weekly cap, drawdown
    tiers, loss streak, give-back stop, plus the hot-hand test over closed trades. Check before sizing up."""
    from .risk_budget import governor_status as gs
    app = get_app()
    return await gs(app.trader, (await regime.market_regime(app.ex))["verdict_for_shorts"])


@tool
async def session_clock() -> dict[str, Any]:
    """UTC session, minutes to funding / day and week close / US open, soft volatility warnings, scheduled macro
    events (48h) and whether the desk is COLD (idle >= 6h or first session of the UTC day: warm up first)."""
    from .warmup import desk_clock
    app = get_app()
    return desk_clock(app.settings, app.journal)


@tool
async def warmup_start(symbols: list[str] | None = None) -> dict[str, Any]:
    """Cold-desk warm-up, step 1: t0 snapshot (price, mark/index/basis, funding + interval, OI, spread, depth
    ±1%, 1m vol, BTC 15m) of the candidates (default: pending limit entries + top 5 gainers). Study the tape for
    15 minutes, then call warmup_check."""
    from .warmup import WarmUp
    app = get_app()
    return await WarmUp(app.ex, app.journal).start(symbols)


@tool
async def warmup_check() -> dict[str, Any]:
    """Cold-desk warm-up, step 2 (>= 15 min after start): t+15 snapshot and a verdict per coin (persisted, or
    dropped with reasons). Completing it warms the desk; both snapshots are kept for the persistence study."""
    from .warmup import WarmUp
    app = get_app()
    return await WarmUp(app.ex, app.journal).check()


@tool
async def market_map(fresh: bool = False) -> str:
    """The whole tape at once: regime, breadth, dispersion, BTC/ETH, and every sector ranked by its move net of BTC
    (median returns, breadth, volume surge, funding tilt, leader/laggard, rotation in/out), plus market pauses and
    sector vetoes. Read it before judging any single coin (docs/MARKET_MAP.md)."""
    from .market.market_map import build_map, cached, render
    app = get_app()
    m = (None if fresh else cached(app.journal, 900)) or await build_map(app.ex, app.journal)
    return render(m)


@tool
async def coin_profile(symbol: str) -> dict[str, Any]:
    """Per-coin behaviour: how it acts at round numbers / 24h highs (sweep study), where its moves and volume happen
    by session (30d), sector/beta context, our past trades on it. Coin numbers are shrunk toward the all-coin value
    (w = n/(n+30)): treat low-w numbers as anecdotes."""
    from .profile import build_profile
    app = get_app()
    return await build_profile(app.ex, app.journal, symbol)


@tool
async def read_coin(symbol: str) -> dict[str, Any]:
    """What game is this coin playing right now and how do we profit? Lifecycle stage (new listing, pumping,
    post-pump, bleeding, unlock ahead, delisting risk) x the game on the tape x controller label (thin float,
    insider overhang, MM range, organic) x session -> playbook, size note, stops, and layered evidence
    (universe -> archetype -> coin). 'observe' = no known script fits: log it, don't force a trade."""
    from .read import read_coin as rc
    app = get_app()
    return await rc(app.ex, app.journal, symbol)


@tool
async def position_ladder(symbol: str) -> dict[str, Any]:
    """3-level ladder for an open short (docs/LADDER.md): T1-T3 with no-edge p0 and edge-adjusted odds, EV in R
    net of costs, the state score (squeeze / tape / breadth / time parts) and the exposure the rule would hold.
    SHADOW MODE: informative only; it is being validated against hold-to-plan and a price-only placebo."""
    from .exchange import to_symbol
    from .ladder import build_ladder
    app = get_app()
    pos = [p for p in await app.trader.positions() if p["symbol"] == to_symbol(symbol)]
    return await build_ladder(app.ex, app.journal, pos[0], log=False) if pos else {"error": "no open position"}
