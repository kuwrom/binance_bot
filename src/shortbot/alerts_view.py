"""Alerts as the agent sees them: urgent kinds first, true counts, third-party text never in the brief, and an explicit
acknowledgement. The brief (SessionStart hook: startup, resume, clear, compact) only READS: alerts stay listed until
`start_session` acknowledges them, so a compaction, a resumed session or a coding session never swallows one.
The watermark is an alert id (kv alerts_ack_id); ids never collide the way second-resolution timestamps do."""

from __future__ import annotations

from typing import Any

from .untrusted import flat, third_party_alert, wrap

ACK_KEY = "alerts_ack_id"
LEGACY_KEY = "last_brief_ts"   # before #22 the brief advanced this on every run
# Kinds the brief always lists first (newest first, up to URGENT_CAP). Exact kinds; a trailing "*" is a prefix.
# Add new protective / exchange-truth alerts here.
URGENT_KINDS = (
    "kill_switch*", "guardian_failing*", "guardian_error*", "fill_while_*", "positions_empty", "position_watch_error",
    "NAKED_POSITION", "forced_close", "close_incomplete", "close_unknown", "entry_unknown", "add_unknown",
    "stop_missing", "stop_restore_failed", "stop_failed", "widen_undone", "position_flip", "bridge_left",
    "bridge_swept", "unexpected_long", "untracked_position", "fill_unresolved", "entry_cancel_failing",
    "zombie_order", "external_order", "daily_loss_limit", "governor_halt", "squeeze_risk", "price_spike",
    "funding_hourly", "backup_failed", "deploy_failed", "deploy_blocked", "plan_stop_unreachable", "adl_risk_persistent", "margin_not_isolated",
    "margin_asset", "margin_thin", "margin_check_error", "multi_assets_mode", "target_cancel_failed",
    "prearm_left", "stop_left", "orphan_stop_*", "unexpected_error",   # #37: stops left behind, order-path bugs
    "break_watch",   # an open short's break score reached 2 of #81's checks (guardian_coins.py); alerts never exit
)
URGENT_ON_BOOK = ("binance_delisting",)   # urgent when it names a coin on our book (#23: the alert carries its symbol)
RECOVERED, FAILED = "guardian_recovered:", ("guardian_failing:", "guardian_error:")   # + the guardian job's name
URGENT_CAP = 8      # the brief must fit the SessionStart hook (cli_brief.BRIEF_MAX_CHARS); the rest: journal
ROUTINE_SHOWN = 5


def is_urgent(kind: str, symbol: str | None = None) -> bool:
    return (kind in URGENT_ON_BOOK and bool(symbol)) or \
        any(kind.startswith(k[:-1]) if k.endswith("*") else kind == k for k in URGENT_KINDS)


def _rows(j, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    """SQL may call urgent(kind, symbol): the Python list above stays the only definition."""
    j.db.create_function("urgent", 2, lambda k, s: int(is_urgent(k, s)), deterministic=True)
    return [dict(r) for r in j.db.execute(sql, args).fetchall()]


# Market-wide jobs: the market guardian runs them for both desks (guardian.MARKET_JOBS), so both sessions see their
# alerts, and the general listing/delisting heads-up (guardian_jobs._announce: no symbol on a book).
MARKET_ACTORS = tuple(f"guardian:{j}" for j in (
    "social", "refresh", "housekeeping", "processes", "metrics", "research", "calendar", "market_map", "sweeps",
    "supply", "laggard", "long_shadow", "scan", "cadence_shadow", "mtf_views", "onchain"))
# this desk's alerts (rows from before #32 have no mode: demo's) and the shared market ones
MINE = (f"(COALESCE(mode, 'demo') = ? OR actor IN ({', '.join(repr(a) for a in MARKET_ACTORS)}) OR "
        "(kind IN ('binance_new_listing', 'binance_delisting') AND symbol IS NULL))")


def ack_id(j) -> int:
    """Newest acknowledged alert id, per desk (Journal.mode_key). Before the first acknowledgement: the last alert the
    old brief showed (demo only)."""
    raw = j.get(j.mode_key(ACK_KEY))
    if raw is not None and raw.isdigit():
        return int(raw)
    legacy = j.get(LEGACY_KEY) if j.mode == "demo" else None
    if not legacy:
        return 0
    row = j.db.execute("SELECT MAX(id) FROM alerts WHERE ts <= ?", (legacy,)).fetchone()
    return int(row[0] or 0)


def _line(a: dict[str, Any], mark: str = "", n: int = 1) -> str:
    head = f"- {mark}#{a['id']} {a['ts'][5:16].replace('T', ' ')} {a['kind']} {a['symbol'] or ''}".rstrip()
    head += f" ({n}x)" if n > 1 else ""
    if third_party_alert(a["kind"]):
        where = f"coin_news {a['symbol'].split('/')[0]}" if a["kind"] == "news_on_position" and a["symbol"] \
            else "latest_announcements"
        return f"{head}: third-party text, fetch via {where}"
    return f"{head}: {flat(a['message'])}"


def brief_lines(j) -> list[str]:
    after = ack_id(j)
    counts = _rows(j, f"SELECT kind, urgent(kind, symbol) AS u, COUNT(*) AS n FROM alerts WHERE id > ? AND {MINE} "
                      "GROUP BY kind, u ORDER BY n DESC, kind", (after, j.mode))
    total = sum(r["n"] for r in counts)
    if not total:
        return [f"### alerts: none unacknowledged (through #{after})"]
    n_urgent = sum(r["n"] for r in counts if r["u"])
    out = [f"### alerts: {total} unacknowledged since #{after} ({n_urgent} urgent); start_session acknowledges them"]
    if n_urgent:
        urgent, over = _urgent_first_open(j, after)
        out += [_line(a, "🚨 (recovered since) " if a["recovered"] else "🚨 ") for a in urgent]
        if n_urgent > URGENT_CAP:
            out.append(f"- 🚨 {n_urgent - URGENT_CAP} more urgent"
                       + (f" ({over} of them guardian errors that recovered)" if over else "")
                       + ": see the journal tool")
    routine = [r for r in counts if not r["u"]]
    if routine:
        n = sum(r["n"] for r in routine)
        out.append(f"- routine ({n}): " + ", ".join(f"{r['kind']} {r['n']}" for r in routine))
        groups = _rows(j, f"SELECT MAX(id) AS id, COUNT(*) AS n FROM alerts WHERE id > ? AND {MINE} AND NOT "
                          "urgent(kind, symbol) GROUP BY kind, symbol ORDER BY MAX(id) DESC LIMIT ?",
                       (after, j.mode, ROUTINE_SHOWN))
        rows = {r["id"]: r for r in _rows(j, f"SELECT * FROM alerts WHERE id IN ({','.join('?' * len(groups))})",
                                          tuple(g["id"] for g in groups))}
        out += [_line(rows[g["id"]], n=g["n"]) for g in groups]   # newest per (kind, symbol): a flood takes one line
    return out


def _urgent_first_open(j, after: int) -> tuple[list[dict[str, Any]], int]:
    """(the URGENT_CAP urgent alerts to list, how many recovered ones the cap left out). A guardian job's
    guardian_failing / guardian_error with a later guardian_recovered for the same job is over: it stays urgent and
    counted, but is listed after the alerts still open, so a timeout that healed never pushes a live one past the cap.
    Newest first within each part."""
    healed = {r["job"]: r["id"] for r in _rows(
        j, f"SELECT substr(kind, {len(RECOVERED) + 1}) AS job, MAX(id) AS id FROM alerts WHERE id > ? AND {MINE} AND "
           "kind LIKE ? GROUP BY job", (after, j.mode, RECOVERED + "%"))}
    ids = _rows(j, f"SELECT id, kind FROM alerts WHERE id > ? AND {MINE} AND urgent(kind, symbol) ORDER BY id DESC",
                (after, j.mode))
    done = {a["id"] for a in ids if a["kind"].startswith(FAILED) and healed.get(a["kind"].split(":", 1)[1], 0) > a["id"]}
    order = [a["id"] for a in ids if a["id"] not in done] + [a["id"] for a in ids if a["id"] in done]
    show = order[:URGENT_CAP]
    rows = {r["id"]: r for r in _rows(j, f"SELECT * FROM alerts WHERE id IN ({','.join('?' * len(show))})",
                                      tuple(show))}
    return [{**rows[i], "recovered": i in done} for i in show], len(done - set(show))


def acknowledge(j) -> dict[str, Any]:
    """Mark every alert of this desk up to the newest as seen and return what was acknowledged (so nothing is acked
    unseen). The other desk's alerts stay for its own session."""
    after = ack_id(j)
    through = int(j.db.execute(f"SELECT MAX(id) FROM alerts WHERE {MINE}", (j.mode,)).fetchone()[0] or after)
    rows = _rows(j, f"SELECT kind, COUNT(*) AS n FROM alerts WHERE id > ? AND id <= ? AND {MINE} GROUP BY kind "
                    "ORDER BY n DESC", (after, through, j.mode))
    items = _rows(j, f"SELECT * FROM alerts WHERE id > ? AND id <= ? AND {MINE} AND urgent(kind, symbol) "
                     "ORDER BY id DESC LIMIT ?", (after, through, j.mode, URGENT_CAP))
    j.set(j.mode_key(ACK_KEY), str(through))
    return {"through_id": through, "count": sum(r["n"] for r in rows), "by_kind": {r["kind"]: r["n"] for r in rows},
            "urgent": [for_agent(a) for a in items]}


def for_agent(a: dict[str, Any]) -> dict[str, Any]:
    """An alert row for a tool result: third-party text (headlines, announcement titles) only as untrusted data."""
    if third_party_alert(a["kind"]):
        return {**a, "message": wrap(a["message"], f"alert:{a['kind']}")}
    return a


def kind_of(j, alert_id: int) -> str | None:
    row = j.db.execute("SELECT kind FROM alerts WHERE id=?", (alert_id,)).fetchone()
    return row[0] if row else None
