"""shortbot-gateway (#36 steps 4-6, docs/GATEWAY.md §2-3): the process that runs the order writes.

- One per mode on the desk host (D1, D2): it holds `run/gateway-<mode>.lock` (flock) while it serves, and listens on
  `run/gateway-<mode>.sock` (gateway_wire.py: a 0700 directory, the peer's uid checked, one JSON object per line).
- It serves the write commands declared orders=True (commands.py) and the session watchdog's `withdraw_entries`
  (killswitch_ops.withdraw_resting; its ctx is this process's own EntryDesk). Each runs through today's LocalGateway in
  its own task, under the caller's actor and Claude session (audit.on_behalf) and the command's #70 weight class:
  journalled on receipt (the caller is told `accepted`), run once per id, its result sent back. A caller that goes
  away never cancels it; a resubmitted id gets the stored answer or "in progress".
- Step 6: it serves the guardian too (origin process "guardian"): every @handler kind (gateway.HANDLERS), each with
  its ctx (gateway.CTX: this process's EntryDesk, or GuardianActs, gateway_acts.py), run for the guardian
  (origin.guardian: the .env baseline, the guardian's alerts, a 5 s symbol wait). Those kinds come from the guardian
  only; the commands and the watchdog's withdrawal from the sessions.
- An intent's exception reaches its caller typed (gateway_wire.error_dict / error_from).
- Policy runs here too, exactly as in an MCP server (get_app(research=True): the research parameters in the journal).
  Risk-adding bodies hold the #74 account lock from their cap checks through the send: its asyncio lock queues them
  account-wide inside this process and its flock against any other writer (a flag-off session, break-glass). That is
  the global admission; the flock stays for good (GATEWAY §8).
- Backpressure: at most BOUND_SYMBOL intents in flight per symbol and BOUND_TOTAL in all; beyond that the caller gets
  `busy` and nothing is received. The symbol locks inside the bodies are the queue (SymbolBusy after 60 s, as today).
- Heartbeat: kv gateway_heartbeat every HEARTBEAT_S (gateway_health.py: the brief and the guardian read it).
- SIGTERM / SIGINT: stop accepting, let running intents finish (DRAIN_S), exit 0. docker compose waits
  stop_grace_period (default 10 s) before SIGKILL, so its service sets 45 s (deploy/DEPLOY.md)."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import fcntl
import logging
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import audit, commands, gateway, gateway_health, origin, procs, weight
from . import gateway_wire as wire
from .gateway import LocalGateway
from .intents import Intent

log = logging.getLogger("shortbot.gateway")
BOUND_SYMBOL = 8         # intents in flight per symbol (GATEWAY §3.3)
BOUND_TOTAL = 64
HEARTBEAT_S = 5.0
DRAIN_S = 30.0
HELLO_S = 2.0            # a client says hello this soon after connecting, or is dropped
SESSION_KINDS = ("withdraw_entries",)   # not a command: the session watchdog's withdrawal (ctx: the EntryDesk)


def served(kind: str, process: Any = None) -> bool:
    """Taken from a client of this process kind: the guardian's handler kinds, the sessions' order commands."""
    if process == "guardian":
        return kind in gateway.HANDLERS
    cmd = commands.by_kind(kind)
    return (cmd is not None and cmd.orders) or kind in SESSION_KINDS


class Refused(ValueError):
    """Not taken: nothing journalled. `stale`: the intent was built for other code than this gateway runs (D5)."""

    def __init__(self, why: str, stale: bool = False):
        super().__init__(why)
        self.stale = stale


class GatewayServer:
    def __init__(self, journal: Any, mode: str, path: Path, ctx: Any, acts: Any = None):
        """ctx: this process's EntryDesk; acts: GuardianActs (gateway_acts.py), None: no guardian kinds served."""
        self.j, self.mode, self.path, self.acts = journal, mode, Path(path), acts
        self.gw = LocalGateway(lambda: journal, ctx=ctx)
        self.gw_guardian = LocalGateway(lambda: journal, ctx=acts)
        self.inflight: dict[asyncio.Task[Any], str | None] = {}   # each running intent's task -> its symbol
        self._server: asyncio.Server | None = None
        self._beats: asyncio.Task[None] | None = None
        self._lock: int | None = None

    # ------------------------------------------------------------ life
    async def start(self) -> None:
        commands.load()
        gateway.load_handlers()
        wire.secure_dir(self.path.parent)
        lock = self.path.with_suffix(".lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RuntimeError(f"another gateway serves {self.mode} ({lock} is locked)") from None
        self._lock = fd
        if self.acts is not None:   # §5: reconciled before it listens (clients break glass or wait meanwhile)
            from .gateway_acts import start_up
            await start_up(self.acts, self.gw, self.gw_guardian)
        self.path.unlink(missing_ok=True)   # a dead gateway's socket: this one holds the lock now
        self._server = await asyncio.start_unix_server(self.connection, path=str(self.path), limit=wire.LIMIT)
        self._beats = asyncio.create_task(self.heartbeats())
        log.info("gateway (%s, protocol v%d) listening on %s", self.mode, wire.PROTOCOL, self.path)

    async def close(self, drain_s: float = DRAIN_S) -> None:
        """Stop accepting; give running intents `drain_s` to finish; remove the socket; release the lock."""
        if self._server is not None:
            self._server.close()
        if self.inflight:
            log.info("draining %d running intent(s), up to %.0f s", len(self.inflight), drain_s)
            _, left = await asyncio.wait(set(self.inflight), timeout=drain_s)
            if left:
                log.warning("%d intent(s) still running at exit: the guardian's sweep ends them", len(left))
        if self._beats is not None:
            self._beats.cancel()
        self.path.unlink(missing_ok=True)
        if self._lock is not None:
            os.close(self._lock)   # releases the flock
            self._lock = None

    async def heartbeats(self) -> None:
        while True:
            try:
                gateway_health.beat(self.j, {"at": time.time(), "pid": os.getpid(), "code": procs.code_version(),
                                             "v": wire.PROTOCOL, "mode": self.mode, "running": len(self.inflight)})
            except Exception:  # noqa: BLE001 - a missed beat shows as a stale heartbeat
                log.exception("gateway heartbeat failed")
            await asyncio.sleep(HEARTBEAT_S)

    # ------------------------------------------------------------ a connection
    async def connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if why := wire.foreign(writer):
                log.warning("connection refused: %s", why)
                raise Refused(why)
            hello = await wire.read(reader, HELLO_S)
            if why := self._mismatch(hello):
                raise Refused(why)
            writer.write(wire.encode({"op": "hello", "ok": True, "v": wire.PROTOCOL, "mode": self.mode,
                                      "pid": os.getpid(), "code": procs.code_version(),
                                      "running": len(self.inflight)}))
            while True:
                msg = await wire.read(reader, None)
                if msg.get("op") == "status":
                    iid = str(msg.get("id") or "")
                    writer.write(wire.encode({"id": iid, "status": "status", "answer": await self.gw.status(iid)}))
                elif msg.get("op") == "submit":
                    writer.write(wire.encode(await self.submit(msg, writer)))
                else:
                    raise Refused(f"unknown op {msg.get('op')!r}")
                await writer.drain()
        except Refused as e:
            self._refuse(writer, str(e), e.stale)
        except (ConnectionError, TimeoutError, ValueError) as e:
            log.debug("connection ended: %s", e)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    def _mismatch(self, hello: dict[str, Any]) -> str | None:
        if hello.get("op") != "hello":
            return "say hello first"
        if hello.get("v") != wire.PROTOCOL:
            return f"protocol v{hello.get('v')} is not this gateway's v{wire.PROTOCOL}"
        if hello.get("mode") != self.mode:
            return f"mode {hello.get('mode')} is not this gateway's {self.mode}"
        return None

    @staticmethod
    def _refuse(writer: asyncio.StreamWriter, why: str, stale: bool = False) -> None:
        if not writer.is_closing():
            writer.write(wire.encode({"status": "refused", "ok": False, "error": why, **({"stale": True} if stale
                                                                                       else {})}))

    # ------------------------------------------------------------ an intent
    def parse(self, raw: Any, process: Any = None) -> Intent:
        """The intent as the gateway runs it: a served kind, its args bound to the command's signature."""
        if not isinstance(raw, dict) or not isinstance(raw.get("args"), dict):
            raise Refused("malformed intent")
        kind, iid, actor = raw.get("kind"), raw.get("id"), raw.get("actor")
        if not isinstance(kind, str) or not served(kind, process):
            raise Refused(f"intent kind {kind!r} is not served by the gateway (to a {process or 'session'} client)")
        if gateway.CTX.get(kind) == "guardian" and self.acts is None:
            raise Refused(f"{kind}: this gateway runs no guardian context")
        if not (isinstance(iid, str) and 0 < len(iid) <= 64) or not isinstance(actor, str):
            raise Refused("an intent needs an id (1-64 characters) and an actor")
        cmd = commands.by_kind(kind)
        try:
            base = cmd.intent(raw["args"]) if cmd is not None else Intent(kind, raw["args"], raw.get("symbol"))
            it = dataclasses.replace(base, id=iid, actor=actor, side=str(raw.get("side") or "short"))
        except (TypeError, ValueError) as e:
            raise Refused(f"{kind}: {e}") from e
        if (sig := raw.get("sig")) != (own := gateway.signature_of(kind)):   # D5: the protocol check for intents
            raise Refused(f"{kind} was built for other code than this gateway runs (arguments {sig}, the gateway's "
                          f"{own}): reconnect that session's shortbot (/mcp), or run it through the CLI", stale=True)
        return it

    async def submit(self, msg: dict[str, Any], writer: asyncio.StreamWriter) -> dict[str, Any]:
        raw = msg.get("origin")
        origin_: dict[str, Any] = raw if isinstance(raw, dict) else {}
        process = origin_.get("process")
        it = self.parse(msg.get("intent"), process)
        here = sum(1 for s in self.inflight.values() if s == it.symbol) if it.symbol else 0
        if len(self.inflight) >= BOUND_TOTAL or here >= BOUND_SYMBOL:
            return {"id": it.id, "status": "busy", "error": f"{len(self.inflight)} intents in flight ({here} on "
                    f"{it.symbol}): bounds {BOUND_TOTAL} / {BOUND_SYMBOL} per symbol"}

        def accepted() -> None:
            if not writer.is_closing():
                writer.write(wire.encode({"id": it.id, "status": "accepted"}))
        task = asyncio.create_task(self.execute(it, origin_.get("claude_session"), accepted, process))
        self.inflight[task] = it.symbol
        task.add_done_callback(lambda t: self.inflight.pop(t, None))
        try:
            return {"id": it.id, "status": "final", "result": wire.jsonable(await asyncio.shield(task))}
        except Exception as e:  # noqa: BLE001 - the caller gets it, typed, as it would in-process
            return {"id": it.id, "status": "error", "error": wire.error_dict(e)}

    async def execute(self, it: Intent, claude_session: Any, accepted: Callable[[], None], process: Any = None) -> Any:
        """Runs `it` here, as its caller would have (named without an underscore: the lock audit reads it)."""
        cmd = commands.by_kind(it.kind)
        cls = cmd.weight if cmd is not None else weight.PROTECTIVE
        gw = self.gw_guardian if gateway.CTX.get(it.kind) == "guardian" else self.gw
        with audit.on_behalf(it.actor, claude_session if isinstance(claude_session, str) else None), \
                weight.tagged(cls, wait_s=weight.ENTRY_WAIT_S), self.run_for(process):
            return await gw.submit(it, on_start=accepted)

    def run_for(self, process: Any) -> contextlib.AbstractContextManager[None]:
        """An intent of the guardian runs as the guardian would run it (origin.py)."""
        if process != "guardian":
            return contextlib.nullcontext()
        return origin.guardian(self.acts.n.as_trader_alert if self.acts is not None else None)


# ------------------------------------------------------------ the process
async def serve() -> int:
    from .app import close_app, get_app
    from .entries import EntryDesk
    from .gateway_acts import GuardianActs
    from .notify import Notifier
    app = get_app()
    desk = EntryDesk(app.trader)
    acts = GuardianActs(app, Notifier(app.settings, app.journal))
    srv = GatewayServer(app.journal, app.settings.mode, wire.socket_path(app.settings), desk, acts)
    try:
        await srv.start()
    except RuntimeError as e:
        log.error("%s", e)
        await close_app()
        return 1
    procs.register(app.journal, "gateway", app.settings.mode)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    log.info("gateway stopping")
    await srv.close(DRAIN_S)
    await close_app()
    return 0


def main() -> None:
    from .redact import install_log_redaction
    audit.set_process("gateway", claude=False)   # it acts for the sessions it serves (audit.on_behalf), not its own
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    install_log_redaction()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    raise SystemExit(asyncio.run(serve()))

