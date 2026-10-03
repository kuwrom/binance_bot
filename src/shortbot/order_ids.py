"""The exchange order ids of a trade's own market orders, with their client ids (#9, #76): journalled at the order's
answer, so the trade's position is found in trade history by id (episodes.match) and its income split by its own fills
(reconcile.trade_income), never by time alone.

Kept in `trades.fill_refs`, #35's per-order JSON doc of the trade, under "orders":
  {"orders": {"<order id>": {"leg": "entry" | "exit", "cid": "sb-o12-1f2e3d4c"}}, "fills": [...], ...}
written only by json_set on the stored doc, one UPDATE each, never read-modify-write: the guardian and the MCP server
can't lose each other's ids (or fill_quality's references). Not the doc's `fills`, although those are keyed by order
id too: a fill is written in the background once its mainnet capture answers (a capture that fails leaves only
`missed`, a process with no event loop leaves nothing), it carries no client id, and the backstop's come from the
episode matched at finalize, i.e. from the very time-based matching these ids replace.

Written by Trader's market primitives (trading_orders.py) for the symbol's journal trade (one per symbol, #12): every
market entry and add (leg entry), every market close, full or partial, the desk's or the guardian's (leg exit). A send
whose outcome stays unknown, or a close that found the position flat, has no order id: nothing is written. Limit entries
keep theirs in the entries table (tranches.own_entries), resting targets in the targets table. Trades journalled
before #76 have none: they are matched by time, as before.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .fill_quality import DOC

log = logging.getLogger("shortbot.order_ids")
LEGS = ("entry", "exit")


def note(j, symbol: str, leg: str, order: Any) -> None:
    """Journal one market order (a ccxt order: its id and clientOrderId) on the symbol's journal trade. Nothing without
    an order id or a trade. Never raises: the order has run, its id is research data."""
    try:
        oid = order.get("id") if isinstance(order, dict) else None
        if oid in (None, "") or leg not in LEGS:
            return
        trade = j.active_trade(symbol)
        if not trade:
            return
        j.db.execute(f"UPDATE trades SET fill_refs = json_set({DOC}, '$.orders.' || json_quote(?), json(?)) WHERE id=?",
                     (str(oid), json.dumps({"leg": leg, "cid": order.get("clientOrderId")}), trade["id"]))
        j.db.commit()
    except Exception as e:  # noqa: BLE001 - never in an order's way: the trade then matches by time
        log.warning("%s order id of %s not journalled: %s: %s", leg, symbol, type(e).__name__, e)


def read(j, trade_id: int) -> dict[str, dict[str, str | None]]:
    """{"entry": {order id: client id}, "exit": {...}} as journalled now (empty for a trade from before #76)."""
    out: dict[str, dict[str, str | None]] = {leg: {} for leg in LEGS}
    rows = j._rows("SELECT CASE WHEN json_valid(fill_refs) THEN json_extract(fill_refs, '$.orders') END AS o "
                   "FROM trades WHERE id=?", (trade_id,))
    doc = json.loads(rows[0]["o"]) if rows and rows[0]["o"] else {}
    for oid, o in (doc.items() if isinstance(doc, dict) else []):
        if isinstance(o, dict) and o.get("leg") in LEGS:
            out[o["leg"]][str(oid)] = o.get("cid")
    return out
