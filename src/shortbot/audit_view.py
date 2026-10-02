"""The read side of the audit trail (#24): `shortbot audit` lists who did what, and `shortbot hook-session` records
which model a Claude Code session runs (only the SessionStart hook is told).

    shortbot audit --since 24h --actor mcp      # rows newest first, counts by actor, Claude sessions with their model

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
    if r["action"] == "call" and snap:
        try:
            s = json.loads(snap)
            out.update(outcome=s.get("outcome"), args=s.get("args"))
        except ValueError:
            pass
    return out


def report(j, since: str | None = None, actor: str | None = None, action: str | None = None,
           limit: int = 50) -> dict[str, Any]:
    where, args = ["1=1"], []
    if since:
        where.append("ts >= ?")
        args.append(_since(since))
    if action:
        where.append("action = ?")
        args.append(action)
    base, base_args = " AND ".join(where), list(args)
    if actor:
        where.append("actor LIKE ?")
        args.append(actor.rstrip("%") + "%")
    w = " AND ".join(where)
    rows = j._rows("SELECT id, ts, actor, claude_session, session_id, mode, code_version, action, symbol, trade_id,"
                   f" reasoning, snapshot FROM decisions WHERE {w} ORDER BY id DESC LIMIT ?", (*args, limit))
    by_actor = dict(j.db.execute(f"SELECT actor, COUNT(*) FROM decisions WHERE {w} AND actor IS NOT NULL"
                                 " GROUP BY actor ORDER BY 2 DESC", args).fetchall())
    unstamped = j.db.execute(f"SELECT COUNT(*) FROM decisions WHERE {base} AND actor IS NULL",
                             base_args).fetchone()[0]
    sessions: dict[str, dict[str, Any]] = {}
    for cs, n, first, last in j.db.execute(
            f"SELECT claude_session, COUNT(*), MIN(ts), MAX(ts) FROM decisions WHERE {w} AND claude_session IS NOT NULL"
            " GROUP BY claude_session ORDER BY MAX(id) DESC", args).fetchall():
        hook = j.db.execute("SELECT snapshot FROM decisions WHERE action='claude_session' AND claude_session=?"
                            " ORDER BY id DESC LIMIT 1", (cs,)).fetchone()
        model = (json.loads(hook[0] or "{}") or {}).get("model") if hook else None
        sessions[cs] = {"model": model, "rows": n, "first": first, "last": last}
    return {"filters": {"since": since, "actor": actor, "action": action}, "by_actor": by_actor,
            "claude_sessions": sessions, "unstamped_rows": unstamped,
            "note": "unstamped = written by a process older than the audit trail (#24); model needs the hook-session "
                    "SessionStart hook", "rows": [_row(r) for r in rows]}


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
