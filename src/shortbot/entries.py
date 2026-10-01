"""Limit-entry groups (docs/POSITION_MANAGEMENT.md §c): propose 3-5 shorts at better levels (below a round
number / prior high, or above the stop cluster); the first fill wins and cancels the rest (OCO-style).

Mechanics (Binance USDⓈ-M, verified in docs/ccxt 2026-09):
- entry = limit SELL above the market (rests as maker) with an exchange-side expiry (GTD, ≥ 10 min) so it
  dies even if the guardian is down. GTX (post-only) and GTD are both time-in-force values, so we pick GTD.
- protection: on fill the guardian (5 s poll) places the closePosition stop; optionally a reduce-only stop is
  pre-armed at placement so protection exists from the first fill instant (demo-verify: `prearm_stop`)
- cancel rules: TTL, price ran away below `cancel_below` (move happened without us), price approaching faster
  than 1·ATR(1h) within 15 min (cascade, not a tap), or the analyst cancels. Replace at most once.
- a cancel pulls the limit first and believes only what filled (`withdraw`): an order that filled before its cancel
  landed keeps its stop and is adopted by the poll; a failed cancel is retried, never relabelled
- risk: every pending order is sized from the CURRENT risk budget; a group counts once (only one may fill)
Every entry row is research data: fill rate, time-to-fill, level type, and unfilled counterfactuals.

This module: entry rows and their lifecycle (fill, expiry, cancel, amend). Placement is mixed in from
entries_place.py (propose_group).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import ccxt.async_support as ccxt

from .entries_place import MIN_TTL_S, EntryPlacement, _now_iso  # noqa: F401 (MIN_TTL_S re-export)
from .market.market_map import cached
from .sessions import session_bucket

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


async def withdraw(t, e: dict[str, Any]) -> str:
    """Cancel a pending entry's limit FIRST, then decide from what actually filled:
    - "unfilled": the limit is dead with nothing filled, so its pre-armed stop is cancelled too
    - "filled": it (partly) filled before the cancel landed: the stop and the pending row stay, the poll adopts it
    - "unknown": the cancel failed (network, clock, no order id): nothing changes, a later pass retries
    Binance answers -2011 (ccxt OrderNotFound) for ANY order no longer in the book, filled or dead, so fetch_order
    decides (MOVR 2026-10-01: a swallowed -2011 stripped a filled order's stop and left a naked short)."""
    if not e.get("order_id"):
        return "unknown"
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
        for col, typ in (("conviction", "TEXT"), ("regime_cell", "TEXT"), ("tranche", "TEXT"), ("leg", "INTEGER")):
            if col not in have:
                self.j.db.execute(f"ALTER TABLE entries ADD COLUMN {col} {typ}")
        self._regime_fn = regime_fn
        self._cancel_fails: dict[int, int] = {}   # entry id -> sibling-retry cancels failed in a row (guardian memory)

    async def _regime(self) -> str:
        if self._regime_fn:
            return await self._regime_fn()
        from .scanner import market_regime
        return (await market_regime(self.tr.ex))["verdict_for_shorts"]

    # ------------------------------------------------------------ storage
    def _rows(self, where: str = "1=1", args: tuple = ()) -> list[dict[str, Any]]:
        rows = self.j._rows(f"SELECT * FROM entries WHERE {where} ORDER BY id", args)
        for r in rows:
            r["plan"] = json.loads(r["plan"]) if r.get("plan") else {}
        return rows

    def _update(self, eid: int, **f: Any) -> None:
        self.j.db.execute(f"UPDATE entries SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), eid))
        self.j.db.commit()

    def pending(self, group_id: str | None = None) -> list[dict[str, Any]]:
        if group_id:
            return self._rows("status='pending' AND group_id=?", (group_id,))
        return self._rows("status='pending'")

    # ------------------------------------------------------------ cancel / amend
    async def _cancel_entry(self, e: dict, status: str, reason: str) -> str:
        """Relabel the row only when its limit died unfilled; returns withdraw's outcome."""
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
        await t.edit_order(e["order_id"], e["symbol"], "limit", "sell", e["amount"], px)
        self._update(e["id"], limit_price=px, replaced=1, reason=f"amended: {reason}")
        return {"ok": True, "entry_id": entry_id, "new_limit": px}

    # ------------------------------------------------------------ the 5 s loop (guardian)
    async def poll(self) -> list[dict[str, Any]]:
        """Detect fills (protect + journal + cancel siblings), expiries, and cancel conditions. A cancel that doesn't
        kill the order unfilled leaves the row pending: the next pass adopts the fill or retries the cancel."""
        events: list[dict[str, Any]] = []
        now_ms = int(time.time() * 1000)
        for e in self.pending():
            if not self._rows("id=? AND status='pending'", (e["id"],)):
                continue   # a sibling filled earlier in this pass and cancelled it: keep that status and reason
            if e.get("tranche"):
                from .tranches import parent_closed
                o = await self.tr.t.fetch_order(e["order_id"], e["symbol"])
                if parent_closed(self, e) and not float(o.get("filled") or 0):
                    if await self._cancel_entry(e, "cancelled", "tranche trade already closed") == "unfilled":
                        events.append({"entry_id": e["id"], "event": "cancelled", "why": "tranche trade closed"})
                    continue
            try:
                o = await self.tr.t.fetch_order(e["order_id"], e["symbol"])
            except Exception:  # noqa: BLE001
                continue
            filled = float(o.get("filled") or 0)
            if filled > 0:
                events.append(await self._on_fill(e, o))
            elif o.get("status") in DEAD or now_ms > e["expires_ms"]:
                if await self._cancel_entry(e, "expired", f"exchange status {o.get('status')}") == "unfilled":
                    events.append({"entry_id": e["id"], "event": "expired"})
            else:
                why = await self._should_cancel(e, now_ms)
                if why and await self._cancel_by_rule(e, why) == "unfilled":
                    events.append({"entry_id": e["id"], "event": "cancelled", "why": why})
        return events

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
        last = float((await self.tr.ex.public.fetch_ticker(e["symbol"]))["last"])
        if e["cancel_below"] and last <= e["cancel_below"]:
            return f"price {last} fell below {e['cancel_below']} without filling: the move happened without us"
        if e["atr_1h"] and now_ms - e["ref_ms"] <= FAST_APPROACH_WINDOW_MS and last - e["ref_price"] >= e["atr_1h"]:
            return f"price rose {last - e['ref_price']:.6g} (≥ 1 ATR) toward the limit within 15 min: cascade risk"
        if now_ms - e["ref_ms"] > FAST_APPROACH_WINDOW_MS:
            self._update(e["id"], ref_price=last, ref_ms=now_ms)  # rolling 15-min reference
        return None

    async def _on_fill(self, e: dict, o: dict) -> dict[str, Any]:
        tr = self.tr
        sym = e["symbol"]
        if o.get("status") != "closed":  # partial: cancel the rest, protect what we have
            try:
                await tr.t.cancel_order(e["order_id"], sym)
            except Exception:  # noqa: BLE001
                pass
        async with tr.lock:
            pos = await tr._verified_position(sym)
            if not pos:
                self._update(e["id"], status="error", reason="fill reported but no position found")
                return {"entry_id": e["id"], "event": "error"}
            if e["prearm_id"]:
                try:
                    await tr.t.cancel_order(e["prearm_id"], sym, params={"trigger": True})
                except Exception:  # noqa: BLE001
                    pass
            cps = await tr.stop_orders(sym, close_position_only=True)
            if not cps:   # a pre-armed reduce-only stop (another leg / order) is NOT whole-position protection
                await tr._place_stop(sym, e["stop_price"], tries=3)
        if cps and min(c["trigger"] for c in cps) > e["stop_price"] * 1.0001:
            # e.g. the guardian's +10% fallback stop landed first: tighten it to this entry's plan stop
            await tr.move_stop(sym, e["stop_price"], f"entry #{e['id']} filled: plan stop")
        from . import tranches
        tid = tranches.open_trade_id(self, e)   # a split entry's later leg, or a same-symbol sibling that filled too
        if tid:
            return await tranches.join(self, e, pos, tid)
        sib = self._filled_sibling(e)            # a sibling on another symbol filled too: two positions, one budget
        orphan = bool(e.get("tranche")) and tranches.parent_closed(self, e)   # a fresh position nobody asked for
        fill, qty = float(pos["entryPrice"]), float(pos["contracts"])
        tid = self.j.create_trade(sym, e["setup"], e["thesis"], e["stop_price"], e["leverage"], e["plan"],
                                  {"entry_type": "limit", "level_type": e["level_type"], "level_reason": e["level_reason"],
                                   "group_id": e["group_id"], "limit_price": e["limit_price"],
                                   "session_bucket": session_bucket(datetime.now(timezone.utc)),
                                   "breadth_pct": ((cached(self.j, 1800) or {}).get("header") or {}).get("breadth_pct"),
                                   "placed_session": session_bucket(datetime.fromisoformat(e["created_at"]))},
                                  tr.ex.experiment)
        self.j.update_trade(tid, conviction=e.get("conviction"), regime_cell=e.get("regime_cell"))
        self.j.activate_trade(tid, fill, qty, round(fill * qty, 2))
        self._update(e["id"], status="filled", trade_id=tid, filled_at=_now_iso(), fill_price=fill, filled_qty=qty)
        others = await self.cancel_group(e["group_id"], f"sibling #{e['id']} filled", keep=e["id"],
                                         keep_tranche=e.get("tranche"))
        late = "".join([f"; #{i} filled before its cancel (adopted next poll)" for i in others["filled_first"]] +
                       [f"; #{i} cancel failed (retried next poll)" for i in others["cancel_failed"]])
        head = ((f"sibling #{sib} also filled: group over its single-bet budget. " if sib else "")
                + (f"leg filled after its trade closed: new trade #{tid}. " if orphan else ""))
        await tr.alert("action_fill", head + f"limit short filled {qty} @ {fill}, stop {e['stop_price']} placed; "
                       f"{others['cancelled']} sibling order(s) cancelled{late}", sym, bool(head))
        return {"entry_id": e["id"], "event": "filled", "trade_id": tid, "fill": fill, "qty": qty}
