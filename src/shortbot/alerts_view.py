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
    "funding_hourly", "backup_failed", "plan_stop_unreachable", "adl_risk_persistent", "margin_not_isolated",
    "margin_asset", "margin_thin", "margin_check_error", "multi_assets_mode", "target_cancel_failed",
    "prearm_left", "stop_left", "orphan_stop_*", "unexpected_error",   # #37: stops left behind, order-path bugs
    "break_watch",   # an open short's break score reached 2 of #81's checks (guardian_coins.py); alerts never exit
)
URGENT_ON_BOOK = ("binance_delisting",)   # urgent when it names a coin on our book (#23: the alert carries its symbol)
URGENT_CAP = 50
ROUTINE_SHOWN = 8


def is_urgent(kind: str, symbol: str | None = None) -> bool:
    return (kind in URGENT_ON_BOOK and bool(symbol)) or \
        any(kind.startswith(k[:-1]) if k.endswith("*") else kind == k for k in URGENT_KINDS)


def _rows(j, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    """SQL may call urgent(kind, symbol): the Python list above stays the only definition."""
    j.db.create_function("urgent", 2, lambda k, s: int(is_urgent(k, s)), deterministic=True)
    return [dict(r) for r in j.db.execute(sql, args).fetchall()]


def ack_id(j) -> int:
    """Newest acknowledged alert id. Before the first acknowledgement: the last alert the old brief showed."""
    raw = j.get(ACK_KEY)
    if raw is not None and raw.isdigit():
        return int(raw)
    legacy = j.get(LEGACY_KEY)
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
    counts = _rows(j, "SELECT kind, urgent(kind, symbol) AS u, COUNT(*) AS n FROM alerts WHERE id > ? "
                      "GROUP BY kind, u ORDER BY n DESC, kind", (after,))
    total = sum(r["n"] for r in counts)
    if not total:
        return [f"### alerts: none unacknowledged (through #{after})"]
    n_urgent = sum(r["n"] for r in counts if r["u"])
    out = [f"### alerts: {total} unacknowledged since #{after} ({n_urgent} urgent); start_session acknowledges them"]
    if n_urgent:
        urgent = _rows(j, "SELECT * FROM alerts WHERE id > ? AND urgent(kind, symbol) ORDER BY id DESC LIMIT ?",
                       (after, URGENT_CAP))
        out += [_line(a, "🚨 ") for a in urgent]
        if n_urgent > URGENT_CAP:
            out.append(f"- 🚨 {n_urgent - URGENT_CAP} more urgent: see the journal tool")
    routine = [r for r in counts if not r["u"]]
    if routine:
        n = sum(r["n"] for r in routine)
        out.append(f"- routine ({n}): " + ", ".join(f"{r['kind']} {r['n']}" for r in routine))
        groups = _rows(j, "SELECT MAX(id) AS id, COUNT(*) AS n FROM alerts WHERE id > ? AND NOT urgent(kind, symbol) "
                          "GROUP BY kind, symbol ORDER BY MAX(id) DESC LIMIT ?", (after, ROUTINE_SHOWN))
        rows = {r["id"]: r for r in _rows(j, f"SELECT * FROM alerts WHERE id IN ({','.join('?' * len(groups))})",
                                          tuple(g["id"] for g in groups))}
        out += [_line(rows[g["id"]], n=g["n"]) for g in groups]   # newest per (kind, symbol): a flood takes one line
    return out


def acknowledge(j) -> dict[str, Any]:
    """Mark every alert up to the newest as seen and return what was acknowledged (so nothing is acked unseen)."""
    after = ack_id(j)
    through = int(j.db.execute("SELECT MAX(id) FROM alerts").fetchone()[0] or after)
    rows = _rows(j, "SELECT kind, COUNT(*) AS n FROM alerts WHERE id > ? AND id <= ? GROUP BY kind ORDER BY n DESC",
                 (after, through))
    items = _rows(j, "SELECT * FROM alerts WHERE id > ? AND id <= ? AND urgent(kind, symbol) ORDER BY id DESC LIMIT ?",
                  (after, through, URGENT_CAP))
    j.set(ACK_KEY, str(through))
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
