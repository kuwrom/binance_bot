"""`shortbot brief`: what the SessionStart hook prints into Claude's context (fast, never fails hard).

Hook output is not a tool result, so it never carries third-party text (headlines, announcement titles, calendar
titles): alerts show kind, symbol, time and id, and point to the tool that returns the text as untrusted data. The
brief only reads (alerts stay until start_session acknowledges them), apart from the guardian watchdog and a setup
ladder stop seen for the first time, which it records and alerts once, as sizing would (setup_ladder.status)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

from . import alerts_view, killswitch, procs, setup_ladder_view
from .entries import EntryDesk
from .untrusted import clean, flat, third_party_alert, third_party_tag

BRIEF_TIMEOUT_S = 20
BACKUP_MAX_AGE_H = 36


async def brief(app) -> str:
    """Compact markdown state for the start of a Claude session."""
    j, lines = app.journal, []
    lines.append(f"## shortbot state ({app.settings.mode} mode, experiment: {app.ex.experiment or 'baseline'})")
    try:   # the watchdog may trip HALTED here: the guardian can't report its own death
        await asyncio.wait_for(killswitch.watch_guardian(app.trader), BRIEF_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        lines.append(f"- ⚠️ guardian watchdog failed: {type(e).__name__}: {e}")
    lines.append(killswitch.brief_line(j))
    try:   # setups the ladder sizes down (#67): one line, only when some setup is HALF, SHADOW or UNKNOWN
        lines += [ln for ln in (setup_ladder_view.brief_line(j),) if ln]
    except Exception as e:  # noqa: BLE001
        lines.append(f"- ⚠️ setup ladder check failed: {type(e).__name__}: {e}")
    lines.append(backup_line(j))
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
    try:
        lines += alerts_view.brief_lines(j)
    except Exception as e:  # noqa: BLE001
        lines.append(f"### ⚠️ could not load alerts: {type(e).__name__}: {e}")
    ls = j.learnings()
    if ls:
        from . import learnings_view   # kind / sample / review date (#28): flags, never gates
        try:
            ls = learnings_view.annotate(j, ls)
        except Exception as e:  # noqa: BLE001
            lines.append(f"- ⚠️ could not audit learnings: {type(e).__name__}: {e}")
        lines.append("### active learnings")
        lines += [f"- #{x['id']} [{x['status']}/{x['confidence']}"
                  + (f" {learnings_view.tag(x['audit'])}" if "audit" in x else "")
                  + f"] {x['setup'] or 'all'}: {flat(x['statement'], 300)}"
                  + (" ⚠️ evidence from third-party text" if _third_party(j, x.get("provenance")) else "")
                  for x in ls[:10]]
        lines += learnings_view.brief_lines(ls) if all("audit" in x for x in ls) else []
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
    return "\n".join(lines)


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


def _third_party(j, provenance: list[str] | None) -> bool:
    """Did a learning's evidence come from third-party text (a news tool, or a news/announcement alert)?"""
    for tag in provenance or []:
        if third_party_tag(tag):
            return True
        if tag.startswith("alert:") and tag[6:].isdigit():
            kind = alerts_view.kind_of(j, int(tag[6:]))
            if kind and third_party_alert(kind):
                return True
    return False
