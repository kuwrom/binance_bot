"""Limit-entry groups (docs/POSITION_MANAGEMENT.md §c): propose 3-5 shorts at better levels (below a round
number / prior high, or above the stop cluster); the first fill wins and cancels the rest (OCO-style).

Mechanics (Binance USDⓈ-M, verified in docs/ccxt 2026-09):
- entry = limit SELL above the market (rests as maker) with an exchange-side expiry (GTD, ≥ 10 min) so it
  dies even if the guardian is down. GTX (post-only) and GTD are both time-in-force values, so we pick GTD.
- protection: on fill the guardian (5 s poll) places the closePosition stop; optionally a reduce-only stop is
  pre-armed at placement so protection exists from the first fill instant (demo-verify: `prearm_stop`). Make before
  break: the pre-arm is cancelled only once the closePosition stop rests
- a fill is journalled at its exchange fill time; one that was closed again before the poll (its pre-armed stop) is
  rebuilt from trade history as a closed trade (entries_fills.py)
- cancel rules: TTL, price ran away below `cancel_below` (move happened without us), price approaching faster
  than 1·ATR(1h) within 15 min (cascade, not a tap), or the analyst cancels. Replace at most once.
- a cancel pulls the limit first and believes only what filled (`withdraw`): an order that filled before its cancel
  landed keeps its stop and is adopted by the poll; a failed cancel is retried, never relabelled
- risk: every pending order is sized from the CURRENT risk budget; a group counts once (only one may fill)
- journal-first: the row and its client order id are written BEFORE the order is sent; an ambiguous send is
  resolved by that id, never by resending (entry_intents.py). Such a row ('unknown': no order id, no pre-armed stop)
  is looked up by that id on every 5 s poll and cancelled with its group like any order that may rest (#60)
- a fill is protected AND journalled inside the symbol lock: a session's close never finds the position without
  its trade (#60); its plan's resting targets follow in the same lock (#71, targets_sync.at_entry)
Every entry row is research data: fill rate, time-to-fill, level type, and unfilled counterfactuals.

This module: entry rows and their lifecycle (fill, expiry, cancel rules). Placement is mixed in from entries_place.py
(propose_group), amend from entries_amend.py, withdrawing (withdraw, cancel_group) from entries_cancel.py;
entry_gates.py withdraws resting entries in a governor halt, a market pause or a governor resize.
The guardian's sends are intents of its job (#36 step 2b, gateway.py): the poll decides from reads, the handlers in
entries_acts.py send (`act`, through the desk's own gateway).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import ccxt.async_support as ccxt

from . import (
    clientid,
    entries_acts,  # noqa: F401 - registers the entry handlers this desk's intents run
    errors,
)
from . import entries_fills as fills
from .entries_amend import EntryAmend
from .entries_cancel import EntryCancel, amended_on_the_book, dead_label
from .entries_place import EntryPlacement
from .entry_gates import STAMPED, Retry
from .entry_intents import HINTS, ON_BOOK, resolve
from .gateway import LocalGateway
from .intents import Intent
from .symlock import SymbolBusy

if TYPE_CHECKING:
    from .trading import Trader

log = logging.getLogger("shortbot.entries")

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY, group_id TEXT NOT NULL, symbol TEXT NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, expires_ms INTEGER NOT NULL, limit_price REAL, stop_price REAL, amount REAL,
    leverage INTEGER, setup TEXT, thesis TEXT, plan TEXT, level_type TEXT, level_reason TEXT,
    cancel_below REAL, atr_1h REAL, ref_price REAL, ref_ms INTEGER, order_id TEXT, prearm_id TEXT,
    replaced INTEGER DEFAULT 0, trade_id INTEGER, filled_at TEXT, fill_price REAL, filled_qty REAL, reason TEXT
);
"""
FAST_APPROACH_WINDOW_MS = 15 * 60 * 1000
POLL_FAILS_ALERT = 3     # polls of one entry failed in a row before a human is told (a fill may go unseen)
TICKER_TIMEOUT_S = 5.0   # the price for the cancel rules; the fill check doesn't wait on it
POLLED = "status='pending' OR (status='unknown' AND client_id IS NOT NULL)"


class EntryDesk(EntryPlacement, EntryAmend, EntryCancel):
    def __init__(self, trader: Trader, regime_fn: Callable[[], Awaitable[str]] | None = None) -> None:
        self.tr = trader
        self.j = trader.j
        self.j.db.executescript(SCHEMA)
        have = {r[1] for r in self.j.db.execute("PRAGMA table_info(entries)")}
        for col, typ in (("conviction", "TEXT"), ("regime_cell", "TEXT"), ("tranche", "TEXT"), ("leg", "INTEGER"),
                         ("client_id", "TEXT"), ("prearm_cid", "TEXT"), ("abandon", "INTEGER DEFAULT 0"),
                         ("mode", "TEXT NOT NULL DEFAULT 'demo'"), ("experiment", "TEXT"), ("limits", "TEXT"),
                         ("overrides", "TEXT"), *((k, "REAL") for k in STAMPED)):
            if col not in have:
                try:
                    self.j.db.execute(f"ALTER TABLE entries ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError as e:   # the guardian and the MCP server may migrate at once
                    if "duplicate column" not in str(e):
                        raise
        self.j.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS entries_client_id ON entries(client_id)")
        self._regime_fn = regime_fn
        self._cancel_fails: dict[int, int] = {}   # entry id -> sibling-retry cancels failed in a row (guardian memory)
        self._poll_fails: dict[int, int] = {}     # entry id -> polls failed in a row
        self._rt_tries: dict[int, int] = {}       # entry id -> polls a round trip waited for trade history
        self._swept_at = 0.0               # entry_intents: last all-symbol open-orders sweep
        self._orphans_at = 0.0             # orphans: last all-symbol conditional-orders sweep
        self._flagged: set[tuple] = set()  # entry_intents: alerted once (unmatched pre-arms, late orders)
        self.retry = Retry()               # entry_gates: withdrawals that keep failing, resubmitted further apart
        self.gw = LocalGateway(lambda: self.j, ctx=self)   # this desk's entry-row sends (entries_acts.py)

    async def act(self, kind: str, e: dict[str, Any], **args: Any) -> Any:
        """Row `e`'s send, an intent of the caller's job: the guardian's poll, sweeps and reconciler decide, the
        handler (entries_acts.py) sends and answers in today's shape."""
        return await self.gw.submit(Intent(kind, {"entry_id": e["id"], **args}, e["symbol"]))

    async def _regime(self) -> str:
        if self._regime_fn:
            return await self._regime_fn()
        from .market.regime import market_regime
        return (await market_regime(self.tr.ex))["verdict_for_shorts"]

    # ------------------------------------------------------------ storage
    def _rows(self, where: str = "1=1", args: tuple = ()) -> list[dict[str, Any]]:
        """This journal's mode only: a live guardian never polls, cancels or counts a demo order (#32)."""
        rows = self.j._rows(f"SELECT * FROM entries WHERE ({where}) AND mode=? ORDER BY id", (*args, self.j.mode))
        for r in rows:
            r["plan"] = json.loads(r["plan"]) if r.get("plan") else {}
        return rows

    async def _update(self, eid: int, **f: Any) -> None:
        await self.j.write(lambda _j: self._set_row(eid, **f))

    async def _settle(self, eid: int, frm: str, **f: Any) -> bool:
        """Compare-and-set: applies only while the row is still `frm`. One statement, so the MCP server and the
        guardian can't overwrite each other's resolution of the same intent. Returns whether it applied. A unit
        (journal.write, #79): the caller waits for the journal, its event loop never does."""
        return await self.j.write(lambda _j: self._cas_row(eid, frm, **f))

    def _set_row(self, eid: int, **f: Any) -> None:
        """_update's statement, for a caller already inside a unit."""
        self.j.db.execute(f"UPDATE entries SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), eid))

    def _cas_row(self, eid: int, frm: str, **f: Any) -> bool:
        """_settle's statement, for a caller already inside a unit."""
        cur = self.j.db.execute(f"UPDATE entries SET {', '.join(k + '=?' for k in f)} WHERE id=? AND status=?",
                                (*f.values(), eid, frm))
        return cur.rowcount == 1

    def pending(self, group_id: str | None = None, all_orders: bool = False) -> list[dict[str, Any]]:
        """Resting entries. all_orders=True (the analyst's view, the brief) adds every order that is or may be on the
        book: sends of unknown outcome (placing/unknown) and orders the desk didn't place (external), with a hint."""
        where = ON_BOOK if all_orders else "status='pending'"
        rows = self._rows(where + " AND group_id=?", (group_id,)) if group_id else self._rows(where)
        for r in rows:
            if r["status"] in HINTS:
                r["hint"] = HINTS[r["status"]]
        return rows

    # ------------------------------------------------------------ the 5 s loop (guardian)
    async def poll(self) -> list[dict[str, Any]]:
        """Detect fills (protect + journal + cancel siblings), expiries, and cancel conditions. A cancel that doesn't
        kill the order unfilled leaves the row pending: the next pass adopts the fill or retries the cancel.
        One entry's failure never stops the others being polled (_poll_failed streaks and alerts). A pass in which
        every entry failed raises: that is an outage, so the guardian lane escalates and the heartbeat stalls.
        Sends of unknown outcome are polled too, by client id (_poll_unknown)."""
        events: list[dict[str, Any]] = []
        now_ms = int(time.time() * 1000)
        tried, failed = 0, 0
        last: Exception | None = None   # the last entry's failure
        for e in self._rows(POLLED):
            now = self._rows(f"id=? AND ({POLLED})", (e["id"],))
            if not now:
                continue   # a sibling filled earlier in this pass and cancelled it: keep that status and reason
            e = now[0]
            tried += 1
            try:
                ev = await (self._poll_unknown(e, now_ms) if e["status"] == "unknown" else self._poll_one(e, now_ms))
            except SymbolBusy as x:   # a session acts on this symbol: the row stays pending, retried next pass
                log.info("entry #%s skipped this pass: %s", e["id"], x)
                continue
            except Exception as x:  # noqa: BLE001 - isolated: the next entry is still polled
                failed, last = failed + 1, x
                await self._poll_failed(e, x)
                continue
            self._poll_fails.pop(e["id"], None)
            if ev:
                events.append(ev)
        if tried and failed == tried and last is not None:   # (a failure sets last)
            raise last
        return events

    async def _poll_one(self, e: dict, now_ms: int) -> dict[str, Any] | None:
        try:
            o = await self.tr.t.fetch_order(e["order_id"], e["symbol"])
        except ccxt.OrderNotFound:
            if now_ms <= e["expires_ms"]:
                raise
            return await self.act("forget_entry", e)
        if (moved := amended_on_the_book(self.j, e, o)) and await self._settle(e["id"], "pending", **moved):
            self.j.log_decision("amend_entry", moved["reason"], e["symbol"], snapshot={
                "entry_id": e["id"], "old_limit": e["limit_price"], "new_limit": moved["limit_price"]})
            e = {**e, **moved}
        if float(o.get("filled") or 0) > 0:
            return await self.act("protect_fill", e, order=o)
        if e.get("tranche"):
            from .tranches import parent_closed
            if parent_closed(self, e):
                if await self.act("cancel_entry", e, status="cancelled",
                                  reason="tranche trade already closed") == "unfilled":
                    return {"entry_id": e["id"], "event": "cancelled", "why": "tranche trade closed"}
                return None
        if o.get("status") in clientid.DEAD or now_ms > e["expires_ms"]:
            status, reason = dead_label(self.j, e, o)   # by the cancel of ours that took it, if one did
            if await self.act("cancel_entry", e, status=status, reason=reason) == "unfilled":
                return {"entry_id": e["id"], "event": status}
            return None
        why = await self._should_cancel(e, now_ms)
        if why and await self._cancel_by_rule(e, why) == "unfilled":
            return {"entry_id": e["id"], "event": "cancelled", "why": why}
        return None

    async def _poll_unknown(self, e: dict, now_ms: int) -> dict[str, Any] | None:
        """A send of unknown outcome has no pre-armed stop (one is sent only after an ack): if it rests and fills, the
        fill is naked until the row is adopted. So every pass looks it up by its client id rather than leaving it to
        the reconciler's 60 s tick: one weight-1 Query Order, what fetch_order costs a pending row, for at most
        GRACE_S. The reconciler's own rules resolve it (entry_intents.resolve, which alerts); one adopted is polled at
        once, so its fill is protected, or its group's sibling rule cancels it, in this same pass."""
        await resolve(self, e)
        now = self._rows("id=? AND status='pending'", (e["id"],))
        return await self._poll_one(now[0], now_ms) if now else None

    async def _poll_failed(self, e: dict, x: Exception) -> None:
        """A fill whose whole-position stop failed alerts at once (its pre-armed stop is kept); any other failure once
        per streak of POLL_FAILS_ALERT: a fill may be going unprotected or unjournalled."""
        log.warning("entry #%s poll failed: %s: %s", e["id"], type(x).__name__, x)
        try:
            if errors.classify(x) is errors.ClockDrift:   # the lane's own resync never sees an error isolated here
                await self.tr.ex.resync_time()
            n = self._poll_fails[e["id"]] = self._poll_fails.get(e["id"], 0) + 1
            naked = isinstance(x, fills.StopNotPlaced)
            if n == (1 if naked else POLL_FAILS_ALERT):
                await self.tr.alert("entry_poll_failing", str(x) if naked else f"entry #{e['id']}: poll failed {n}x "
                                    f"in a row ({type(x).__name__}: {x}): a fill may go unseen, check Binance",
                                    e["symbol"], True)
        except Exception:  # noqa: BLE001 - reporting a failure must not stop the other entries being polled
            log.exception("entry #%s: failure not reported", e["id"])

    async def _should_cancel(self, e: dict, now_ms: int) -> str | None:
        sib = self._filled_sibling(e)
        if sib:   # OCO: the cancel sent when the sibling filled failed (network), so retry it on every pass
            return f"sibling #{sib} filled"
        try:   # the book the order rests on (ref_price came from it too), bounded: the fill check already ran
            last = float((await asyncio.wait_for(self.tr.t.fetch_ticker(e["symbol"]), TICKER_TIMEOUT_S))["last"])
        except Exception as x:  # noqa: BLE001 (incl. TimeoutError) - skip the price rules this pass
            log.warning("entry #%s price rules skipped: %s: %s", e["id"], type(x).__name__, x)
            return None
        if e["cancel_below"] and last <= e["cancel_below"]:
            return f"price {last} fell below {e['cancel_below']} without filling: the move happened without us"
        if e["atr_1h"] and now_ms - e["ref_ms"] <= FAST_APPROACH_WINDOW_MS and last - e["ref_price"] >= e["atr_1h"]:
            return f"price rose {last - e['ref_price']:.6g} (≥ 1 ATR) toward the limit within 15 min: cascade risk"
        if now_ms - e["ref_ms"] > FAST_APPROACH_WINDOW_MS:
            self.j.post(lambda _j: self._set_row(e["id"], ref_price=last, ref_ms=now_ms))  # rolling 15-min reference
        return None
