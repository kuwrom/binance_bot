"""Fills the 5 s entry poll learns about late (#8), rebuilt from the account's trade history (used by entries.py).

- every limit trade opens at its first fill (exchange clock), not at the poll: finalize's income query starts at
  opened_at, so a poll-time open dropped the entry commission (#6)
- round trip: a limit that filled and was closed again (its pre-armed stop, a liquidation, a manual close) before the
  poll looked has no position left to adopt. It is rebuilt from userTrades and journalled as a closed trade with
  exchange-truth PnL, fees and funding, so the loss streak, R stats and lessons see it. The orders it folded in are
  recorded (entry_features.round_trip.orders): a row adopted later (entry_intents) links to it, never counts twice
- a fill into a journal trade still 'open' although its position is gone links to that trade: reconcile closes it
  with this fill's income in it (split between two trades, the unbounded finalize would count it twice: #15)

Binance USDⓈ-M (developers.binance.com, checked 2026-10-01): userTrades rows carry their orderId; startTime..endTime
spans at most 7 days (ccxt caps endTime at since + 7 d on its own), 3 months back (Account-Trade-List). A triggered
algo stop fills under a NEW order whose id is the algo order's actualOrderId, never the algoId (Query-Algo-Order).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from . import reconcile
from .market.market_map import cached
from .sessions import session_bucket

log = logging.getLogger("shortbot.entries")
WEEK_MS = 7 * 86_400_000
ROUND_TRIP_TRIES = 6   # polls (~30 s) for trade history to show a round trip before the row becomes an 'error'


class StopNotPlaced(Exception):
    """A fill whose whole-position stop could not be placed: its pre-armed stop is kept, the row stays pending and
    the next poll retries (make before break)."""

    def __init__(self, e: dict[str, Any], cause: BaseException):
        super().__init__(f"entry #{e['id']} filled but its whole-position stop {e['stop_price']} could not be placed "
                         f"({type(cause).__name__}: {cause}); its pre-armed stop is kept, retried every poll")


def iso(ms: int | None) -> str:
    at = datetime.fromtimestamp(ms / 1000, timezone.utc) if ms else datetime.now(timezone.utc)
    return at.isoformat(timespec="seconds")


def _num(x: Any) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def _qty(xs: list[dict]) -> float:
    return sum(float(x["amount"]) for x in xs)


def _vwap(xs: list[dict]) -> float:
    return sum(float(x["price"]) * float(x["amount"]) for x in xs) / _qty(xs)


def own(trades: list[dict], order_id: Any) -> list[dict]:
    return [x for x in trades if str(x.get("order")) == str(order_id)]


def late_note(others: dict[str, Any]) -> str:
    """cancel_group's outcome for an alert: siblings that filled first, or whose cancel failed."""
    return "".join([f"; #{i} filled before its cancel (adopted next poll)" for i in others["filled_first"]] +
                   [f"; #{i} cancel failed (retried next poll)" for i in others["cancel_failed"]])


async def history(t, e: dict[str, Any], o: dict[str, Any]) -> list[dict[str, Any]]:
    """The symbol's trades since this order was created (exchange clock), oldest first; [] if unreadable. Never
    older than ccxt's 7-day window allows (it would silently cut the query to the order's first week)."""
    since = max(int(o.get("timestamp") or 0), int(time.time() * 1000) - WEEK_MS + 60_000)
    try:
        rows = await t.fetch_my_trades(e["symbol"], since=since, limit=1000)
    except Exception as x:  # noqa: BLE001 - the fill path goes on with its fallbacks
        log.warning("trade history %s: %s: %s", e["symbol"], type(x).__name__, x)
        return []
    return sorted(rows, key=lambda x: (int(x["timestamp"]), _num(x.get("id"))))


def first_fill_ms(trades: list[dict], e: dict[str, Any], o: dict[str, Any]) -> int | None:
    """When the order first filled: its earliest trade, else its last fill (lastTradeTimestamp), else None (now)."""
    mine = own(trades, e["order_id"])
    return int(mine[0]["timestamp"]) if mine else o.get("lastTradeTimestamp")


def walk(trades: list[dict], order_id: Any) -> dict[str, Any] | None:
    """From the order's first fill to the next flat point (sells add, buys reduce). None while the history doesn't
    reach flat, or if it buys back more than was sold (the symbol wasn't flat before this fill: never guessed)."""
    start = next((i for i, x in enumerate(trades) if str(x.get("order")) == str(order_id) and x["side"] == "sell"),
                 None)
    if start is None:
        return None
    sells: list[dict] = []
    buys: list[dict] = []
    net = 0.0
    for i in range(start, len(trades)):
        x = trades[i]
        (sells if x["side"] == "sell" else buys).append(x)
        net += float(x["amount"]) * (1 if x["side"] == "sell" else -1)
        tol = 1e-9 * _qty(sells)
        if net < -tol:
            return None
        if buys and net <= tol:
            closed = int(x["timestamp"])
            nxt = int(trades[i + 1]["timestamp"]) if i + 1 < len(trades) else None
            return {"sells": sells, "qty": _qty(sells), "entry": _vwap(sells), "exit": _vwap(buys),
                    "opened_ms": int(sells[0]["timestamp"]), "closed_ms": closed,
                    "until_ms": max(closed, nxt - 1) if nxt else None,   # the next activity on the symbol isn't ours
                    "orders": sorted({str(s["order"]) for s in sells}),
                    "exit_orders": sorted({str(b["order"]) for b in buys})}
    return None


def new_trade(desk, e: dict[str, Any], fill_ms: int | None, **features: Any) -> int:
    """The journal trade an entry fill opens, stamped at the fill (exchange clock), not at the poll."""
    at = datetime.fromtimestamp(fill_ms / 1000, timezone.utc) if fill_ms else datetime.now(timezone.utc)
    j = desk.j
    tid = j.create_trade(e["symbol"], e["setup"], e["thesis"], e["stop_price"], e["leverage"], e["plan"],
                         {"entry_type": "limit", "level_type": e["level_type"], "level_reason": e["level_reason"],
                          "group_id": e["group_id"], "limit_price": e["limit_price"],
                          "session_bucket": session_bucket(at),
                          "breadth_pct": ((cached(j, 1800) or {}).get("header") or {}).get("breadth_pct"),
                          "placed_session": session_bucket(datetime.fromisoformat(e["created_at"])), **features},
                         desk.tr.ex.experiment)
    j.update_trade(tid, conviction=e.get("conviction"), regime_cell=e.get("regime_cell"),
                   opened_at=at.isoformat(timespec="seconds"))
    return tid


def _folded(j, symbol: str, order_id: Any) -> int | None:
    """The round-trip trade this order's fills were already journalled in (it filled with a sibling, before its row
    was pending: a send of unknown outcome that entry_intents adopted later)."""
    rows = j._rows("SELECT id FROM trades WHERE symbol=? AND EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid("
                   "trades.entry_features) THEN trades.entry_features END, '$.round_trip.orders') WHERE value=?)"
                   " ORDER BY id DESC LIMIT 1", (symbol, str(order_id)))
    return rows[0]["id"] if rows else None


async def round_trip(desk, e: dict[str, Any], o: dict[str, Any], trades: list[dict]) -> dict[str, Any]:
    """A fill with no position behind it: rebuild what happened from trade history and journal it (module doc)."""
    j, sym = desk.j, e["symbol"]
    active = j.active_trade(sym)
    if active and active["status"] == "open":
        return await _link(desk, e, o, trades, active["id"], True, f"filled into trade #{active['id']}, whose "
                           "position closed on the exchange before the poll: reconcile closes it with this fill in it")
    prior = _folded(j, sym, e["order_id"])
    if prior:
        return await _link(desk, e, o, trades, prior, False, f"already journalled in round trip trade #{prior}")
    rt = walk(trades, e["order_id"])
    if rt is None:
        return await _unresolved(desk, e, o)
    desk._rt_tries.pop(e["id"], None)
    stopped = await _drop_prearm(desk, e, rt["exit_orders"])
    reason = (f"pre-armed stop {e['stop_price']} filled before the poll" if stopped
              else "closed on the exchange before the poll (liquidation / manual / ADL)")
    tid = new_trade(desk, e, rt["opened_ms"], round_trip={"orders": rt["orders"], "exit_orders": rt["exit_orders"],
                                                          "exit": "prearm_stop" if stopped else "exchange"})
    if not _claim(desk, e, own(rt["sells"], e["order_id"]), tid):
        j.abort_trade(tid, f"entry #{e['id']} was resolved elsewhere first")
        return {"entry_id": e["id"], "event": "resolved_elsewhere"}
    j.activate_trade(tid, rt["entry"], rt["qty"], round(rt["entry"] * rt["qty"], 2))
    j.update_trade(tid, mae_pct=round(max(0.0, (rt["exit"] / rt["entry"] - 1) * 100), 3))   # at least the exit's
    for x in desk.pending():   # same-symbol orders that filled into this round trip too: one trade, not two
        if x["symbol"] == sym and str(x["order_id"]) in rt["orders"]:
            _claim(desk, x, own(rt["sells"], x["order_id"]), tid)
    res = None
    try:
        res = await reconcile.finalize(desk.tr, j.get_trade(tid), reason, until_ms=rt["until_ms"])
        j.update_trade(tid, closed_at=iso(rt["closed_ms"]))
    except Exception as x:  # noqa: BLE001 - the trade stays open with no position: reconcile closes it (60 s)
        log.warning("round trip #%s not finalized: %s: %s", tid, type(x).__name__, x)
    others = await desk.cancel_group(e["group_id"], f"sibling #{e['id']} filled", keep=e["id"],
                                     keep_tranche=e.get("tranche"))
    net = f"net {res['net']:+.2f} USDT" if res else "PnL pending (reconcile closes it within a minute)"
    await desk.tr.alert("action_fill", f"limit short #{e['id']} filled {rt['qty']:g} @ {rt['entry']:.6g} and was "
                        f"closed @ {rt['exit']:.6g} ({reason}): trade #{tid} journalled closed, {net}. "
                        f"{others['cancelled']} sibling order(s) cancelled{late_note(others)}. Add the lesson.",
                        sym, True)
    return {"entry_id": e["id"], "event": "round_trip", "trade_id": tid, "fill": rt["entry"], "qty": rt["qty"],
            "exit": rt["exit"], "net": res["net"] if res else None}


def _claim(desk, e: dict[str, Any], mine: list[dict], tid: int, o: dict | None = None) -> bool:
    """pending -> filled (compare-and-set), stamped with the order's own fills."""
    o = o or {}
    return desk._settle(e["id"], "pending", status="filled", trade_id=tid,
                        filled_at=iso(int(mine[0]["timestamp"]) if mine else o.get("lastTradeTimestamp")),
                        fill_price=_vwap(mine) if mine else float(o.get("average") or e["limit_price"]),
                        filled_qty=_qty(mine) if mine else float(o.get("filled") or 0))


async def _link(desk, e: dict[str, Any], o: dict[str, Any], trades: list[dict], tid: int, urgent: bool,
                why: str) -> dict[str, Any]:
    if not _claim(desk, e, own(trades, e["order_id"]), tid, o):
        return {"entry_id": e["id"], "event": "resolved_elsewhere"}
    await _drop_prearm(desk, e, [])
    desk.j.log_decision("fill_linked", f"entry #{e['id']} {why}", e["symbol"], snapshot={"trade_id": tid})
    others = await desk.cancel_group(e["group_id"], f"sibling #{e['id']} filled", keep=e["id"],
                                     keep_tranche=e.get("tranche"))
    await desk.tr.alert("action_fill", f"entry #{e['id']} filled {o.get('filled')} and its position is already gone: "
                        f"{why}. {others['cancelled']} sibling order(s) cancelled{late_note(others)}", e["symbol"],
                        urgent)
    return {"entry_id": e["id"], "event": "fill_linked", "trade_id": tid}


async def _unresolved(desk, e: dict[str, Any], o: dict[str, Any]) -> dict[str, Any]:
    """The fill is certain (the order says so) but history doesn't show the round trip yet: siblings go now, the row
    waits ROUND_TRIP_TRIES polls, then it is an 'error' a human journals."""
    await desk.cancel_group(e["group_id"], f"sibling #{e['id']} filled", keep=e["id"], keep_tranche=e.get("tranche"))
    n = desk._rt_tries[e["id"]] = desk._rt_tries.get(e["id"], 0) + 1
    if n < ROUND_TRIP_TRIES:
        return {"entry_id": e["id"], "event": "fill_unresolved", "tries": n}
    desk._rt_tries.pop(e["id"], None)
    why = f"order {e['order_id']} filled {o.get('filled')}, but no position and no round trip in trade history"
    if desk._settle(e["id"], "pending", status="error", reason=why):
        await desk.tr.alert("fill_unresolved", f"entry #{e['id']}: {why} after {n} polls. Check Binance and journal "
                            "it by hand.", e["symbol"], True)
    return {"entry_id": e["id"], "event": "error"}


async def _drop_prearm(desk, e: dict[str, Any], exit_orders: list[str]) -> bool:
    """Whether this entry's pre-armed stop made the exit (its actualOrderId among the exit's orders). One still
    resting is cancelled: with no position it would cut the next short on the symbol. An unresolved one (prearm_cid
    only) is entry_intents' to find and cancel, now that the row is no longer pending."""
    if not e.get("prearm_id"):
        return False
    t, sym = desk.tr.t, e["symbol"]
    try:
        a = await t.fetch_order(e["prearm_id"], sym, params={"trigger": True})
    except Exception:  # noqa: BLE001 - unreadable: cancel it anyway, the exit stays unlabelled
        a = {}
    if a.get("status") in (None, "open"):
        async with desk.tr.lock:
            try:
                await t.cancel_order(e["prearm_id"], sym, params={"trigger": True})
            except Exception:  # noqa: BLE001 - best effort, as in withdraw
                pass
    return str((a.get("info") or {}).get("actualOrderId") or "") in exit_orders
