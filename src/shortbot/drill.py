"""Demo-only plumbing drills (#46): `shortbot drill NAME`, modelled on the probes (probes.py). A probe checks what
Binance does; a drill checks that the desk's own controls do what the runbook says, end to end, on the demo account.

- Refusals (nothing sent, nothing journalled): settings.mode demo and the trade client on demo-fapi, the kill switch
  ACTIVE with a verified guardian (probes._outside: killswitch.gate), both drill symbols listed and clean (probes.
  _inside: no journal trade, no resting entry, no position or order on them), and what each drill needs on top: the
  kill-switch drill a flat desk (no open trade, no resting entry, no position: its flatten closes everything), the
  restart drill no resting entry anywhere (its stale-heartbeat halt withdraws every one).
- Steps: each is the real command's intent (commands.py), submitted as `shortbot <subcommand>` submits it
  (gateway_client.for_command: to the order gateway when SHORTBOT_GATEWAY is on), actor cli:drill:<name>. The drill
  command itself is no intent: an intent's code never submits another (gateway.py), so it orchestrates and journals a
  drill_step per step and a drill_result per run (drill_log.py).
- Its trades: setup exp_drill (kept out of every statistic: journal_tables.NOT_DRILL), MIN_NOTIONAL x 1.05
  (steps.min_size, as the probes size), a full plan (trigger, invalidation, two far targets, a 3 h auto time stop: a
  drill that dies leaves its position for the guardian to close), veto_override OVERRIDE (the answer says so), a
  lesson on every exit.
- kv drill:window:<name> (uptime.py) while it runs: a guardian gap inside it is tagged as the drill's.
Cases: drill_kill.py (kill-switch), drill_restart.py (restart). The backup drill is backup_drill.py."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from . import audit, commands, drill_log, gateway_client, intents, probes, uptime
from . import killswitch as ks
from .drill_log import OVERRIDE, check
from .entries import EntryDesk
from .exchange import to_raw, to_symbol
from .journal import now_iso
from .journal_tables import DRILL_SETUP
from .steps import min_size

SYMBOL, SECOND = "XRPUSDT", "ADAUSDT"   # liquid perps the desk rarely trades; --symbol / --second to change
STOP_PCT = 10.0                         # the drill short's stop, above the price
ENTRY_PCT, ENTRY_STOP_PCT = 15.0, 30.0  # the far resting entry: it must rest, never fill
BRIEF_CHARS = 1500
STATUS_WAIT_S = 10.0


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)   # tests patch it


class DrillStop(RuntimeError):
    """A step the drill can't go on without failed: the cleanup runs, the result says which."""


def brief(res: Any) -> Any:
    """A step's answer as journalled: whole when short, else its main keys."""
    if len(json.dumps(res, default=str)) <= BRIEF_CHARS or not isinstance(res, dict):
        return res
    keep = ("ok", "error", "trade_id", "kill_switch", "changed", "state", "group_id", "placed", "rejected", "closed",
            "failed", "still_open", "resting", "stop", "fill", "amount", "vetoed")
    return {**{k: res[k] for k in keep if k in res}, "truncated": True}


def _ok(res: Any) -> bool:
    return not (isinstance(res, dict) and res.get("ok") is False)


# ------------------------------------------------------------ refusals
async def refusal(tr: Any, syms: list[str], name: str, flat: bool = False, no_entries: bool = False) -> str | None:
    """Why this drill can't run now (module doc), or None."""
    if len(set(syms)) != len(syms):
        return f"the drill symbols must differ: {syms}"
    for s in syms:
        if why := await probes._outside(tr, s, name, what="drill"):
            return why
    if flat:
        if trades := tr.j.open_trades(include_opening=True):
            return f"the kill-switch drill needs a flat desk: open trades {[t['symbol'] for t in trades]}"
        if pos := await tr.raw_positions():
            return f"the kill-switch drill needs a flat desk: positions on {[p['symbol'] for p in pos]}"
    if (flat or no_entries) and (rows := EntryDesk(tr).pending(all_orders=True)):
        return (f"resting entries {[r['id'] for r in rows]}: the drill's halt would withdraw them. Run it when none "
                "rests")
    for s in syms:
        if why := await probes._inside(tr, s, name, what="drill"):
            return why
    return None


# ------------------------------------------------------------ one run
class Drill:
    def __init__(self, app: Any, name: str, run: str | None = None, started: str | None = None):
        self.app, self.tr, self.j, self.name = app, app.trader, app.journal, name
        self.run, self.started, self.t0 = run or intents.new_id(), started or now_iso(), time.monotonic()
        self.actor = f"cli:drill:{name}"
        self.steps: list[dict[str, Any]] = []
        self.checks: dict[str, dict[str, Any]] = {}

    def check(self, name: str, ok: bool, detail: Any = None) -> bool:
        self.checks[name] = check(ok, detail)
        return bool(ok)

    def note(self, step: str, snapshot: dict[str, Any], symbol: str | None = None) -> None:
        """A drill_step row of the drill's own (a snapshot, a wait): no intent."""
        with audit.acting(self.actor):
            self.j.log_decision(drill_log.STEP, f"{self.name}: {step}", symbol,
                                snapshot={"run": self.run, "drill": self.name, "step": step, **snapshot})

    async def submit(self, step: str, cli: str, **kwargs: Any) -> dict[str, Any]:
        """One step: the `shortbot <cli>` command's intent, sent where that command's would go; journalled."""
        commands.load()   # the declarations (cmd_*.py): imported once
        cmd = commands.by_cli(cli)
        t0 = time.monotonic()
        with audit.acting(self.actor):
            it = cmd.intent(kwargs)
            gw = gateway_client.for_command(cmd, lambda: self.j, lambda: self.app.settings)
            try:
                res = await gw.submit(it)
            except Exception as e:  # noqa: BLE001 - recorded as the step's answer: the drill's checks decide
                res = {"ok": False, "raised": True, "error": f"{type(e).__name__}: {e}"[:500]}
        res = res if isinstance(res, dict) else {"result": res}
        row = {"step": step, "kind": cmd.kind, "intent_id": it.id, "symbol": it.symbol,
               "via": "gateway" if isinstance(gw, gateway_client.RemoteGateway) else "local",
               "ms": round((time.monotonic() - t0) * 1000), "ok": _ok(res), "result": brief(res)}
        self.steps.append(row)
        self.note(step, {k: v for k, v in row.items() if k != "step"}, it.symbol)
        return res

    async def intent_status(self, intent_id: str) -> dict[str, Any]:
        """The step's intent row once final (status, sent); its own process may post it a moment later."""
        end = time.monotonic() + STATUS_WAIT_S
        while True:
            row = intents.get(self.j, intent_id) or {}
            if row.get("status") not in (None, *intents.OPEN) or time.monotonic() > end:
                return {"status": row.get("status"), "sent": row.get("sent")}
            await _sleep(0.2)

    @asynccontextmanager
    async def window(self, seconds: float) -> AsyncIterator[None]:
        await self.j.write(uptime.open_window, self.name, seconds)
        try:
            yield
        finally:
            await self.j.write(uptime.close_window, self.name)

    def finish(self, verdict: str | None = None, **extra: Any) -> dict[str, Any]:
        extra.pop("drill", None)   # the caller's answer head: the result names its drill itself
        v = verdict or drill_log.verdict(self.checks)
        with audit.acting(self.actor):
            return drill_log.result(self.j, self.name, v, self.checks, run=self.run, started=self.started,
                                    seconds=round(time.monotonic() - self.t0, 1), steps=self.steps,
                                    intents=[s["intent_id"] for s in self.steps], override=OVERRIDE,
                                    override_note=f"every drill trade carries veto_override={OVERRIDE!r}: it "
                                                  "overrides any veto, regime or cold-desk gate it meets", **extra)

    # ------------------------------------------------------------ the drill's trades
    def plan(self, price: float, sym: str, targets_auto: bool) -> dict[str, Any]:
        return {"trigger": f"{OVERRIDE} ({self.name}): a scheduled plumbing test, no market read",
                "invalidation": "none: the drill closes it itself; one that dies leaves it to the 3 h time stop",
                "targets": [{"price": self.px(sym, price, -30), "fraction": 0.5},
                            {"price": self.px(sym, price, -40), "fraction": 0.5}],
                "time_stop_hours": 3.0, "review_every_hours": 1.0,
                "kill_conditions": ["any drill step fails: the drill closes it"],
                "auto": {"breakeven": False, "trail": False, "targets": targets_auto, "time_stop": True}}

    def px(self, sym: str, price: float, pct: float) -> float:
        return float(self.tr.t.price_to_precision(sym, price * (1 + pct / 100)))

    async def price(self, sym: str) -> float:
        return float((await self.tr.t.fetch_ticker(sym))["last"])

    async def open_short(self, sym: str, step: str = "open the drill short", targets_auto: bool = False,
                         min_steps: int = 1) -> tuple[dict[str, Any], float]:
        """The `open` command at the minimum size; (its answer, the price it was sized at)."""
        price = await self.price(sym)
        qty = min_size(self.tr.t, sym, price, min_steps=min_steps)
        res = await self.submit(step, "open", symbol=to_raw(sym), stop_price=self.px(sym, price, STOP_PCT),
                                setup=DRILL_SETUP, thesis=f"{OVERRIDE}: {self.name}",
                                plan=self.plan(price, sym, targets_auto), notional_usdt=round(qty * price * 1.01, 4),
                                leverage=2, veto_override=OVERRIDE)
        return res, price

    async def rest_entry(self, sym: str, ttl_hours: float) -> dict[str, Any]:
        """The `propose` command: one far limit entry, the smallest size the budget allows (size_mult 0.1)."""
        price = await self.price(sym)
        prop = {"symbol": to_raw(sym), "limit_price": self.px(sym, price, ENTRY_PCT),
                "stop_price": self.px(sym, price, ENTRY_STOP_PCT), "setup": DRILL_SETUP,
                "thesis": f"{OVERRIDE}: {self.name}", "plan": self.plan(price, sym, False), "leverage": 2,
                "level_type": "drill", "level_reason": "a far drill entry: it must rest, never fill", "size_mult": 0.1,
                "veto_override": OVERRIDE}
        return await self.submit("rest a far entry", "propose", proposals=[prop], ttl_hours=ttl_hours,
                                 warmup_override=OVERRIDE)

    # ------------------------------------------------------------ reads
    async def order_open(self, sym: str, order_id: Any) -> bool:
        return any(str(o.get("id")) == str(order_id) for o in await self.tr.t.fetch_open_orders(sym))

    def entry_row(self, entry_id: int | None) -> dict[str, Any]:
        return next(iter(EntryDesk(self.tr)._rows("id=?", (entry_id,))), {}) if entry_id is not None else {}

    def state(self) -> str:
        return str(ks.read(self.j)["state"])

    async def closed_trade(self, tid: int) -> dict[str, Any]:
        """The trade's row once closed (the process that closed it may post it a moment later), else as it is."""
        end = time.monotonic() + STATUS_WAIT_S
        while (t := self.j.get_trade(tid) or {}).get("status") != "closed" and time.monotonic() < end:
            await _sleep(0.2)
        return t

    # ------------------------------------------------------------ cleanup
    async def cleanup(self, syms: list[str], group: str | None, lesson: str) -> None:
        """After a failed step: the drill's entry cancelled, its positions closed (allowed in any state). A position
        the cleanup can't close keeps its stop and its 3 h time stop; the answer lists what is left."""
        if group and EntryDesk(self.tr).pending(group, all_orders=True):
            await self.submit("cleanup: cancel the drill entry", "cancel-group", group_id=group,
                              reason=f"{OVERRIDE}: {self.name} cleanup")
        for s in syms:
            if await self.tr.position(s):
                await self.submit(f"cleanup: close {s}", "close", symbol=to_raw(s),
                                  reason=f"{OVERRIDE}: {self.name} cleanup", lesson=lesson)

    async def left(self, syms: list[str]) -> dict[str, Any]:
        return {s: {"position": bool(await self.tr.position(s)), "stops": len(await self.tr.stop_orders(s, True)),
                    "orders": len(await self.tr.t.fetch_open_orders(s))} for s in syms}


# ------------------------------------------------------------ the command
async def run(app: Any, name: str, what: str | None = None, phase: str | None = None, verify: bool = False,
              symbol: str = SYMBOL, second: str = SECOND, evidence: str | None = None,
              wait_min: float = 15.0) -> dict[str, Any]:
    from . import drill_kill, drill_restart
    syms = [to_symbol(symbol), to_symbol(second)]
    if name == "status":
        return {"ok": True, "results": drill_log.latest_results(app.journal),
                "uptime": uptime.summary(app.journal)}
    if name == "attest":
        with audit.acting("cli:drill:attest"):
            return drill_log.attest(app.journal, what or "", evidence or "")
    if name == "kill-switch":
        return await (drill_kill.verify(app) if verify else drill_kill.run(app, *syms))
    if name == "restart":
        if phase == "before":
            return await drill_restart.before(app, *syms)
        if phase == "after":
            return await drill_restart.after(app, min(15.0, max(0.0, wait_min)))
        return {"ok": False, "error": "restart runs in two phases: --phase before, then (guardian stopped >= 11 min, "
                                      "the watchdog run, the guardian started) --phase after"}
    return {"ok": False, "error": f"unknown drill {name!r}"}
