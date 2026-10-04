"""A session's side of the order gateway (#36 step 4, docs/GATEWAY.md §3): RemoteGateway, the same interface as
LocalGateway, behind SHORTBOT_GATEWAY.

- Who uses it (for_session): an MCP server, a CLI call or the web console (audit.process_kind), with the flag on,
  outside a running intent, for the write commands declared orders=True (commands.py) and the session watchdog's
  withdrawal. Everything else runs here, as before: journal-only writes, probes and the gateway's own process. The
  guardian has its own client (gateway_guardian.GuardianClient, step 6), which changes the policy steps below.
- One connection per intent: connect and hello within CONNECT_S, retried for CONNECT_RETRY_S (a drained restart),
  the gateway's uid checked (gateway_wire.py); then `accepted` within ACCEPT_S (journalling may wait for another
  process's write lock), then the result within FINAL_S, else a `pending` answer: the intent keeps running there, and
  `status(id)` (the socket, else the journal) tells how it ended. Nothing is ever re-sent blind: an agent retry is a
  new call, after a re-read.
- busy: the gateway's queue is full, nothing was received: the refusal is journalled here under the intent's id.
- When the gateway can't take it (no socket, no hello, another uid, protocol or mode, no `accepted`), the journal
  decides, under the same intent id (claimed once: intents.receive, then a compare-and-set start):
  - risk-adding kinds fail closed: the refusal is journalled here;
  - BREAKGLASS kinds (killswitch.ALLOWED: close, partial close, move_stop (tighten only), cancelling entries, flatten;
    plus halts, whose withdrawal is a cancel) run in this process under the symbol flock, actor
    `breakglass:<the caller's actor>`, with an urgent gateway_down_breakglass alert (D3); their exchange writes leave
    the process inside write_guard.writing("breakglass") (step 7);
  - if the gateway had already journalled it, both find its row and answer it (stored, or still running: never
    resend), so it never runs twice.
- A stale client (D5, step 5: an MCP server on code older than the disk's, `stale` = its stale_guard refusal) sends
  its intent all the same: the gateway runs it on the code on disk, after the protocol check (hello, and the intent's
  signature: gateway.signature_of). Nothing of it ever runs here: whenever the gateway doesn't take it (down, another
  protocol, a signature built for other code) the stale refusal is journalled instead, break-glass included."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from . import audit, commands, write_guard
from . import gateway_wire as wire
from .gateway import Gateway, LocalGateway
from .intents import Intent

log = logging.getLogger("shortbot.gateway")
CONNECT_S = 2.0           # connect + hello, per attempt
CONNECT_RETRY_S = 5.0     # attempts for this long: a drained restart of the gateway
ACCEPT_S = 30.0           # the gateway journals the intent before it says accepted
FINAL_S = 300.0           # then the result; past it: `pending` (it runs on)
SESSIONS = ("mcp", "cli", "web")
BREAKGLASS = frozenset({"close_position", "move_stop", "cancel_entry_group", "flatten_all", "halt_trading",
                        "kill_switch", "withdraw_entries"})


def for_session(journal: Callable[[], Any], settings: Callable[[], Any], ctx: Any = None,
                stale: dict[str, Any] | None = None) -> Gateway:
    """Where a session's order write goes: the gateway process when SHORTBOT_GATEWAY is on, else this one."""
    if audit.intent_id() is None and audit.process_kind() in SESSIONS and settings().gateway:
        return RemoteGateway(journal, settings, ctx, stale)
    return LocalGateway(journal, ctx)


def for_command(cmd: commands.Command, journal: Callable[[], Any], settings: Callable[[], Any],
                stale: dict[str, Any] | None = None) -> Gateway:
    """`stale`: this client's stale-code refusal (stale_guard.refuse), for whatever would run here (module doc)."""
    return for_session(journal, settings, stale=stale) if cmd.orders else LocalGateway(journal)


class Unavailable(Exception):
    """The gateway can't take an intent from this process (it may still have received one: the journal decides)."""


class RemoteGateway:
    """A session's client. The guardian's (gateway_guardian.GuardianClient) changes the steps marked `policy`."""

    def __init__(self, journal: Callable[[], Any], settings: Callable[[], Any], ctx: Any = None,
                 stale: dict[str, Any] | None = None):
        self._journal, self._settings, self.ctx, self.stale = journal, settings, ctx, stale

    # ------------------------------------------------------------ policy
    def process(self) -> str | None:
        return audit.process_kind()

    def retry_s(self) -> float:
        return CONNECT_RETRY_S

    def accept_s(self, intent: Intent) -> float:
        return ACCEPT_S

    def breaks_glass(self, intent: Intent) -> bool:
        return intent.kind in BREAKGLASS

    async def busy(self, intent: Intent, why: Any) -> Any:
        """The gateway's queues are full: nothing received. The refusal is journalled here under the intent's id."""
        return await LocalGateway(self._journal).submit(intent, {
            "ok": False, "busy": True, "error": f"the order gateway is busy ({why}): {intent.kind} was not received, "
            "nothing sent. Re-read the state, then retry"})

    async def lost(self, intent: Intent) -> Any:
        """Accepted, but its end isn't known here (the wait ended, or the gateway went away): what status() says."""
        return await self.status(intent.id)

    async def status(self, intent_id: str) -> Any | None:
        """The answer a resubmission would get (None: never journalled): from the gateway, else from the journal."""
        try:
            r, w = await self._connect()
        except Unavailable:
            return await LocalGateway(self._journal).status(intent_id)
        try:
            w.write(wire.encode({"op": "status", "id": intent_id}))
            return (await wire.read(r, CONNECT_S)).get("answer")
        except (ConnectionError, TimeoutError, ValueError):
            return await LocalGateway(self._journal).status(intent_id)
        finally:
            w.close()

    async def submit(self, intent: Intent, refusal: Any = None) -> Any:
        if refusal is not None:   # refused here (stale code): nothing is sent, the row says so
            return await LocalGateway(self._journal).submit(intent, refusal)
        try:
            r, w = await self._connect()
        except Unavailable as e:
            return await self._without(intent, str(e))
        try:
            w.write(wire.encode({"op": "submit", "intent": wire.intent_dict(intent),
                                 "origin": {"claude_session": audit.claude_session(), "process": self.process()}}))
            try:
                first = await wire.read(r, self.accept_s(intent))
            except (ConnectionError, TimeoutError, ValueError) as e:
                return await self._without(intent, f"no answer to the intent ({type(e).__name__}: {e})")
            if first.get("status") == "busy":
                return await self.busy(intent, first.get("error"))
            if first.get("status") == "refused":
                return await self._without(intent, f"the gateway refused it: {first.get('error')}")
            if first.get("status") != "accepted":
                return self._final(first)
            try:
                return self._final(await wire.read(r, FINAL_S))
            except TimeoutError:
                log.warning("intent %s (%s) still running after %.0f s: pending", intent.id, intent.kind, FINAL_S)
            except (ConnectionError, ValueError) as e:
                log.warning("intent %s (%s): lost the gateway after it accepted (%s)", intent.id, intent.kind, e)
            return await self.lost(intent)
        finally:
            w.close()

    @staticmethod
    def _final(msg: dict[str, Any]) -> Any:
        if msg.get("status") == "error":   # raised as it would have been in-process (typed: gateway_wire)
            err = msg.get("error")
            raise wire.error_from(err if isinstance(err, dict) else {})
        return msg.get("result")

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        settings = self._settings()
        path, end, why = wire.socket_path(settings), time.monotonic() + self.retry_s(), "no attempt"
        while True:
            try:
                return await asyncio.wait_for(self._hello(path, settings.mode), CONNECT_S)
            except Unavailable:
                raise   # another uid, protocol or mode: retrying won't change it
            except (OSError, TimeoutError, ValueError) as e:   # no socket, nobody listening, no hello yet
                why = f"{path}: {type(e).__name__}: {e}"
            if time.monotonic() >= end:
                raise Unavailable(why)
            await asyncio.sleep(0.2)

    @staticmethod
    async def _hello(path: Any, mode: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        r, w = await asyncio.open_unix_connection(str(path), limit=wire.LIMIT)
        try:
            if why := wire.foreign(w):
                raise Unavailable(f"the listener on {path} is not this user's: {why}")
            w.write(wire.encode({"v": wire.PROTOCOL, "op": "hello", "mode": mode}))
            hello = await wire.read(r, None)
            if not hello.get("ok"):
                raise Unavailable(f"the gateway refused this client: {hello.get('error')}")
            if hello.get("v") != wire.PROTOCOL or hello.get("mode") != mode:
                raise Unavailable(f"the gateway speaks protocol v{hello.get('v')} in mode {hello.get('mode')}; this "
                                  f"client v{wire.PROTOCOL} in mode {mode}")
            return r, w
        except BaseException:
            w.close()
            raise

    async def _without(self, intent: Intent, why: str) -> Any:
        """The gateway can't take `intent`: the journal decides under its id (module doc)."""
        local = LocalGateway(self._journal, self.ctx)
        if self.stale is not None:   # nothing runs on stale code here (D5)
            log.warning("gateway didn't take %s %s (%s), and this client's code is stale: refused", intent.kind,
                        intent.id, why)
            return await local.submit(intent, {**self.stale, "gateway": why})
        if not self.breaks_glass(intent):
            log.warning("gateway unavailable (%s): %s %s refused", why, intent.kind, intent.id)
            return await local.submit(intent, {
                "ok": False, "gateway": "unavailable", "error": f"the order gateway is unavailable ({why}): "
                f"{intent.kind} refused, nothing sent. New risk never bypasses it; closes, stop tightening, cancels, "
                "halts and flatten still run here (break-glass). See the brief's gateway line"})
        actor = f"breakglass:{intent.actor}"
        ran: list[bool] = []
        log.warning("gateway unavailable (%s): %s %s runs here as break-glass", why, intent.kind, intent.id)
        try:
            with audit.acting(actor), write_guard.writing("breakglass"):
                return await local.submit(dataclasses.replace(intent, actor=actor), on_start=lambda: ran.append(True))
        finally:
            if ran:
                await self._alert(intent, why)

    async def _alert(self, intent: Intent, why: str) -> None:
        from .killswitch import notify
        try:
            await notify(self._journal(), self._settings(), "gateway_down_breakglass",
                         f"The order gateway is unavailable ({why}). {intent.kind} (intent {intent.id}, "
                         f"{intent.actor}, pid {os.getpid()}) ran in the session's own process as break-glass, under "
                         "the symbol lock. Bring the gateway back (deploy/DEPLOY.md).", urgent=True)
        except Exception:  # noqa: BLE001 - the alert never undoes the protection
            log.exception("gateway_down_breakglass alert failed")

