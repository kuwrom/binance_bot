"""`shortbot brief`: what the SessionStart hook prints into Claude's context (fast, never fails hard).

Hook output is not a tool result, so it never carries third-party text (headlines, announcement titles, calendar
titles): alerts show kind, symbol, time and id, and point to the tool that returns the text as untrusted data. The
brief only reads (alerts stay until start_session acknowledges them), apart from the guardian watchdog and a setup
ladder stop seen for the first time, which it records and alerts once, as sizing would (setup_ladder.status)."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime

from . import alerts_view, desk_limits, gateway_health, killswitch, procs, setup_ladder_view
from .entries import EntryDesk
from .journal_writer import connect
from .untrusted import clean

BRIEF_TIMEOUT_S = 20
GOLIVE_TIMEOUT_S: float = BRIEF_TIMEOUT_S   # the go-live line's bound, as every other line's
# Claude Code shows a SessionStart hook's output inline only up to 10,000 characters; past that the session sees a
# 2 KB preview, which cut the rulebook off (2026-10-05). The alerts go last and are trimmed first.
BRIEF_MAX_CHARS = 9000
BACKUP_MAX_AGE_H = 36


async def brief(app) -> str:
    """Compact markdown state for the start of a Claude session."""
    j, lines = app.journal, []
    desk = "LIVE desk, REAL MONEY: docs/DESKS.md \"The live desk\"" if app.settings.is_live else "demo mode"
    lines.append(f"## shortbot state ({desk}, experiment: {app.ex.experiment or 'baseline'})")
    try:   # the watchdog may trip HALTED here: the guardian can't report its own death
        await asyncio.wait_for(killswitch.watch_guardian(app.trader), BRIEF_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        lines.append(f"- ⚠️ guardian watchdog failed: {type(e).__name__}: {e}")
    lines.append(killswitch.brief_line(j))
    if gw := gateway_health.brief_line(j, app.settings):   # SHORTBOT_GATEWAY on: is the order gateway up (#36)
        lines.append(gw)
    try:   # setups the ladder sizes down (#67): one line, only when some setup is HALF, SHADOW or UNKNOWN
        lines += [ln for ln in (setup_ladder_view.brief_line(j),) if ln]
    except Exception as e:  # noqa: BLE001
        lines.append(f"- ⚠️ setup ladder check failed: {type(e).__name__}: {e}")
    lines.append(backup_line(j))
    try:   # the human's desk limits on this desk (desk_limits.py): what the guardian and every check size under
        lines.append(desk_limits.brief_line(app.settings, j))
    except Exception as e:  # noqa: BLE001
        lines.append(f"- ⚠️ desk limits unreadable: {type(e).__name__}: {e}")
    # the go-live gate (#46): never raises, bounded; parameters vs the live desk's baseline
    lines.append(await golive_line(j, desk_limits.baseline(app.settings, j, "live")))
    try:   # long-lived processes still on code older than the code on disk (#58)
        lines.append(await asyncio.wait_for(procs.brief_line(j), BRIEF_TIMEOUT_S))
    except Exception as e:  # noqa: BLE001
        lines.append(f"- ⚠️ stale-process check failed: {type(e).__name__}: {e}")
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
    try:   # focus coins (coin_memory.py): state, read age and bias, events; never third-party text
        from .coin_memory import brief_lines as coin_lines
        lines += coin_lines(j)
    except Exception as e:  # noqa: BLE001
        lines.append(f"- ⚠️ could not load coin memory: {type(e).__name__}: {e}")
    try:   # the rulebook, what is relevant now, what to re-check
        from . import learnings_brief
        lines += learnings_brief.section(j, orders)
    except Exception as e:  # noqa: BLE001
        lines.append(f"### ⚠️ could not load learnings: {type(e).__name__}: {e}")
    from .warmup import desk_clock
    dc = desk_clock(app.settings, j)
    c = dc["clock"]
    lines.append(f"### clock: {c['utc']} {c['weekday']} "
                 f"{c['session']} session, funding in {c['min_to_funding']:.0f} min; desk "
                 f"{'COLD (' + dc['desk']['why'] + '): warm-up before entries' if dc['desk']['cold'] else 'warm'}")
    lines += [f"- macro: {clean(e['name'], 40)} ({e['kind']}) {e['utc']}"   # calendar titles are third-party
              for e in dc["macro_week"] if e["minutes_to"] >= 0][:4]
    stats = j.stats()
    lines.append(f"### record: {stats['closed_trades']} closed trades, net {stats['total_pnl']} USDT; "
                 f"{len(j.candidates(labelled_only=True, limit=100000))} labelled research candidates")
    try:
        alerts = alerts_view.brief_lines(j)
    except Exception as e:  # noqa: BLE001
        alerts = [f"### ⚠️ could not load alerts: {type(e).__name__}: {e}"]
    return fit(lines, alerts)


def fit(lines: list[str], alerts: list[str], limit: int = BRIEF_MAX_CHARS) -> str:
    """The brief within `limit` characters: everything else whole, then as many alert lines as fit (the header and
    the urgent ones first), then a pointer to the rest."""
    out, more = list(lines), "- … more alerts: run `journal` (start_session acknowledges them all)"
    used = len("\n".join(out))
    for ln in alerts:
        if used + len(ln) + len(more) + 2 > limit:
            out.append(more)
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out)


def _settle(fut: asyncio.Future[str], res: str | None, err: Exception | None) -> None:
    if not fut.done():   # else it timed out: nobody waits for it any more
        if err is not None:
            fut.set_exception(err)
        else:
            fut.set_result(res or "")


def _off_loop(fn: Callable[[], str]) -> asyncio.Future[str]:
    """fn() on a daemon thread. Not asyncio.to_thread: asyncio.run joins its executor at exit, so a read left running
    past its timeout would still hold the hook's process; a daemon thread is dropped at exit."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()

    def run() -> None:
        res, err = None, None
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001 - handed to the waiter
            err = e
        try:
            loop.call_soon_threadsafe(_settle, fut, res, err)
        except RuntimeError:   # the loop closed meanwhile
            pass
    threading.Thread(target=run, name="brief-golive", daemon=True).start()
    return fut


def _golive(j, limits, now: datetime | None, path: str) -> str:
    """golive_view.brief_line on a connection of its own to the journal file at `path` ('' = ':memory:': j itself)."""
    from . import golive_view
    if not path:
        return golive_view.brief_line(j, limits, now)
    view = copy.copy(j)
    view.db = connect(path)
    view.db.row_factory = sqlite3.Row
    try:
        return golive_view.brief_line(view, limits, now)
    finally:
        view.db.close()


async def golive_line(j, limits, now: datetime | None = None) -> str:
    """The go-live gate's line (golive_view.brief_line, a light check), bounded like every brief line: a slow or
    failing journal gives a short `unavailable` line, never a stalled or failed brief. It reads off the event loop on
    a connection of its own (a journal file), so a read left running past the bound never shares this one."""
    try:
        path = next((r[2] for r in j.db.execute("PRAGMA database_list") if r[1] == "main"), "")   # no file I/O
        return await asyncio.wait_for(_off_loop(lambda: _golive(j, limits, now, path)), GOLIVE_TIMEOUT_S)
    except TimeoutError:
        return f"- go-live: unavailable (no answer in {GOLIVE_TIMEOUT_S:g} s; `shortbot golive` for the report)"
    except Exception as e:  # noqa: BLE001
        return f"- go-live: unavailable ({type(e).__name__}: {str(e)[:80]})"


def backup_line(j, now: float | None = None) -> str:
    """Age of the newest verified backup (kv backup_last_ok, written by `shortbot backup`)."""
    try:
        b = json.loads(j.get("backup_last_ok") or "null")
        age_h = ((now or time.time()) - datetime.fromisoformat(b["ts"]).timestamp()) / 3600
    except (ValueError, TypeError, KeyError):
        return "- ⚠️ backup: no verified backup recorded (kv backup_last_ok): check the `shortbot backup` job"
    off = {False: "; ⚠️ off-box copy FAILED", None: ", local only"}.get(b.get("offbox_ok"), "")
    if age_h > BACKUP_MAX_AGE_H:
        return (f"- ⚠️ backup: last verified copy {age_h:.0f} h ago (> {BACKUP_MAX_AGE_H} h): check the backup job"
                f"{off}")
    return f"- backup verified {age_h:.0f} h ago{off}"
