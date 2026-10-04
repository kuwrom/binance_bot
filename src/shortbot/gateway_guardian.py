"""The guardian as a client of the order gateway (#36 step 6, docs/GATEWAY.md §3.4, D3).

- for_guardian: with SHORTBOT_GATEWAY on (Settings.gateway_guardian), the guardian's intents go to the gateway: its
  position pass's (ctx: the guardian) and its desk's (ctx: the EntryDesk), each run there with the gateway's ctx for
  its kind. With `sessions` or 0/off they run in the guardian, as before (the rollback).
- Protection never waits for the gateway: one connect attempt (gateway_client.CONNECT_S); after a failure the link
  counts as down for DOWN_S and the next intents don't try; a late_ok kind waits LATE_ACCEPT_S for `accepted` (the
  gateway starts one within gateway.CLAIM_WAIT_S even while the journal is locked), a strict one ACCEPT_S.
- Break-glass for every kind: they all only reduce risk (GATEWAY §4.2: the six late_ok ones can't wait; plan_step and
  pull_targets carry the time stops, plan exits and target pulls; the reconciler's cancels remove resting orders), and
  the lock audit already fails any risk-adding guardian send. It runs here under the symbol flock, actor
  breakglass:guardian:<job>, its intent row the journal of it.
- No alert fatigue from deploys: gateway_down_breakglass is urgent (Telegram, once per BREAKGLASS_COOLDOWN_S) only when
  the gateway is really down: unreachable from here for gateway_health.DOWN_URGENT_S, or its heartbeat that old.
  Shorter (a deploy's restart) it is an alerts row only.
- Answers stay what the guardian's code expects: busy is SymbolBusy (skipped this pass, as a held lock); a run whose
  end this side can't see is IntentPending unless its row says how it ended; errors come typed (gateway_wire).
- ping (the guardian's 30 s gateway job): a hello round trip, kv `gateway_client:guardian` {at, client, ok, why}; the
  deploy health check reads it (deploy/autodeploy.sh)."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import gateway, gateway_client, gateway_health, intents
from .gateway import Gateway, LocalGateway
from .gateway_client import RemoteGateway, Unavailable
from .intents import Intent
from .results import Result
from .symlock import SymbolBusy

log = logging.getLogger("shortbot.gateway")
DOWN_S = 10.0                  # after a failed reach, the next intents break glass without trying
LATE_ACCEPT_S = 5.0            # a late_ok kind: started within gateway.CLAIM_WAIT_S, journal locked or not
BREAKGLASS_COOLDOWN_S = 600.0
KEY = "gateway_client:guardian"


class IntentPending(RuntimeError):
    """The gateway took the intent and its end isn't known here: never resent; the next pass decides again."""


@dataclass
class Link:
    """The guardian's view of the gateway, shared by its two clients."""
    down_since: float | None = None   # wall time of this outage's first failure
    skip_until: float = 0.0           # monotonic: no attempt before

    def failed(self) -> None:
        self.down_since = self.down_since or time.time()
        self.skip_until = time.monotonic() + DOWN_S

    def ok(self) -> None:
        self.down_since, self.skip_until = None, 0.0

    def outage_s(self, now: float | None = None) -> float:
        return 0.0 if self.down_since is None else (now or time.time()) - self.down_since


def for_guardian(journal: Callable[[], Any], settings: Callable[[], Any], ctx: Any, n: Any, link: Link) -> Gateway:
    if settings().gateway_guardian:
        return GuardianClient(journal, settings, ctx, n, link)
    return LocalGateway(journal, ctx)


class GuardianClient(RemoteGateway):
    def __init__(self, journal: Callable[[], Any], settings: Callable[[], Any], ctx: Any, n: Any, link: Link):
        super().__init__(journal, settings, ctx)
        self.n, self.link = n, link

    def process(self) -> str | None:
        return "guardian"

    def retry_s(self) -> float:
        return 0.0

    def accept_s(self, intent: Intent) -> float:
        return LATE_ACCEPT_S if intent.kind in gateway.LATE_OK else gateway_client.ACCEPT_S

    def breaks_glass(self, intent: Intent) -> bool:
        return True

    async def busy(self, intent: Intent, why: Any) -> Any:
        raise SymbolBusy(intent.symbol or "the account", 0, f"the order gateway's queues are full ({why})")

    async def lost(self, intent: Intent) -> Any:
        row = intents.get(self._journal(), intent.id)
        if row is None or row["status"] in intents.OPEN:
            raise IntentPending(f"{intent.kind} {intent.id} runs on in the gateway; the next pass decides again")
        r = Result.from_row(row)
        if r.body is None:
            raise IntentPending(f"{intent.kind} {intent.id} ended {row['status']}: {r.error}")
        return r.body

    async def _connect(self) -> Any:
        if time.monotonic() < self.link.skip_until:
            raise Unavailable(f"unreachable {self.link.outage_s():.0f} s (tried again in under {DOWN_S:g} s)")
        try:
            rw = await super()._connect()
        except Unavailable:
            self.link.failed()
            raise
        self.link.ok()
        return rw

    async def _without(self, intent: Intent, why: str) -> Any:
        self.link.failed()   # refused or silent: not usable for now
        res = await super()._without(intent, why)
        if isinstance(res, dict) and res.get("in_progress"):   # the gateway journalled it first: it runs there
            raise IntentPending(f"{intent.kind} {intent.id} runs on in the gateway; the next pass decides again")
        return res

    async def _alert(self, intent: Intent, why: str) -> None:
        j, now = self._journal(), time.time()
        down = max(self.link.outage_s(now), gateway_health.age_s(j, now))
        msg = (f"The order gateway is unavailable ({why}). {intent.kind} (intent {intent.id}, {intent.actor}) ran in "
               "the guardian's own process as break-glass, under the symbol lock")
        try:
            if down >= gateway_health.DOWN_URGENT_S:
                await self.n.send("gateway_down_breakglass", f"{msg}; the gateway has been down {down:.0f} s. Bring "
                                  "it back (deploy/DEPLOY.md §4a).", cooldown_s=BREAKGLASS_COOLDOWN_S, urgent=True)
            else:   # a restart (every deploy has one): recorded, nobody paged
                j.log_alert("gateway_down_breakglass", f"{msg} (down {down:.0f} s: a restart, not urgent).",
                            intent.symbol)
        except Exception:  # noqa: BLE001 - the alert never undoes the protection
            log.exception("gateway_down_breakglass alert failed")

    async def ping(self) -> dict[str, Any]:
        """Hello and close, past the down memo: can the guardian reach a live gateway now?"""
        try:
            _, w = await super()._connect()
        except Unavailable as e:
            self.link.failed()
            return {"ok": False, "why": str(e)}
        w.close()
        self.link.ok()
        return {"ok": True, "why": None}


async def ping(gw: Gateway, j: Any) -> dict[str, Any]:
    """The guardian's gateway check (module doc); kv KEY, posted."""
    rec: dict[str, Any] = {"at": time.time(), "client": isinstance(gw, GuardianClient), "ok": None, "why": None}
    if isinstance(gw, GuardianClient):
        rec.update(await gw.ping())
    j.post(lambda j: j.set(j.mode_key(KEY), json.dumps(rec)))
    return rec
