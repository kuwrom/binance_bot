"""Journalling the fills the 5 s entry poll finds (entries_acts.on_fill), and those it learns about late (#8), rebuilt
from the account's trade history.

- a fill with its position open is journalled inside the symbol lock (`adopt`), siblings and alerts come after it
  (`announce`): a session's close never finds the position without its trade (#60)

- every limit trade opens at its first fill (exchange clock), not at the poll: finalize's income query starts at
  opened_at, so a poll-time open dropped the entry commission (#6)
- round trip: a limit that filled and was closed again (its pre-armed stop, a liquidation, a manual close) before the
  poll looked has no position left to adopt. It is rebuilt from userTrades and journalled as a closed trade with
  exchange-truth PnL, fees and funding, so the loss streak, R stats and lessons see it. The orders it folded in are
  recorded (entry_features.round_trip.orders): a row adopted later (entry_intents) links to it, never counts twice
- a fill into a journal trade still 'open' although its position is gone links to that trade: reconcile closes it
  through this fill (a trade ends at the flat point after the last fill attributed to it: episodes.match), once

Binance USDⓈ-M (developers.binance.com, checked 2026-10-01): userTrades rows carry their orderId; startTime..endTime
spans at most 7 days (ccxt caps endTime at since + 7 d on its own), 3 months back (Account-Trade-List). A triggered
algo stop fills under a NEW order whose id is the algo order's actualOrderId, never the algoId (Query-Algo-Order).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from . import fill_quality, reconcile
from .entries_cancel import drop_prearm
from .episodes import qty as _qty
from .episodes import vwap as _vwap
from .episodes import walk  # the one flat-to-flat walk: reconcile matches journal trades with it too
from .market.market_map import cached
from .sessions import session_bucket

if TYPE_CHECKING:
    from .entries import EntryDesk
    from .journal import Journal
    from .trading_orders import Client

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
    at = datetime.fromtimestamp(ms / 1000, UTC) if ms else datetime.now(UTC)
    return at.isoformat(timespec="seconds")


def _num(x: Any) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def own(trades: list[dict[str, Any]], order_id: Any) -> list[dict[str, Any]]:
    return [x for x in trades if str(x.get("order")) == str(order_id)]


def late_note(others: dict[str, Any]) -> str:
    """cancel_group's outcome for an alert: siblings that filled first, or whose cancel failed."""
    return "".join([f"; #{i} filled before its cancel (adopted next poll)" for i in others["filled_first"]] +
                   [f"; #{i} cancel failed (retried next poll)" for i in others["cancel_failed"]])


async def history(t: Client, e: dict[str, Any], o: dict[str, Any]) -> list[dict[str, Any]]:
    """The symbol's trades since this order was created (exchange clock), oldest first, each fill once (a row answered
    twice is one fill: by its id, as episodes.history merges its reads, #46); [] if unreadable. Never older than ccxt's
    7-day window allows (it would silently cut the query to the order's first week)."""
    since = max(int(o.get("timestamp") or 0), int(time.time() * 1000) - WEEK_MS + 60_000)
    try:
        rows = await t.fetch_my_trades(e["symbol"], since=since, limit=1000)
    except Exception as x:  # noqa: BLE001 - the fill path goes on with its fallbacks
        log.warning("trade history %s: %s: %s", e["symbol"], type(x).__name__, x)
        return []
    once = {(x.get("id"), x["timestamp"]): x for x in rows}
    return sorted(once.values(), key=lambda x: (int(x["timestamp"]), _num(x.get("id"))))


def first_fill_ms(trades: list[dict[str, Any]], e: dict[str, Any], o: dict[str, Any]) -> int | None:
    """When the order first filled: its earliest trade, else its last fill (lastTradeTimestamp), else None (now)."""
    mine = own(trades, e["order_id"])
    return int(mine[0]["timestamp"]) if mine else o.get("lastTradeTimestamp")


async def plan_stop(desk: EntryDesk, e: dict[str, Any], cps: list[dict[str, Any]]) -> None:
    """A closePosition stop looser than the plan's (e.g. the guardian's +10% fallback stop landed first) is tightened
    to this entry's plan stop. A widened trade keeps its wider stop (its size was cut for it, #14)."""
    jt = desk.j.active_trade(e["symbol"]) or {}
    stop = max(e["stop_price"], float(jt.get("stop_price") or 0)) if "widened" in (jt.get("notes") or "") \
        else e["stop_price"]
    if cps and min(c["trigger"] for c in cps) > stop * 1.0001:
        await desk.tr.move_stop(e["symbol"], stop, f"entry #{e['id']} filled: plan stop")


def adopt(desk: EntryDesk, e: dict[str, Any], o: dict[str, Any], pos: dict[str, Any],
          trades: list[dict[str, Any]], rest: bool = False) -> tuple[dict[str, Any], tuple[str, bool] | None]:
    """A fill whose position is open, journalled (inside the symbol lock): an add to the open trade of its split entry
    or of a same-symbol sibling (tranches.join), else a new trade. rest: the order's remainder keeps resting, so the
    row stays pending with what filled so far (entries_partial.py). Returns (event, (alert, urgent) or None) for
    announce(), which runs after the lock."""
    from . import entries_partial as partial
    from . import tranches
    at = first_fill_ms(trades, e, o)
    part = partial.claim(o) if rest else None
    tid = tranches.open_trade_id(desk, e)   # a split entry's later leg, or a same-symbol sibling that filled too
    if tid:
        return _referenced(desk, e, o, trades, tranches.join(desk, e, pos, tid, iso(at), part))
    sib = desk._filled_sibling(e)            # a sibling on another symbol filled too: two positions, one budget
    orphan = bool(e.get("tranche")) and tranches.parent_closed(desk, e)   # a fresh position nobody asked for
    fill, qty = float(pos["entryPrice"]), float(pos["contracts"])
    tid = new_trade(desk, e, at)
    f = {"status": "filled", "trade_id": tid, "filled_at": iso(at), "fill_price": fill, "filled_qty": qty, **(part or {})}
    if not (partial.cas_first(desk, e["id"], "pending", **f) if rest else desk._cas_row(e["id"], "pending", **f)):
        desk.j.abort_trade(tid, f"entry #{e['id']} was resolved elsewhere first")
        return {"entry_id": e["id"], "event": "resolved_elsewhere"}, None
    desk.j.activate_trade(tid, fill, qty, round(fill * qty, 2))
    head = ((f"sibling #{sib} also filled: group over its single-bet budget. " if sib else "")
            + (f"leg filled after its trade closed: new trade #{tid}. " if orphan else ""))
    return _referenced(desk, e, o, trades, ({"entry_id": e["id"], "event": "filled", "trade_id": tid, "fill": fill,
                                             "qty": qty},
                       (head + f"limit short filled {qty} @ {fill}, stop {e['stop_price']} placed"
                        + (partial.note(e, o) if rest else ""), bool(head))))


def _referenced(desk: EntryDesk, e: dict[str, Any], o: dict[str, Any], trades: list[dict[str, Any]],
                done: tuple[Any, ...]) -> tuple[Any, ...]:
    """A journalled fill's mainnet reference at its own fills (fill_quality.py, #35): in the background, after the
    journal row, never in the way of the lock or the stop."""
    if done[0].get("trade_id"):
        fill_quality.record_fills(desk.tr, done[0]["trade_id"], "entry", "limit", own(trades, e["order_id"]),
                                  {**o, "id": e["order_id"]})
    return done


async def announce(desk: EntryDesk, e: dict[str, Any], ev: dict[str, Any],
                   alert: tuple[str, bool] | None) -> dict[str, Any]:
    """After the symbol lock: a new trade's OCO siblings are cancelled (they may rest on other symbols, and a task
    holds one symbol lock at a time), then the fill is alerted."""
    if alert is None:
        return ev
    msg, urgent = alert
    if ev["event"] == "filled":
        others = await desk.cancel_group(e["group_id"], f"sibling #{e['id']} filled", keep=e["id"],
                                         keep_tranche=e.get("tranche"))
        msg += f"; {others['cancelled']} sibling order(s) cancelled{late_note(others)}"
    await desk.tr.alert("action_fill", msg, e["symbol"], urgent)
    return ev


def new_trade(desk: EntryDesk, e: dict[str, Any], fill_ms: int | None, **features: Any) -> int:
    """The journal trade an entry fill opens, stamped at the fill (exchange clock), not at the poll, and attributed to
    the experiments and limits the entry was placed (sized) under: the guardian polling the fill has neither (#32)."""
    at = datetime.fromtimestamp(fill_ms / 1000, UTC) if fill_ms else datetime.now(UTC)
    j = desk.j
    over = json.loads(e["overrides"]) if e.get("overrides") else {}   # a veto override at placement (#11)
    if e.get("level_pick"):   # the level picker's choice and every candidate's odds (entry_levels.record)
        over = {**over, "level_pick": json.loads(e["level_pick"])}
    tid = j.create_trade(e["symbol"], e["setup"], e["thesis"], e["stop_price"], e["leverage"], e["plan"],
                         {"entry_type": "limit", "level_type": e["level_type"], "level_reason": e["level_reason"],
                          "group_id": e["group_id"], "limit_price": e["limit_price"],
                          "session_bucket": session_bucket(at),
                          "breadth_pct": ((cached(j, 1800) or {}).get("header") or {}).get("breadth_pct"),
                          "placed_session": session_bucket(datetime.fromisoformat(e["created_at"])),
                          "limits": json.loads(e["limits"]), **over, **features},
                         e.get("experiment"))
    j.update_trade(tid, conviction=e.get("conviction"), regime_cell=e.get("regime_cell"),
                   opened_at=at.isoformat(timespec="seconds"))
    return tid


def _folded(j: Journal, symbol: str, order_id: Any) -> int | None:
    """The round-trip trade this order's fills were already journalled in (it filled with a sibling, before its row
    was pending: a send of unknown outcome that entry_intents adopted later)."""
    rows = j._rows("SELECT id FROM trades WHERE symbol=? AND EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid("
                   "trades.entry_features) THEN trades.entry_features END, '$.round_trip.orders') WHERE value=?)"
                   " ORDER BY id DESC LIMIT 1", (symbol, str(order_id)))
    return rows[0]["id"] if rows else None


async def round_trip(desk: EntryDesk, e: dict[str, Any], o: dict[str, Any],
                     trades: list[dict[str, Any]]) -> dict[str, Any]:
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
    ours = {str(r["order_id"]) for r in desk._rows("symbol=? AND order_id IS NOT NULL AND status NOT LIKE "
                                                   "'external%'", (sym,))}
    foreign = [x for x in rt["orders"] if x not in ours]
    if foreign:   # the position's PnL is exact, but it is not one clean bet: research must be able to tell
        reason += f"; NOT one bet: includes orders that aren't this desk's limit entries ({', '.join(foreign)})"
    tid = await j.write(lambda _j: _journal_round_trip(desk, e, rt, stopped, foreign))
    if tid is None:
        return {"entry_id": e["id"], "event": "resolved_elsewhere"}
    res = None
    try:
        if (trade := j.get_trade(tid)) is None:   # journalled just above
            raise LookupError(f"trade #{tid} is not in the journal")
        res = await reconcile.finalize(desk.tr, trade, reason, episode=rt)   # closed_at = its exit fill
    except Exception as x:  # noqa: BLE001 - the trade stays open with no position: reconcile closes it (60 s)
        log.warning("round trip #%s not finalized: %s: %s", tid, type(x).__name__, x)
    others = await desk.cancel_group(e["group_id"], f"sibling #{e['id']} filled", keep=e["id"],
                                     keep_tranche=e.get("tranche"))
    net = f"net {res['net']:+.2f} USDT" if res else "PnL pending (reconcile closes it within a minute)"
    await desk.tr.alert("action_fill", f"limit short #{e['id']} filled {rt['qty']:g} @ {rt['entry']:.6g} and was "
                        f"closed @ {rt['exit']:.6g} ({reason}): trade #{tid} journalled closed, {net}. "
                        f"{others['cancelled']} sibling order(s) cancelled{late_note(others)}. No position to "
                        "manage; record any lesson with add_learning.",
                        sym, True)
    return {"entry_id": e["id"], "event": "round_trip", "trade_id": tid, "fill": rt["entry"], "qty": rt["qty"],
            "exit": rt["exit"], "net": res["net"] if res else None}


def _journal_round_trip(desk: EntryDesk, e: dict[str, Any], rt: dict[str, Any], stopped: bool,
                        foreign: list[str]) -> int | None:
    """The round trip's trade, one unit: opened, claimed by its entry (None: resolved elsewhere first) and by the
    same-symbol orders that filled into it too (one trade, not two)."""
    j, sym = desk.j, e["symbol"]
    tid = new_trade(desk, e, rt["opened_ms"], round_trip={"orders": rt["orders"], "exit_orders": rt["exit_orders"],
                                                          "exit": "prearm_stop" if stopped else "exchange",
                                                          "foreign_orders": foreign})
    if not _claim(desk, e, own(rt["sells"], e["order_id"]), tid):
        j.abort_trade(tid, f"entry #{e['id']} was resolved elsewhere first")
        return None
    j.activate_trade(tid, rt["entry"], rt["qty"], round(rt["entry"] * rt["qty"], 2))
    j.update_trade(tid, mae_pct=round(max(0.0, (rt["exit"] / rt["entry"] - 1) * 100), 3))   # at least the exit's
    for x in desk.pending():
        if x["symbol"] == sym and str(x["order_id"]) in rt["orders"]:
            _claim(desk, x, own(rt["sells"], x["order_id"]), tid)
    return tid


def _claim(desk: EntryDesk, e: dict[str, Any], mine: list[dict[str, Any]], tid: int,
           o: dict[str, Any] | None = None) -> bool:
    """pending -> filled (compare-and-set), stamped with the order's own fills."""
    o = o or {}
    return desk._cas_row(e["id"], "pending", status="filled", trade_id=tid,
                        filled_at=iso(int(mine[0]["timestamp"]) if mine else o.get("lastTradeTimestamp")),
                        fill_price=_vwap(mine) if mine else float(o.get("average") or e["limit_price"]),
                        filled_qty=_qty(mine) if mine else float(o.get("filled") or 0))


async def _link(desk: EntryDesk, e: dict[str, Any], o: dict[str, Any], trades: list[dict[str, Any]], tid: int,
                urgent: bool, why: str) -> dict[str, Any]:
    if not await desk.j.write(lambda _j: _claim(desk, e, own(trades, e["order_id"]), tid, o)):
        return {"entry_id": e["id"], "event": "resolved_elsewhere"}
    await _drop_prearm(desk, e, [])
    desk.j.log_decision("fill_linked", f"entry #{e['id']} {why}", e["symbol"], snapshot={"trade_id": tid})
    others = await desk.cancel_group(e["group_id"], f"sibling #{e['id']} filled", keep=e["id"],
                                     keep_tranche=e.get("tranche"))
    await desk.tr.alert("action_fill", f"entry #{e['id']} filled {o.get('filled')} and its position is already gone: "
                        f"{why}. {others['cancelled']} sibling order(s) cancelled{late_note(others)}", e["symbol"],
                        urgent)
    return {"entry_id": e["id"], "event": "fill_linked", "trade_id": tid}


async def _unresolved(desk: EntryDesk, e: dict[str, Any], o: dict[str, Any]) -> dict[str, Any]:
    """The fill is certain (the order says so) but history doesn't show the round trip yet: siblings go now, the row
    waits ROUND_TRIP_TRIES polls, then it is an 'error' a human journals."""
    await desk.cancel_group(e["group_id"], f"sibling #{e['id']} filled", keep=e["id"], keep_tranche=e.get("tranche"))
    n = desk._rt_tries[e["id"]] = desk._rt_tries.get(e["id"], 0) + 1
    if n < ROUND_TRIP_TRIES:
        return {"entry_id": e["id"], "event": "fill_unresolved", "tries": n}
    desk._rt_tries.pop(e["id"], None)
    why = f"order {e['order_id']} filled {o.get('filled')}, but no position and no round trip in trade history"
    if await desk._settle(e["id"], "pending", status="error", reason=why):
        await desk.tr.alert("fill_unresolved", f"entry #{e['id']}: {why} after {n} polls. Check Binance and journal "
                            "it by hand.", e["symbol"], True)
    return {"entry_id": e["id"], "event": "error"}


async def _drop_prearm(desk: EntryDesk, e: dict[str, Any], exit_orders: list[str]) -> bool:
    """Whether this entry's pre-armed stop made the exit (its actualOrderId among the exit's orders). One still
    resting is cancelled: with no position it would cut the next short on the symbol. An unresolved one (prearm_cid
    only) is entry_intents' to find and cancel, now that the row is no longer pending."""
    if not e.get("prearm_id"):
        return False
    tr, sym = desk.tr, e["symbol"]
    try:
        a = await tr.t.fetch_order(e["prearm_id"], sym, params={"trigger": True})
    except Exception:  # noqa: BLE001 - unreadable: cancel it anyway, the exit stays unlabelled
        a = {}
    if a.get("status") in (None, "open"):
        async with tr.symbol_lock(sym, f"entry #{e['id']}: drop the pre-arm of a round trip"):
            await drop_prearm(tr, sym, e["prearm_id"], tr.alert, f"entry #{e['id']} filled and closed again")
    return str((a.get("info") or {}).get("actualOrderId") or "") in exit_orders
