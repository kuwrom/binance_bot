"""`shortbot brief`: what the SessionStart hook prints into Claude's context (fast, never fails hard)."""

from __future__ import annotations

import asyncio
import time

from . import killswitch
from .entries import EntryDesk

BRIEF_TIMEOUT_S = 20


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
