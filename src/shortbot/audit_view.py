"""The read side of the audit trail (#24): `shortbot audit` lists who did what, and `shortbot hook-session` records
which model a Claude Code session runs (only the SessionStart hook is told).

    shortbot audit --since 24h --actor mcp      # rows newest first, counts by actor, Claude sessions with their model
    shortbot audit --action intent              # the writes (#36): kind, status, args

The hook needs one entry in .claude/settings.json, next to `shortbot brief` (the owner's file; see deploy/DEPLOY.md):
    {"type": "command", "command": "cd \\"$CLAUDE_PROJECT_DIR\\" && .venv/bin/shortbot hook-session", "timeout": 30}
Without it the model stays unknown."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import IO, Any

from . import audit

HOOK_KEYS = ("model", "source", "permission_mode", "agent_type", "effort")
_REL = re.compile(r"^(\d+(?:\.\d+)?)([mhd])$")


def _since(s: str) -> str:
    """'24h' / '7d' / '30m' or an ISO time (naive = UTC) -> the ISO text decisions.ts compares with."""
    if m := _REL.match(s.strip()):
        n, unit = float(m[1]), m[2]
        dt = datetime.now(timezone.utc) - timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: n})
    else:
        dt = datetime.fromisoformat(s.strip())
        dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _row(r: dict[str, Any]) -> dict[str, Any]:
    snap = r.pop("snapshot")
    out = {**r, "code_version": (r["code_version"] or "")[:12] or None, "reasoning": (r["reasoning"] or "")[:300]}
    if r["action"] == "call" and snap:   # a write's record before intents (#36 step 2): history, kept readable
        try:
            s = json.loads(snap)
            out.update(outcome=s.get("outcome"), args=s.get("args"))
        except ValueError:
            pass
    return out


def _intent(r: dict[str, Any]) -> dict[str, Any]:
    """A write's record (intents.py): listed as action `intent`, with its kind, status and arguments."""
    return {"intent_id": r["id"], "ts": r["received_at"], "actor": r["actor"], "claude_session": r["claude_session"],
            "session_id": r["session_id"], "mode": r["mode"], "code_version": (r["code_version"] or "")[:12] or None,
            "action": "intent", "kind": r["kind"], "status": r["status"], "symbol": r["symbol"],
            "trade_id": r["trade_id"], "args": json.loads(r["args"]), **({"error": r["error"][:300]} if r["error"]
                                                                          else {})}


def _filters(ts: str, since: str | None, actor: str | None, action: str | None,
             own: str | None) -> tuple[str, list[Any]]:
    """WHERE for one table: its time column `ts`; `action` matches its action column `own` (None: intents, whose
    action is `intent`)."""
    where, args = ["1=1"], []
    if since:
        where.append(f"{ts} >= ?")
        args.append(_since(since))
    if action and own:
        where.append(f"{own} = ?")
        args.append(action)
    elif action and action != "intent":
        where.append("0")
    if actor:
        where.append("actor LIKE ?")
        args.append(actor.rstrip("%") + "%")
    return " AND ".join(where), args


def report(j, since: str | None = None, actor: str | None = None, action: str | None = None,
           limit: int = 50) -> dict[str, Any]:
    """Decision rows and intents (every write, #36) newest first, counted by actor and Claude session."""
    wd, ad = _filters("ts", since, actor, action, "action")
    wi, ai = _filters("received_at", since, actor, action, None)
    rows = [_row(r) for r in j._rows(
        "SELECT id, ts, actor, claude_session, session_id, mode, code_version, action, symbol, trade_id, reasoning,"
        f" snapshot FROM decisions WHERE {wd} ORDER BY id DESC LIMIT ?", (*ad, limit))]
    rows += [_intent(r) for r in j._rows(f"SELECT * FROM intents WHERE {wi} ORDER BY rowid DESC LIMIT ?",
                                         (*ai, limit))]
    rows = sorted(rows, key=lambda r: r["ts"][:19], reverse=True)[:limit]   # to the second, as decisions are
    by_actor: dict[str, int] = {}
    sessions: dict[str, dict[str, Any]] = {}
    for table, ts, w, a in (("decisions", "ts", wd, ad), ("intents", "received_at", wi, ai)):
        for who, n in j.db.execute(f"SELECT actor, COUNT(*) FROM {table} WHERE {w} AND actor IS NOT NULL"
                                   " GROUP BY actor", a).fetchall():
            by_actor[who] = by_actor.get(who, 0) + n
        for cs, n, first, last in j.db.execute(
                f"SELECT claude_session, COUNT(*), MIN({ts}), MAX({ts}) FROM {table} WHERE {w}"
                " AND claude_session IS NOT NULL GROUP BY claude_session", a).fetchall():
            s = sessions.setdefault(cs, {"model": None, "rows": 0, "first": first, "last": last})
            s.update(rows=s["rows"] + n, first=min(s["first"], first), last=max(s["last"], last))
    for cs, s in sessions.items():
        hook = j.db.execute("SELECT snapshot FROM decisions WHERE action='claude_session' AND claude_session=?"
                            " ORDER BY id DESC LIMIT 1", (cs,)).fetchone()
        s["model"] = (json.loads(hook[0] or "{}") or {}).get("model") if hook else None
    base, base_args = _filters("ts", since, None, action, "action")
    unstamped = j.db.execute(f"SELECT COUNT(*) FROM decisions WHERE {base} AND actor IS NULL", base_args).fetchone()[0]
    return {"filters": {"since": since, "actor": actor, "action": action},
            "by_actor": dict(sorted(by_actor.items(), key=lambda kv: -kv[1])),
            "claude_sessions": dict(sorted(sessions.items(), key=lambda kv: kv[1]["last"], reverse=True)),
            "unstamped_rows": unstamped,
            "note": "unstamped = written by a process older than the audit trail (#24); model needs the hook-session "
                    "SessionStart hook; action `intent` = a write (#36), `call` = a write before intents",
            "rows": rows}


def hook_session(stream: IO[str]) -> int:
    """SessionStart hook: one `claude_session` row (model, source) under the hook's session id. Prints nothing and
    always exits 0: a bad payload or a busy journal must never get in the way of a session starting."""
    try:
        payload = json.loads(stream.read(1 << 16))
        if not isinstance(payload, dict) or not payload.get("session_id"):
            return 0
        from .journal import MODES, Journal
        from .provenance import desk_db
        mode = os.getenv("SHORTBOT_MODE", "demo").lower()
        audit.set_process("hook", claude_session=str(payload["session_id"]))
        j = Journal(desk_db(), mode if mode in MODES else "demo")
        snap = {k: payload[k] for k in HOOK_KEYS if payload.get(k) is not None}
        with audit.acting("hook:SessionStart"):
            j.log_decision("claude_session", f"{payload.get('source') or 'start'}: model "
                           f"{payload.get('model') or 'unknown'}", snapshot=snap)
        j.db.close()
    except Exception:  # noqa: BLE001
        pass
    return 0
