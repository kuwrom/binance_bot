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
  resolved by that id, never by resending (entry_intents.py)
Every entry row is research data: fill rate, time-to-fill, level type, and unfilled counterfactuals.

This module: entry rows and their lifecycle (fill, expiry, cancel, amend). Placement is mixed in from
entries_place.py (propose_group).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from typing import Any

import ccxt.async_support as ccxt

from . import entries_fills as fills
from . import killswitch
from .entries_place import MIN_TTL_S, EntryPlacement  # noqa: F401 (MIN_TTL_S re-export)
from .entry_intents import HINTS, ON_BOOK, located
from .stops import is_clock_drift
from .symlock import SymbolBusy

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
DEAD = ("canceled", "expired", "rejected")
CANCEL_FAILS_ALERT = 3   # sibling-retry cancels failed in a row before a human is told (once per streak)
POLL_FAILS_ALERT = 3     # polls of one entry failed in a row before a human is told (a fill may go unseen)
TICKER_TIMEOUT_S = 5.0   # the price for the cancel rules; the fill check doesn't wait on it


async def withdraw(t, e: dict[str, Any]) -> str:
    """Cancel a pending entry's limit FIRST, then decide from what actually filled:
    - "unfilled": the limit is dead with nothing filled, so its pre-armed stop is cancelled too
    - "filled": it (partly) filled before the cancel landed: the stop and the pending row stay, the poll adopts it
    - "unknown": the cancel failed (network, clock, a send not found yet): nothing changes, a later pass retries
    Binance answers -2011 (ccxt OrderNotFound) for ANY order no longer in the book, filled or dead, so fetch_order
    decides (MOVR 2026-10-01: a swallowed -2011 stripped a filled order's stop and left a naked short).
    A row with no order id (a send of unknown outcome) is first looked up by its client id (entry_intents.located)."""
    if not e.get("order_id"):
        e = await located(t, e)
        if isinstance(e, str):
            return e
    try:
        o = await t.cancel_order(e["order_id"], e["symbol"]) or {}
    except ccxt.OrderNotFound:
        try:
            o = await t.fetch_order(e["order_id"], e["symbol"])
        except Exception:  # noqa: BLE001
            return "unknown"
        if not float(o.get("filled") or 0) and o.get("status") not in DEAD:
            return "unknown"
    except Exception:  # noqa: BLE001 - not sent, or sent with the outcome unknown: the order may still rest
        return "unknown"
    if float(o.get("filled") or 0) > 0:
        return "filled"
    if e.get("prearm_id"):
        try:
            await t.cancel_order(e["prearm_id"], e["symbol"], params={"trigger": True})
        except Exception:  # noqa: BLE001 - best effort: with no position a reduce-only stop has nothing to close
            pass
    return "unfilled"


class EntryDesk(EntryPlacement):
    def __init__(self, trader, regime_fn=None):
        self.tr = trader
        self.j = trader.j
        self.j.db.executescript(SCHEMA)
        have = {r[1] for r in self.j.db.execute("PRAGMA table_info(entries)")}
        for col, typ in (("conviction", "TEXT"), ("regime_cell", "TEXT"), ("tranche", "TEXT"), ("leg", "INTEGER"),
                         ("client_id", "TEXT"), ("prearm_cid", "TEXT"), ("abandon", "INTEGER DEFAULT 0"),
                         ("mode", "TEXT NOT NULL DEFAULT 'demo'"), ("experiment", "TEXT"), ("limits", "TEXT")):
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
        self._flagged: set[tuple] = set()  # entry_intents: alerted once (unmatched pre-arms, late orders)

    async def _regime(self) -> str:
        if self._regime_fn:
            return await self._regime_fn()
        from .scanner import market_regime
        return (await market_regime(self.tr.ex))["verdict_for_shorts"]

    # ------------------------------------------------------------ storage
    def _rows(self, where: str = "1=1", args: tuple = ()) -> list[dict[str, Any]]:
        """This journal's mode only: a live guardian never polls, cancels or counts a demo order (#32)."""
        rows = self.j._rows(f"SELECT * FROM entries WHERE ({where}) AND mode=? ORDER BY id", (*args, self.j.mode))
        for r in rows:
            r["plan"] = json.loads(r["plan"]) if r.get("plan") else {}
        return rows

    def _update(self, eid: int, **f: Any) -> None:
        self.j.db.execute(f"UPDATE entries SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), eid))
        self.j.db.commit()

    def _settle(self, eid: int, frm: str, **f: Any) -> bool:
        """Compare-and-set: applies only while the row is still `frm`. One statement, so the MCP server and the
        guardian can't overwrite each other's resolution of the same intent. Returns whether it applied."""
        cur = self.j.db.execute(f"UPDATE entries SET {', '.join(k + '=?' for k in f)} WHERE id=? AND status=?",
                                (*f.values(), eid, frm))
        self.j.db.commit()
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

    # ------------------------------------------------------------ cancel / amend
    async def _cancel_entry(self, e: dict, status: str, reason: str) -> str:
        """Relabel the row only when its limit died unfilled; returns withdraw's outcome."""
        async with self.tr.symbol_lock(e["symbol"], f"cancel entry #{e['id']}: {reason}"):
            got = await withdraw(self.tr.t, e)
            if got == "unfilled":
                self._update(e["id"], status=status, reason=reason)
        return got

    def _filled_sibling(self, e: dict) -> int | None:
        """Another order of this OCO group that already filled ('error' = a fill was reported); a split entry's own
        legs don't count, they are one unit."""
        rows = self._rows("group_id=? AND id!=? AND status IN ('filled','error')"
                          " AND (tranche IS NULL OR tranche IS NOT ?)", (e["group_id"], e["id"], e.get("tranche")))
        return rows[0]["id"] if rows else None

    async def cancel_group(self, group_id: str, reason: str, keep: int | None = None,
                           keep_tranche: str | None = None) -> dict[str, Any]:
        """filled_first: filled before the cancel landed, so they keep their stops and the guardian's poll adopts them.
        cancel_failed: still live and pending; call again."""
        got: dict[str, list[int]] = {"unfilled": [], "filled": [], "unknown": []}
        for e in self.pending(group_id):
            if e["id"] != keep and not (keep_tranche and e.get("tranche") == keep_tranche):
                got[await self._cancel_entry(e, "cancelled", reason)].append(e["id"])
        return {"ok": not got["unknown"], "cancelled": len(got["unfilled"]), "filled_first": got["filled"],
                "cancel_failed": got["unknown"]}

    async def amend(self, entry_id: int, new_limit: float, reason: str) -> dict[str, Any]:
        rows = self._rows("id=? AND status='pending'", (entry_id,))
        if not rows:
            return {"ok": False, "error": "no pending entry with that id"}
        e = rows[0]
        if e["replaced"]:
            return {"ok": False, "error": "already replaced once (rule: replace at most once, don't chase)"}
        if new_limit >= e["stop_price"]:
            return {"ok": False, "error": "new limit must stay below the stop"}
        t = self.tr.t
        px = float(t.price_to_precision(e["symbol"], new_limit))
        # a lower sell limit fills sooner and loses more at the stop; raising it toward the stop only reduces risk
        lower = px < e["limit_price"]
        if lower and (blocked := await killswitch.gate(self.tr, "amend (lower limit)")):
            return blocked
        async with self.tr.symbol_lock(e["symbol"], f"amend entry #{entry_id}: {reason}"):
            if not self._rows("id=? AND status='pending' AND COALESCE(replaced, 0)=0 AND limit_price=?",
                              (entry_id, e["limit_price"])):
                return {"ok": False, "error": f"entry #{entry_id} changed since you looked (filled, cancelled or "
                                              "amended meanwhile): re-read pending_entries"}
            if lower and (blocked := killswitch.state_refusal(self.j, "amend (lower limit)")):
                return blocked
            await t.edit_order(e["order_id"], e["symbol"], "limit", "sell", e["amount"], px)
            self._update(e["id"], limit_price=px, replaced=1, reason=f"amended: {reason}")
        return {"ok": True, "entry_id": entry_id, "new_limit": px}

    # ------------------------------------------------------------ the 5 s loop (guardian)
    async def poll(self) -> list[dict[str, Any]]:
        """Detect fills (protect + journal + cancel siblings), expiries, and cancel conditions. A cancel that doesn't
        kill the order unfilled leaves the row pending: the next pass adopts the fill or retries the cancel.
        One entry's failure never stops the others being polled (_poll_failed streaks and alerts). A pass in which
        every entry failed raises: that is an outage, so the guardian lane escalates and the heartbeat stalls."""
        events: list[dict[str, Any]] = []
        now_ms = int(time.time() * 1000)
        tried, failed, last = 0, 0, None
        for e in self.pending():
            if not self._rows("id=? AND status='pending'", (e["id"],)):
                continue   # a sibling filled earlier in this pass and cancelled it: keep that status and reason
            tried += 1
            try:
                ev = await self._poll_one(e, now_ms)
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
        if tried and failed == tried:
            raise last
        return events

    async def _poll_one(self, e: dict, now_ms: int) -> dict[str, Any] | None:
        try:
            o = await self.tr.t.fetch_order(e["order_id"], e["symbol"])
        except ccxt.OrderNotFound:
            if now_ms <= e["expires_ms"]:
                raise
            return await self._forgotten(e)
        if float(o.get("filled") or 0) > 0:
            return await self._on_fill(e, o)
        if e.get("tranche"):
            from .tranches import parent_closed
            if parent_closed(self, e):
                if await self._cancel_entry(e, "cancelled", "tranche trade already closed") == "unfilled":
                    return {"entry_id": e["id"], "event": "cancelled", "why": "tranche trade closed"}
                return None
        if o.get("status") in DEAD or now_ms > e["expires_ms"]:
            if await self._cancel_entry(e, "expired", f"exchange status {o.get('status')}") == "unfilled":
                return {"entry_id": e["id"], "event": "expired"}
            return None
        why = await self._should_cancel(e, now_ms)
        if why and await self._cancel_by_rule(e, why) == "unfilled":
            return {"entry_id": e["id"], "event": "cancelled", "why": why}
        return None

    async def _forgotten(self, e: dict) -> dict[str, Any] | None:
        """Not found past its GTD: Binance forgets an unfilled CANCELED/EXPIRED order 3 days after its creation and
        keeps any order with a fill for 90 days (Query-Order doc), so it expired unfilled. Its pre-armed stop goes."""
        if e.get("prearm_id"):
            async with self.tr.symbol_lock(e["symbol"], f"entry #{e['id']}: drop a forgotten entry's pre-arm"):
                try:
                    await self.tr.t.cancel_order(e["prearm_id"], e["symbol"], params={"trigger": True})
                except Exception:  # noqa: BLE001 - best effort, as in withdraw
                    pass
        why = "order forgotten by Binance (-2013) past its GTD: it expired unfilled"
        return {"entry_id": e["id"], "event": "expired", "why": why} if self._settle(
            e["id"], "pending", status="expired", reason=why) else None

    async def _poll_failed(self, e: dict, x: Exception) -> None:
        """A fill whose whole-position stop failed alerts at once (its pre-armed stop is kept); any other failure once
        per streak of POLL_FAILS_ALERT: a fill may be going unprotected or unjournalled."""
        log.warning("entry #%s poll failed: %s: %s", e["id"], type(x).__name__, x)
        try:
            if is_clock_drift(x):   # the lane's own resync never sees an error isolated here
                await self.tr.ex.resync_time()
            n = self._poll_fails[e["id"]] = self._poll_fails.get(e["id"], 0) + 1
            naked = isinstance(x, fills.StopNotPlaced)
            if n == (1 if naked else POLL_FAILS_ALERT):
                await self.tr.alert("entry_poll_failing", str(x) if naked else f"entry #{e['id']}: poll failed {n}x "
                                    f"in a row ({type(x).__name__}: {x}): a fill may go unseen, check Binance",
                                    e["symbol"], True)
        except Exception:  # noqa: BLE001 - reporting a failure must not stop the other entries being polled
            log.exception("entry #%s: failure not reported", e["id"])

    async def _cancel_by_rule(self, e: dict, why: str) -> str:
        """A cancel rule fired. When the OCO retry keeps failing, a second order stays live in a group that already
        filled: tell a human once per streak of CANCEL_FAILS_ALERT failures in a row."""
        got = await self._cancel_entry(e, "cancelled", why)
        if got != "unknown" or not why.startswith("sibling #"):
            self._cancel_fails.pop(e["id"], None)
            return got
        n = self._cancel_fails[e["id"]] = self._cancel_fails.get(e["id"], 0) + 1
        if n == CANCEL_FAILS_ALERT:
            await self.tr.alert("entry_cancel_failing", f"entry #{e['id']}: cancel failing ({why}), order still live",
                                e["symbol"], True)
        return got

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
            self._update(e["id"], ref_price=last, ref_ms=now_ms)  # rolling 15-min reference
        return None

    async def _on_fill(self, e: dict, o: dict) -> dict[str, Any]:
        """Protect, then journal. The remainder of a partial fill is pulled first (withdraw: what filled is believed,
        the pre-arm is not touched here). Make before break: the pre-armed stop goes only once the whole-position
        stop rests; if that stop can't be placed the pre-arm stays and the row stays pending (StopNotPlaced)."""
        tr, sym = self.tr, e["symbol"]
        rest, cps = "filled", []
        async with tr.symbol_lock(sym, f"entry #{e['id']} filled: protect it"):
            if o.get("status") == "open":   # partly filled and still resting
                rest = await withdraw(tr.t, {**e, "prearm_id": None})
            pos = await tr._verified_position(sym)
            if pos:
                cps = await tr.stop_orders(sym, close_position_only=True)
                if not cps:   # a pre-armed reduce-only stop (another leg / order) is NOT whole-position protection
                    try:
                        await tr._place_stop(sym, e["stop_price"], tries=3)
                    except Exception as x:  # noqa: BLE001
                        raise fills.StopNotPlaced(e, x) from x
                if e["prearm_id"]:
                    try:
                        await tr.t.cancel_order(e["prearm_id"], sym, params={"trigger": True})
                    except Exception:  # noqa: BLE001
                        pass
        if rest == "unknown":
            return await self._rest_unknown(e)
        self._cancel_fails.pop(e["id"], None)
        jt = self.j.active_trade(sym) or {}   # a widened trade keeps its wider stop (its size was cut for it, #14)
        plan_stop = max(e["stop_price"], float(jt.get("stop_price") or 0)) if "widened" in (jt.get("notes") or "") \
            else e["stop_price"]
        if cps and min(c["trigger"] for c in cps) > plan_stop * 1.0001:
            # e.g. the guardian's +10% fallback stop landed first: tighten it to this entry's plan stop
            await tr.move_stop(sym, plan_stop, f"entry #{e['id']} filled: plan stop")
        trades = await fills.history(tr.t, e, o)
        if not pos:   # filled and closed again before this poll (its pre-armed stop): rebuild it from history
            return await fills.round_trip(self, e, o, trades)
        at = fills.first_fill_ms(trades, e, o)
        from . import tranches
        tid = tranches.open_trade_id(self, e)   # a split entry's later leg, or a same-symbol sibling that filled too
        if tid:
            return await tranches.join(self, e, pos, tid, fills.iso(at))
        sib = self._filled_sibling(e)            # a sibling on another symbol filled too: two positions, one budget
        orphan = bool(e.get("tranche")) and tranches.parent_closed(self, e)   # a fresh position nobody asked for
        fill, qty = float(pos["entryPrice"]), float(pos["contracts"])
        tid = fills.new_trade(self, e, at)
        if not self._settle(e["id"], "pending", status="filled", trade_id=tid, filled_at=fills.iso(at),
                            fill_price=fill, filled_qty=qty):
            self.j.abort_trade(tid, f"entry #{e['id']} was resolved elsewhere first")
            return {"entry_id": e["id"], "event": "resolved_elsewhere"}
        self.j.activate_trade(tid, fill, qty, round(fill * qty, 2))
        others = await self.cancel_group(e["group_id"], f"sibling #{e['id']} filled", keep=e["id"],
                                         keep_tranche=e.get("tranche"))
        head = ((f"sibling #{sib} also filled: group over its single-bet budget. " if sib else "")
                + (f"leg filled after its trade closed: new trade #{tid}. " if orphan else ""))
        await tr.alert("action_fill", head + f"limit short filled {qty} @ {fill}, stop {e['stop_price']} placed; "
                       f"{others['cancelled']} sibling order(s) cancelled{fills.late_note(others)}", sym, bool(head))
        return {"entry_id": e["id"], "event": "filled", "trade_id": tid, "fill": fill, "qty": qty}

    async def _rest_unknown(self, e: dict) -> dict[str, Any]:
        """The fill is protected but its remainder's cancel failed: it may still rest and fill more, so the row stays
        pending (the next pass retries the cancel, then journals all that filled). A streak alerts once."""
        n = self._cancel_fails[e["id"]] = self._cancel_fails.get(e["id"], 0) + 1
        if n == CANCEL_FAILS_ALERT:
            await self.tr.alert("entry_cancel_failing", f"entry #{e['id']}: filled in part, the rest won't cancel "
                                "(order still live); position protected, journalling waits", e["symbol"], True)
        return {"entry_id": e["id"], "event": "fill_unsettled"}
