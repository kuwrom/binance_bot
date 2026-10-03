"""A journal trade is a position on the exchange (#15): on a one-way USDⓈ-M account a symbol goes flat -> short -> flat,
and a trade is the stretch of the account's trade history from its first entry fill to the flat point after it. The
journal is matched to the exchange by that history, never by the symbol alone, so a stop-out and a re-entry before
the next reconcile are two trades, each with its own exit fill and its own income.

- span() / walk(): sells add, buys reduce, flat ends the position. Never guessed: buying back more than was sold means
  the symbol wasn't flat where the walk began (None). Used for round trips too (entries_fills.py)
- match(): a journal trade's position, found by its own order ids (`by`: "ids"): a limit trade's entry orders
  (entries table), a market trade's journalled entry and exit orders (order_ids.py, #76). It begins at the first fill
  of its own entry orders. A trade journalled before #76 has no market order ids (`by`: "time"): it begins at the first
  position not already over by its opened_at (desk clock, whole seconds), looking SKEW_MS back for a desk clock ahead
  of Binance's, so a re-entry in the second its previous position closed would be matched to that position. It ends at
  the first flat point after the last fill of an order attributed to it (its own close, a split leg or sibling that
  joined it, a fill linked by entries_fills): one row may then span two positions, but no fill is lost or counted twice
- holds(): which position a FUNDING_FEE row belongs to: the one held when it settled (Binance FAQ "Introduction to
  Binance Futures Funding Rates", checked 2026-10-03: only a position open at the funding time pays or receives, and the
  actual funding transaction time deviates from it by up to 15 s)
- history(): the symbol's trades from a start to now, read in 7-day windows; a full page is split in two (which rows a
  busier window answers is undocumented)

Binance USDⓈ-M (developers.binance.com Account-Trade-List, checked 2026-10-02): startTime..endTime at most 7 days
apart, 3 months back, 500 rows by default and 1000 at most, each row with its orderId. ccxt 4.5.84 fetch_my_trades
sends `until` as endTime, and given only a since a week old or more caps endTime at since + 7 d itself
(async_support/binance.py fetch_my_trades): a since-only read of an older trade silently stops after its first week.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from . import order_ids, tranches

WEEK_MS = 7 * 86_400_000
PAGE = 1000
MAX_CALLS = 40          # 13 quiet weeks reach Binance's 3 months back; busy weeks split
SKEW_MS = 60_000        # a desk clock this far ahead of Binance's still finds a market trade's entry fill
SETTLE_MS = 60_000      # income rows that settle with the exit fill (a liquidation's INSURANCE_CLEAR: its exact time
                        # is undocumented); flat, the symbol has no funding to pay
FUNDING_LAG_MS = 16_000  # a funding row's stamp vs the position that paid it: up to 15 s of settlement, + 1 s of stamp


def qty(xs: list[dict]) -> float:
    return sum(float(x["amount"]) for x in xs)


def vwap(xs: list[dict]) -> float:
    return sum(float(x["price"]) * float(x["amount"]) for x in xs) / qty(xs)


def _num(x: Any) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def span(trades: list[dict], start: int, through: int | None = None) -> dict[str, Any] | None:
    """From trades[start] (a sell) to the first flat point after index `through` (default: start). closed_ms is None
    while the history ends short of flat; None if it buys back more than was sold."""
    sells: list[dict] = []
    buys: list[dict] = []
    net = sold = 0.0
    prior = int(trades[start - 1]["timestamp"]) if start > 0 else None   # the symbol's fill before it (holds())
    for i in range(start, len(trades)):
        x = trades[i]
        sell, amount = x["side"] == "sell", float(x["amount"])
        (sells if sell else buys).append(x)
        net += amount if sell else -amount
        sold += amount if sell else 0.0
        tol = 1e-9 * sold
        if net < -tol:
            return None
        if buys and net <= tol and i >= (through or start):
            closed = int(x["timestamp"])
            nxt = int(trades[i + 1]["timestamp"]) if i + 1 < len(trades) else None
            return {**_legs(sells, prior), "exit": vwap(buys), "closed_ms": closed,
                    "until_ms": max(closed, nxt - 1) if nxt else None,   # the next activity on the symbol isn't ours
                    "exit_orders": sorted({str(b["order"]) for b in buys}), "buys": buys}
    return {**_legs(sells, prior), "exit": None, "closed_ms": None, "until_ms": None, "open_qty": net}


def _legs(sells: list[dict], prior: int | None) -> dict[str, Any]:
    return {"sells": sells, "qty": qty(sells), "entry": vwap(sells), "opened_ms": int(sells[0]["timestamp"]),
            "orders": sorted({str(s["order"]) for s in sells}), "prior_ms": prior}


def holds(ep: dict[str, Any], ms: int) -> bool:
    """Did this position pay (or receive) the funding row stamped `ms`? Funding settles on the position held when it
    runs (module doc): the position open at ms (opened at or before it, closed after it); else, when none was (the
    symbol's fill before this position, the last of the previous one, is at or before ms), this one if it opened within
    FUNDING_LAG_MS after ms. Exclusive between neighbours: the previous position claims ms only if it closed after it."""
    o, c, p = int(ep["opened_ms"]), ep.get("closed_ms"), ep.get("prior_ms")
    if o <= ms:
        return c is None or int(c) > ms
    return o - ms < FUNDING_LAG_MS and (p is None or int(p) <= ms)


def walk(trades: list[dict], order_id: Any) -> dict[str, Any] | None:
    """From the order's first fill to the next flat point. None while the history doesn't reach flat, or if it buys back
    more than was sold (the symbol wasn't flat before this fill)."""
    start = next((i for i, x in enumerate(trades) if str(x.get("order")) == str(order_id) and x["side"] == "sell"),
                 None)
    rt = span(trades, start) if start is not None else None
    return rt if rt and rt["closed_ms"] is not None else None


def first_open(trades: list[dict], opened_ms: int) -> int | None:
    """Index of the first sell of the first position not already over before opened_ms (an earlier position's tail is
    skipped)."""
    start, net, sold = None, 0.0, 0.0
    for i, x in enumerate(trades):
        sell = x["side"] == "sell"
        if start is None:
            if not sell:
                continue
            start, net, sold = i, 0.0, 0.0
        net += float(x["amount"]) * (1 if sell else -1)
        sold += float(x["amount"]) if sell else 0.0
        if net <= 1e-9 * sold:
            if int(x["timestamp"]) >= opened_ms:
                return start
            start = None
    return start


async def history(t, symbol: str, since_ms: int, until_ms: int | None = None) -> list[dict[str, Any]]:
    """The symbol's trades from since_ms to until_ms (default: Binance's now), oldest first, read in 7-day windows."""
    stop = until_ms if until_ms is not None else int(time.time() * 1000)
    calls, out, a = [0], {}, since_ms
    while True:
        last = a + WEEK_MS - 1 >= stop
        rows = await _window(t, symbol, a, stop if last else a + WEEK_MS - 1, calls, open_end=last and until_ms is None)
        out.update({(x.get("id"), x["timestamp"]): x for x in rows})
        if last:
            return sorted(out.values(), key=lambda x: (int(x["timestamp"]), _num(x.get("id"))))
        a += WEEK_MS


async def _window(t, symbol: str, a: int, b: int, calls: list[int], open_end: bool = False) -> list[dict]:
    """One window. The last one of an open-ended read sends no endTime (inside ccxt's week, so none is added): Binance
    answers up to ITS now, whatever the desk clock says. A full page is halved."""
    calls[0] += 1
    if calls[0] > MAX_CALLS:
        raise RuntimeError(f"{symbol}: trade history needs more than {MAX_CALLS} reads")
    rows = await t.fetch_my_trades(symbol, since=a, limit=PAGE, params={} if open_end else {"until": b})
    if len(rows) < PAGE:
        return rows
    hi = max([b] + [int(x["timestamp"]) for x in rows])   # an open end may hold fills past the desk's clock
    if hi - a < 1000:
        raise RuntimeError(f"{symbol}: {PAGE}+ fills within one second")
    mid = (a + hi) // 2
    head = await _window(t, symbol, a, mid, calls) + await _window(t, symbol, mid + 1, hi, calls)
    return head + (await _window(t, symbol, hi + 1, hi + 1, calls, True) if open_end else [])


async def match(trader, trade: dict[str, Any], read_until: int | None = None) -> dict[str, Any]:
    """The trade's position from trade history. state: closed (flat: its exit fills exist; until_ms bounds it at the
    next activity on the symbol, else SETTLE_MS past its exit fill), open, no_entry (no entry fill found) or broken (it
    buys back more than was sold); by: how its start was found, "ids" or "time" (module doc). read_until: history is
    read up to there (default: now), for a trade long closed."""
    opened = int(datetime.fromisoformat(trade["opened_at"]).timestamp() * 1000)
    trades = await history(trader.t, trade["symbol"], opened - SKEW_MS, read_until)
    ids = order_ids.read(trader.j, trade["id"])   # read now: the caller's row may predate its close
    own = {str(e["order_id"]) for e in tranches.own_entries(trader.j, trade["id"]) if e.get("order_id")}
    own |= {*ids["entry"], *ids["exit"]}
    mine = [i for i, x in enumerate(trades) if str(x.get("order")) in own]
    start = None
    if ids["entry"] or (trade.get("entry_features") or {}).get("entry_type") == "limit":
        start = next((i for i in mine if trades[i]["side"] == "sell"), None)
    by = "time" if start is None else "ids"
    if start is None:
        start = first_open(trades, opened)
    if start is None:
        return {"state": "no_entry"}
    ep = span(trades, start, max([i for i in mine if i >= start], default=start))
    if ep is None:
        return {"state": "broken"}
    if ep["closed_ms"] is None:
        return {"state": "open", **ep, "by": by}
    return {**ep, "state": "closed", "until_ms": ep["until_ms"] or ep["closed_ms"] + SETTLE_MS, "by": by}
