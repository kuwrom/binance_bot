"""The order gateway's interface (#36, docs/GATEWAY.md §4): every write is an Intent submitted here, journalled on
receipt, run once per id, and answered with today's result; its row keeps the typed result (results.py, step 3: the
status exact by what it sent, the error typed). Step 2 runs it in the caller's process (LocalGateway):
MCP tools and `shortbot` subcommands that write (commands.py) and the guardian's protective actions all go through
`submit`. Step 4 puts the same interface behind a socket.

- Handlers are found by kind: a write command's body (commands.by_kind), or a function registered with @handler,
  called as handler(ctx, **args). `ctx` is what the gateway runs for: nothing for a command (its body finds the app
  itself); the guardian for its position-pass actions (restore_stop, force_close, cap_stop, plan_step, pull_targets);
  the EntryDesk for entry-row actions (entries_acts.py, orphans.sweep_symbol: submitted through the desk's own
  gateway, EntryDesk.gw), whoever decided them: the guardian's fill poll, sweeps and reconciler, or a session's
  watchdog (killswitch_ops.withdraw_resting).
- Policy stays where it is (§4.1): vetoes, budget and sizing run inside the bodies, as before. The intent records the
  call and runs it.
- One intent at a time per task: a submit inside a running intent is a bug, like nested symbol locks.
- A resubmitted id never runs again: it answers the stored result, or "in progress" (unknown: never resend it).
  Only a busy intent (nothing sent: a symbol or the account lock was held) runs again under its id (§3.5).
- The journal never makes the caller's event loop wait (#79): receipt and start are one unit the caller awaits before
  anything runs (journal-first), the result a posted unit. A handler registered late_ok (LATE_OK: protection before
  journal-first) waits at most CLAIM_WAIT_S for that unit while another process holds the write lock, then runs
  anyway; its rows are posted, marked journal_late (intents.py)."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Protocol

from . import audit, commands, intents
from .intents import Intent
from .results import Result

log = logging.getLogger("shortbot.gateway")
HANDLERS: dict[str, Callable[..., Awaitable[Any]]] = {}
# Kinds that run without their row while the journal is locked: protective, and every send idempotent. Each registers
# itself (@handler(kind, late_ok=True)); test_guardian_intents pins the set:
# - restore_stop: a closePosition stop, one per side, found before one is placed; bridges swept by client id.
# - force_close: a reduce-only market close, re-checked under the lock (a stop now resting or a flat position: none).
# - cap_stop: a tighter closePosition stop, decided again under the lock; one per side.
# - protect_fill: a fill's whole-position stop (listed before it is placed), its pre-arm's cancel (Gone is done), its
#   remainder's cancel (withdraw believes what filled), the plan stop (tighten only: a no-op at the same tick), the OCO
#   siblings' cancels (rows by compare-and-set). A held lock must never delay a fill's stop.
# - cancel_entry: one resting row withdrawn (cascade, cancel_below, the OCO sibling retry, a closed split's leg,
#   expiry): re-read under the lock, compare-and-set; withdraw believes fills.
# - withdraw_entries: the HALTED, governor, pause and resize sweeps and the session watchdog: as cancel_entry, per row.
LATE_OK: set[str] = set()
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


def answer(j: Any, row: dict[str, Any]) -> Any:
    """What a resubmitted id gets: its stored result, else why there is none (still open, or it ended without one). A
    resolved one (resolver.py) says how, and is no longer unknown."""
    st, iid = row["status"], row["id"]
    if st in intents.OPEN:
        return {"ok": False, "unknown": True, "in_progress": True, "intent_id": iid, "status": st,
                "error": f"intent {iid} ({row['kind']}) is still {st}: it may be sending orders. Ask again later and "
                         "never resend it under a new id; re-read positions and pending_entries"}
    r = Result.from_row(row)
    out = r.body if r.body is not None else {
        "ok": False, "intent_id": iid, "status": st, "error": str(r.error) if r.error else f"intent {iid} ended {st}",
        **({"unknown": True} if st in intents.UNRESOLVED else {})}
    if not row["resolved_at"]:
        return out
    return {**{k: v for k, v in (out if isinstance(out, dict) else {"result": out}).items() if k != "unknown"},
            "status": st, "resolved": {"at": row["resolved_at"],
                                       "orders": {o["client_id"]: o["outcome"] for o in intents.orders(j, iid)}}}


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
        with audit.within(intent.id), intents.recording() as led:
            try:
                res = await run(self.ctx, **intent.args)
            except BaseException as e:
                j.post(intents.finish, intent.id, Result.failure(e, led, intent.side), intents.now_ms())
                raise
        j.post(intents.finish, intent.id, Result.of(res, led, intent.side), intents.now_ms())
        return res


def _claim(j: Any, intent: Intent, refusal: Any) -> tuple[Any] | None:
    """One unit before anything runs: journal `intent` on receipt and start it. None: run it now; else (the answer)
    without running: its refusal, or a resubmitted id's stored answer."""
    seen = intents.receive(j, intent, refusal)
    if seen is not None:
        if intents.dumps(intent.args) != intents.dumps(seen["args"]) or intent.kind != seen["kind"]:
            raise ValueError(f"intent id {intent.id} reused for another request ({seen['kind']})")
        if seen["status"] != "busy":
            return (answer(j, seen),)
    elif refusal is not None:
        return (refusal,)
    if not intents.start(j, intent.id):   # another caller started it first
        row = intents.get(j, intent.id)
        assert row is not None   # journalled above, in this unit
        return (answer(j, row),)
    return None
