"""The order gateway's interface (#36, docs/GATEWAY.md §4): every write is an Intent submitted here, journalled on
receipt, run once per id, and answered with today's result. Step 2 runs it in the caller's process (LocalGateway):
MCP tools and `shortbot` subcommands that write (commands.py) and the guardian's protective actions all go through
`submit`. Step 4 puts the same interface behind a socket.

- Handlers are found by kind: a write command's body (commands.by_kind), or a function registered with @handler
  (the guardian's restore_stop, force_close, cap_stop, plan_step), called as handler(ctx, **args). `ctx` is what the
  gateway runs for: nothing for a command (its body finds the app itself), the guardian for its actions.
- Policy stays where it is (§4.1): vetoes, budget and sizing run inside the bodies, as before. The intent records the
  call and runs it.
- One intent at a time per task: a submit inside a running intent is a bug, like nested symbol locks.
- A resubmitted id never runs again: it answers the stored result, or "in progress" (unknown: never resend it).
  Only a busy intent (nothing sent: a symbol or the account lock was held) runs again under its id (§3.5).
- The journal never makes the caller's event loop wait (#79): receipt and start are one unit the caller awaits before
  anything runs (journal-first), the result a posted unit. A handler registered late_ok (the guardian's restore_stop,
  force_close, cap_stop: protection before journal-first) waits at most CLAIM_WAIT_S for that unit while another
  process holds the write lock, then runs anyway; its rows are posted, marked journal_late (intents.py)."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Protocol

import ccxt.async_support as ccxt

from . import audit, commands, errors, intents
from .commands import CommandError
from .intents import Intent
from .redact import redact
from .symlock import AccountBusy, SymbolBusy

log = logging.getLogger("shortbot.gateway")
HANDLERS: dict[str, Callable[..., Awaitable[Any]]] = {}
LATE_OK: set[str] = set()   # kinds that run without their row while the journal is locked (protection, idempotent)
CLAIM_WAIT_S = 2.0          # how long a late_ok intent waits for its row first


def handler(kind: str, late_ok: bool = False) -> Callable[[Callable], Callable]:
    """Register the function that runs intents of `kind`: fn(ctx, **args). late_ok: see the module doc; only for a
    protective guardian action whose sends are idempotent."""
    def register(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        if kind in HANDLERS:
            raise ValueError(f"two handlers for intent kind {kind}")
        HANDLERS[kind] = fn
        if late_ok:
            LATE_OK.add(kind)
        return fn
    return register


def handler_for(kind: str) -> Callable[..., Awaitable[Any]]:
    if kind in HANDLERS:
        return HANDLERS[kind]
    cmd = commands.by_kind(kind)
    if cmd is None:
        raise ValueError(f"no handler for intent kind {kind!r}")

    async def run(ctx: Any, **args: Any) -> Any:
        return await cmd.body(**args)
    run.__signature__ = inspect.Signature([  # type: ignore[attr-defined]  # inspect reads it; typeshed has no such attr
        inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD), *cmd.signature.parameters.values()])
    return run


class Gateway(Protocol):
    async def submit(self, intent: Intent, refusal: Any = None) -> Any: ...

    def status(self, intent_id: str) -> dict[str, Any] | None: ...


def status_of(res: Any) -> str:
    """A returned result's status: today's outcome under §4.5's names (step 3 splits refused by what was sent)."""
    if isinstance(res, dict):
        if res.get("unknown"):
            return "unknown"
        if res.get("ok") is False:
            return "busy" if res.get("busy") else "refused"
    return "done"


def failure_of(e: BaseException) -> str:
    """A raised error's status: nothing sent (refused, busy), maybe sent (unknown), failed, or interrupted (cancelled:
    the run stopped wherever it was)."""
    if not isinstance(e, Exception):
        return "interrupted"
    if isinstance(e, (SymbolBusy, AccountBusy)):
        return "busy"
    if isinstance(e, CommandError):
        return "refused"
    if isinstance(e, (errors.OrderError, ccxt.BaseError)) and errors.classify(e).unknown:
        return "unknown"
    return "failed"


def answer(row: dict[str, Any]) -> Any:
    """What a resubmitted id gets: its stored result, else why there is none (still open, or it ended without one)."""
    st, iid = row["status"], row["id"]
    if st in ("done", "refused", "unknown", "busy") and row["result"] is not None:
        return row["result"]
    if st in intents.OPEN:
        return {"ok": False, "unknown": True, "in_progress": True, "intent_id": iid, "status": st,
                "error": f"intent {iid} ({row['kind']}) is still {st}: it may be sending orders. Ask again later and "
                         "never resend it under a new id; re-read positions and pending_entries"}
    return {"ok": False, "intent_id": iid, "status": st, "error": row["error"] or f"intent {iid} ended {st}",
            **({"unknown": True} if st in ("unknown", "interrupted") else {})}


class LocalGateway:
    """Runs each intent in this process, on the code this process imported (GATEWAY.md §8 step 2)."""

    def __init__(self, journal: Callable[[], Any], ctx: Any = None):
        self._journal, self.ctx = journal, ctx

    def status(self, intent_id: str) -> dict[str, Any] | None:
        return intents.get(self._journal(), intent_id)

    async def submit(self, intent: Intent, refusal: Any = None) -> Any:
        """Journal `intent`, run it, record its result; `refusal`: the caller refused it (stale code), journal only."""
        run = handler_for(intent.kind)
        inspect.signature(run).bind(self.ctx, **intent.args)   # a malformed intent is never journalled
        if (outer := audit.intent_id()) is not None:
            raise RuntimeError(f"nested intents: {intent.kind} submitted inside intent {outer}; an intent's code "
                               "runs its steps itself")
        if intent.side != "short" and refusal is None:
            refusal = {"ok": False, "error": f"side {intent.side}: this desk is short-only until LONG_BUILD phase 2"}
        j, at = self._journal(), intents.now_ms()
        claim = j.write(_claim, intent, refusal)
        if intent.kind in LATE_OK and refusal is None:
            try:
                verdict = await asyncio.wait_for(claim, CLAIM_WAIT_S)
            except TimeoutError:   # the write lock is held elsewhere: protect first, journal when it frees
                log.warning("intent %s (%s) runs before its row: the journal's write lock is held (journal_late)",
                            intent.id, intent.kind)
                j.post(intents.receive, intent, None, at, True)
                verdict = None
        else:
            verdict = await claim
        if verdict is not None:
            return verdict[0]
        with audit.within(intent.id):
            try:
                res = await run(self.ctx, **intent.args)
            except BaseException as e:
                j.post(intents.finish, intent.id, failure_of(e), None, redact(f"{type(e).__name__}: {e}"),
                       intents.now_ms())
                raise
        j.post(intents.finish, intent.id, status_of(res), res, None, intents.now_ms())
        return res


def _claim(j: Any, intent: Intent, refusal: Any) -> tuple[Any] | None:
    """One unit before anything runs: journal `intent` on receipt and start it. None: run it now; else (the answer)
    without running: its refusal, or a resubmitted id's stored answer."""
    seen = intents.receive(j, intent, refusal)
    if seen is not None:
        if intents.dumps(intent.args) != intents.dumps(seen["args"]) or intent.kind != seen["kind"]:
            raise ValueError(f"intent id {intent.id} reused for another request ({seen['kind']})")
        if seen["status"] != "busy":
            return (answer(seen),)
    elif refusal is not None:
        return (refusal,)
    if not intents.start(j, intent.id):   # another caller started it first
        row = intents.get(j, intent.id)
        assert row is not None   # journalled above, in this unit
        return (answer(row),)
    return None
