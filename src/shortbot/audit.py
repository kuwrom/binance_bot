"""Who did what (#24). Journal.log_decision / log_alert stamp every row with the process's stamp, so no call site
passes it:
- actor: the process kind (`set_process` at each console script: mcp, cli, guardian, admin), narrowed per call by
  `acting`: mcp:<tool>, cli:<cmd>, guardian:<job>, admin:<cmd>. A process that never set one is py:<argv0>.
- claude_session: CLAUDE_CODE_SESSION_ID, which Claude Code sets in Bash commands, hooks and (since 2.1.154) stdio MCP
  servers. An MCP server keeps the id it was spawned with (stale after /clear). The guardian drops it: started from a
  Claude Bash it inherits that session's id, but acts on its own.
- code_version: procs.code_version() (#58), the hash of the package's code taken at IMPORT, so a long-lived process
  stamps the code it runs, not what was merged on disk since; the same value its kv proc:<pid> row carries.
- the model is known only to the SessionStart hook (stdin `model`): `shortbot hook-session` records it (audit_view).

- intent_id: the intent (#36, intents.py) running in this task, set by the gateway (`within`), so every row an
  intent's code writes links back to it. The intent row itself is the record of a write's call (it replaced the
  `call` decision row): request, actor and result.
- on behalf of a caller: the shortbot-gateway process (#36 step 4) runs each session's intent with that session's
  actor and Claude session (`on_behalf`); the code version stays its own, the code that ran.

`refusals(op)` wraps each order path: a refusal (ok False, not a no-op) or an exception that journalled no decision
of its own gets a `rejected` / `failed` row, an identical one (same actor, op, symbol, reason with numbers masked) at
most once per DEDUPE_S with the count of those suppressed."""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .procs import code_version
from .redact import redact

log = logging.getLogger(__name__)

CODE_VERSION = code_version()      # fixed at import: see the module doc
DEDUPE_S = 600.0
MAX_TEXT = 2000
MAX_RESULT = 4000
# keys whose value is blanked, compared without case, '_' or '-' (exact: a sector like "fan_token" stays)
SECRET_KEYS = frozenset({"secret", "apisecret", "apikey", "token", "bottoken", "accesstoken", "telegrambottoken",
                         "password", "passphrase", "signature"})
_REASON_KEYS = ("error", "vetoed", "rejected_by_budget", "rejected_by_risk", "plan_errors", "wait", "rejected")

_proc: dict[str, Any] = {"kind": None, "claude_session": os.getenv("CLAUDE_CODE_SESSION_ID") or None}
_actor: ContextVar[str | None] = ContextVar("audit_actor", default=None)
_scope: ContextVar[list[int] | None] = ContextVar("audit_scope", default=None)
_intent: ContextVar[str | None] = ContextVar("audit_intent", default=None)
_session: ContextVar[tuple[str | None] | None] = ContextVar("audit_session", default=None)   # (claude_session,)
_seen: dict[tuple, list[float]] = {}   # dedupe: key -> [first logged at, suppressed since]
_now = time.monotonic


def set_process(kind: str, claude: bool = True, claude_session: str | None = None) -> None:
    """Once, at a console script's entry. claude=False: ignore an inherited CLAUDE_CODE_SESSION_ID."""
    _proc["kind"] = kind
    _proc["claude_session"] = claude_session or (os.getenv("CLAUDE_CODE_SESSION_ID") or None if claude else None)


@contextmanager
def acting(actor: str):
    tok = _actor.set(actor)
    try:
        yield
    finally:
        _actor.reset(tok)


def actor() -> str:
    return _actor.get() or _proc["kind"] or f"py:{Path(sys.argv[0] or 'python').name}"


def process_kind() -> str | None:
    """What this process is (set_process): mcp, cli, web, guardian, gateway, admin; None for a script or a test."""
    return _proc["kind"]


def claude_session() -> str | None:
    s = _session.get()
    return s[0] if s is not None else _proc["claude_session"]


@contextmanager
def on_behalf(actor: str, claude_session: str | None):
    """The rows written inside are another process's call (the gateway running a session's intent)."""
    tok_a, tok_s = _actor.set(actor), _session.set((claude_session,))
    try:
        yield
    finally:
        _session.reset(tok_s)
        _actor.reset(tok_a)


@contextmanager
def within(intent_id: str):
    """The rows written inside belong to this intent (gateway.py)."""
    tok = _intent.set(intent_id)
    try:
        yield
    finally:
        _intent.reset(tok)


def intent_id() -> str | None:
    return _intent.get()


def stamp() -> dict[str, Any]:
    return {"actor": actor(), "claude_session": claude_session(), "code_version": CODE_VERSION,
            "intent_id": _intent.get()}


def count_decision() -> None:
    """Journal.log_decision calls this: the order path in progress journalled a decision of its own."""
    if (box := _scope.get()) is not None:
        box[0] += 1


# ------------------------------------------------------------ redaction
def clean(obj: Any, trim: bool = True) -> Any:
    """A copy safe to journal: secret-named keys blanked, signed URLs / bot tokens redacted, long text cut."""
    if isinstance(obj, dict):
        return {k: "<redacted>" if _secret(k) else clean(v, trim) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v, trim) for v in obj]
    if isinstance(obj, str):
        s = redact(obj)
        return s[:MAX_TEXT] + "..." if trim and len(s) > MAX_TEXT else s
    return obj


def _secret(k: Any) -> bool:
    return isinstance(k, str) and k.lower().replace("_", "").replace("-", "") in SECRET_KEYS


def _compact(res: Any) -> Any:
    try:
        text = json.dumps(clean(res), default=str)
    except (TypeError, ValueError):
        return {"unserializable": type(res).__name__}
    if len(text) <= MAX_RESULT:
        return json.loads(text)
    return {"truncated": True, "keys": list(res)[:40]} if isinstance(res, dict) else {"truncated": True}


def _why(res: dict) -> str:
    for k in _REASON_KEYS:
        if v := res.get(k):
            return v if isinstance(v, str) else f"{k} {json.dumps(clean(v), default=str)}"
    return "refused"


def _err(e: BaseException) -> str:
    return redact(f"{type(e).__name__}: {e}")


def _trade_id(res: Any) -> int | None:
    t = res.get("trade_id") if isinstance(res, dict) else None
    return t if isinstance(t, int) and not isinstance(t, bool) else None


def _symbol(s: Any) -> str | None:
    if not isinstance(s, str) or not s:
        return None
    from .exchange import to_symbol  # here: journal imports this module, and exchange pulls in ccxt
    return to_symbol(s)


def bound_args(sig: inspect.Signature, args: tuple, kwargs: dict) -> dict[str, Any]:
    try:
        out = dict(sig.bind_partial(*args, **kwargs).arguments)
    except TypeError:
        out = {"args": list(args), **kwargs}
    out.pop("self", None)
    return out


# ------------------------------------------------------------ order paths
def _dedupe(key: tuple) -> int | None:
    """None: suppress this row; else write it, with this many identical ones suppressed since the last."""
    now = _now()
    for k in [k for k, (t, n) in _seen.items() if now - t > DEDUPE_S and not n]:
        del _seen[k]
    hit = _seen.get(key)
    if hit and now - hit[0] < DEDUPE_S:
        hit[1] += 1
        return None
    _seen[key] = [now, 0]
    return int(hit[1]) if hit else 0


def _refusal(owner: Any, op: str, args: dict, res: dict | None = None, error: BaseException | None = None) -> None:
    try:
        j = getattr(owner, "j", None) or getattr(getattr(owner, "tr", None), "j", None)
        if j is None:
            return
        why = _err(error) if error else _why(res or {})
        sym = _symbol(args.get("symbol"))
        n = _dedupe((actor(), op, sym, re.sub(r"\d+(\.\d+)?", "#", why)))
        if n is None:
            return
        snap = {"op": op, "args": clean(args), **({"error": why} if error else {"result": _compact(res)}),
                **({"suppressed": n} if n else {})}
        tok = _scope.set(None)   # this row is not the path's own decision
        try:
            j.log_decision("failed" if error else "rejected", f"{op}: {why}", sym, snapshot=snap,
                           trade_id=_trade_id(res))
        finally:
            _scope.reset(tok)
    except Exception:  # noqa: BLE001 - the audit row must never change the order path's outcome
        log.exception("could not journal the %s refusal", op)


def refusals(op: str) -> Callable:
    """Decorate an order-path method (its owner has .j, or .tr.j): see the module doc."""
    def wrap(f: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        sig = inspect.signature(f)

        @functools.wraps(f)
        async def inner(self, *args: Any, **kwargs: Any) -> Any:
            box = [0]
            tok = _scope.set(box)
            try:
                res = await f(self, *args, **kwargs)
            except Exception as e:
                _scope.reset(tok)
                tok = None
                if not box[0]:
                    _refusal(self, op, bound_args(sig, (self, *args), kwargs), error=e)
                raise
            finally:
                if tok is not None:
                    _scope.reset(tok)
            if isinstance(res, dict) and res.get("ok") is False and not res.get("noop") and not box[0]:
                _refusal(self, op, bound_args(sig, (self, *args), kwargs), res)
            return res
        return inner
    return wrap
