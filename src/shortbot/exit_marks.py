"""Real-time advisory pass of the next-level check (exit_odds.py): at every closed 5m bar the guardian writes one
`next_level_marks` row per open position: the σ forecast and its parts, the three-way odds of the next target vs the
stop, the EV net of fees and funding, the analyst's own p from the plan, and at each closed 15m bar the fat-tailed
Monte Carlo odds too. `reassess` (and so the MCP tool) shows the newest row as `next_level_cached`.

SHADOW ONLY: it runs in the research lane, after and apart from the protective lane, each position under a timeout
and its own try/except, so it can never block or delay a stop. Nothing exits on it; research scores it against holding
(docs/POSITION_MANAGEMENT.md "Next-level check")."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from . import exit_odds

log = logging.getLogger("shortbot.exit_marks")

BAR_MS, MC_EVERY_MS = 300_000, 900_000   # closed forms per 5m bar; Monte Carlo per 15m bar
PER_POSITION_S = 20.0
MC_PATHS = 4000
MC_HISTORY_5M = 1000


def due_bar(now_ms: int, last_bar: int | None) -> int | None:
    """The newest closed 5m bar boundary if no mark was written for it yet."""
    bar = now_ms // BAR_MS * BAR_MS
    return bar if last_bar is None or bar > last_bar else None


async def mark(ex: Any, journal: Any, trade: dict[str, Any], pos: dict[str, Any], bar_ms: int) -> dict[str, Any] | None:
    """The row's payload for one position (None without a stop or a σ)."""
    sym, price, stop = pos["symbol"], pos["mark"], pos.get("exchange_stop") or trade.get("stop_price")
    hours = exit_odds.trade_horizon(trade)
    hub, sig, iv = await exit_odds.market_inputs(ex, sym, journal, hours)
    rate = None
    try:
        rate = (await ex.public.fetch_funding_rate(sym)).get("fundingRate")
    except Exception as e:  # noqa: BLE001 - funding unread: the block shows none (flagged)
        log.warning("funding rate unread for %s (%s: %s)", sym, type(e).__name__, e)
    opened = trade.get("opened_at")
    age = (bar_ms / 1000 - datetime.fromisoformat(opened).timestamp()) / 3600 if opened else 0.0
    nl = exit_odds.for_trade(trade, price, stop, age, sig.get("used"), rate, iv)
    if nl is None:
        return None
    entry = trade.get("entry_price") or pos.get("entry") or price
    advice = [a["action"] for a in exit_odds.actions(nl, (entry - price) / nl["r_unit"], entry)]
    out = {**nl, "sigma": {k: sig.get(k) for k in ("used", "ewma", "har", "parkinson", "model", "scale", "har_parts",
                                                   "gaps_5m")},
           "funding_rate_read": rate is not None, "analyst_p": (trade.get("plan") or {}).get("analyst_p"),
           "advice": advice, "bar_ms": bar_ms}
    if bar_ms % MC_EVERY_MS == 0 and nl["target"] is not None:
        out["mc"] = await _mc(hub, sym, nl)
    return out


async def _mc(hub: Any, sym: str, nl: dict[str, Any]) -> dict[str, Any] | None:
    """Fat-tailed odds for the same bracket, off the event loop."""
    from .probability.montecarlo import simulate_short
    flow = await hub.context(sym).flow_candles("5m", MC_HISTORY_5M)
    if not flow:
        return None
    sim = await asyncio.to_thread(simulate_short, [c[4] for c in flow], nl["price"], nl["stop"], [nl["target"]],
                                  nl["hours"], nl["sigma_hourly"], MC_PATHS, 1.0, 7, True, nl["side"],
                                  [c[0] for c in flow])
    if not sim.get("ok"):
        return None
    t = sim["targets"][0]
    return {"p_target_first": round(t["p_target_first"], 4), "p_stop_first": round(t["p_stop_first"], 4),
            "p_neither": round(t["p_neither"], 4), "avg_stop_loss_R": t["avg_stop_loss_R"],
            "hours_simulated": sim["hours_simulated"]}


async def run_pass(ex: Any, journal: Any, positions: list[dict[str, Any]], bar_ms: int) -> int:
    """One mark per open journal-tracked position; each under PER_POSITION_S and its own try/except. Returns rows
    written."""
    rows = []
    ts = datetime.fromtimestamp(bar_ms / 1000, UTC).isoformat(timespec="seconds")
    for p in positions:
        trade = journal.active_trade(p["symbol"])
        if not trade or trade.get("status") != "open":
            continue
        try:
            m = await asyncio.wait_for(mark(ex, journal, trade, p, bar_ms), PER_POSITION_S)
        except Exception:  # noqa: BLE001 - research logging never blocks the guardian
            log.exception("next-level mark %s failed", p["symbol"])
            continue
        if m is not None:
            rows.append((ts, bar_ms, journal.mode, trade["id"], p["symbol"], p["mark"], json.dumps(m, default=str)))
    if rows:   # one unit (#79)
        await journal.write(lambda j: j.db.executemany(
            "INSERT INTO next_level_marks (ts, bar_ms, mode, trade_id, symbol, price, data) VALUES (?,?,?,?,?,?,?)",
            rows))
    return len(rows)


def latest(journal: Any, trade_id: int) -> dict[str, Any] | None:
    """The newest mark of a trade, with its age; None before the first one."""
    try:
        r = journal.db.execute("SELECT ts, bar_ms, data FROM next_level_marks WHERE trade_id=? ORDER BY id DESC "
                               "LIMIT 1", (trade_id,)).fetchone()
    except Exception as e:  # noqa: BLE001 - a read aid: never fails reassess
        log.warning("next-level mark unread (%s: %s)", type(e).__name__, e)
        return None
    if not r:
        return None
    age_min = (datetime.now(UTC).timestamp() * 1000 - r[1]) / 60_000
    return {"ts": r[0], "age_min": round(age_min, 1), **json.loads(r[2])}
