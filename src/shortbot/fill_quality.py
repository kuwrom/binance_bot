"""Demo fills benchmarked against MAINNET prices (#35). Signals and research labels read mainnet (`ex.public`); fills,
fees and PnL come from the demo account, whose book is thinner and different. Every entry and exit fill of a journal
trade gets the mainnet reference at its fill, in `trades.fill_refs`; fill_report.py turns those into bps next to the
backtest's slippage model, and calibrates it.

Never on the order path: `record` schedules a background task and returns; nothing awaits it before an order or a
stop. A capture is bounded by its own timeout (`ex.public` retries 3x with a 20 s timeout of its own). A failure
stores no reference and is counted (`missed`, by order id, with why). `drain` lets a one-shot CLI finish them; a
capture still running when a process exits is cancelled quietly (asyncio.run), nothing is written after that.
- book: a fill the desk sees as it happens (younger than FRESH_MS: its market entry or close) -> mainnet best bid/ask
  (GET /fapi/v1/ticker/bookTicker, weight 2) and mark (GET /fapi/v1/premiumIndex, weight 1), together, TIMEOUT_S.
  ref_ms = the later of the two answers' own stamps (the book's last update; the mark's, in whole seconds).
- trade: a fill learnt about late (a limit the 5 s poll finds, a round trip, a stop-out reconcile finds) -> the last
  mainnet print at or before the fill's exchange time (GET /fapi/v1/aggTrades, weight 20, start..end < 1 h, only the
  past 48 h). Binance keeps no book history: a late fill's reference is a trade price (`at`: the side it printed on).
- with the first entry of a trade: the mainnet 24h quote volume (24 h ticker, weight 1), the backtest's slippage input.
(Binance docs checked 2026-10-02; shapes as answered live that day: tests/fake_engine.py ProdView.)

`trades.fill_refs` (JSON), written only by atomic json_insert / json_set statements, never read-modify-write, so the
guardian and the MCP server can't lose each other's rows (entry_features is rewritten whole by several writers):
  {"v": 1, "vol24_usd": 3.1e7, "missed": {"<order id>": "timeout"},
   "fills": [{leg, type, order, px, qty, ms, src, ref_ms, bid, ask, mark | last, at, trigger?}, ...],
   "orders": {"<order id>": {leg, cid}}}   <- the trade's market order ids, written at the order's answer (order_ids.py)
One fill = one order (the VWAP of its fills, at its first fill), keyed by order id: an order recorded live is never
recorded again by the backstop, whichever process gets there first. Raw quotes, not bps, so the reference rule can
change without re-capturing. leg / type are free strings: entry/market, entry/limit, exit/market (a desk close),
exit/exchange (a stop-out, liquidation or manual close found in trade history), exit/target (a resting plan target,
targets.py: live once its order is final, and labelled so by the backstop too).

Hooks: trading_open._enter (its snap starts at the fill), trading_manage.close / _close_part, entries_fills.adopt
(limit fills, joined legs), reconcile.finalize with an episode (the backstop: every order of the trade's position
without a reference yet: round trips, stop-outs, a live capture that missed, within aggTrades' 48 h).

Known gaps (fill_report's coverage shows them): guardian emergency closes (guardian_positions' squeeze/kill close,
stops.py's close past the stop, ensure_stop past its stop) finalize without an episode, so they get no exit
reference; add_to_winner adds (positioning.py) get none live, only the backstop's if the trade closes on the exchange
within 48 h, labelled with the trade's entry type; a backstop exit that was in fact a desk market close is labelled
`exchange`; the trades journalled before #35 have none (aggTrades keeps 48 h: no backfill).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from .exchange import to_raw

log = logging.getLogger("shortbot.fill_quality")

TIMEOUT_S = 2.0                  # a live book + mark capture
TRADE_TIMEOUT_S = 5.0            # a reconstructed reference (aggTrades pages), off the order path
FRESH_MS = 1_500                 # a fill younger than this is benchmarked against the live book
AGG_WINDOWS_MS = (2_000, 60_000)  # the last print before a fill: a 2 s window, then a minute for a thin coin
AGG_PAGES = 3                    # 1000-row pages read on within one window
HORIZON_MS = 47 * 3_600_000      # aggTrades answers only the past 48 h
DOC = "CASE WHEN json_valid(fill_refs) THEN fill_refs ELSE '{\"v\":1,\"fills\":[],\"missed\":{}}' END"
_NEW = f"NOT EXISTS (SELECT 1 FROM json_each({DOC}, '$.fills') WHERE json_extract(value, '$.order') = ?)"
_PENDING: set[asyncio.Task] = set()


def _num(x: Any) -> float | None:
    try:
        return float(x) if x is not None and float(x) > 0 else None
    except (TypeError, ValueError):
        return None


def _spawn(coro) -> asyncio.Task | None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:   # no running loop: nothing to benchmark with
        coro.close()
        return None
    _PENDING.add(task)     # a strong reference: the loop keeps only weak ones
    task.add_done_callback(_PENDING.discard)
    return task


def pending() -> int:
    """Captures still running (in this loop, when called from one)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    return sum(1 for t in _PENDING if not t.done() and (loop is None or t.get_loop() is loop))


async def drain(timeout: float | None = None) -> None:
    """Wait for this loop's captures, a live one's span by default (a one-shot CLI before it exits; the guardian's
    shutdown is not held up longer). Never raises; what is still running is cancelled with the loop."""
    loop = asyncio.get_running_loop()
    mine = [t for t in _PENDING if t.get_loop() is loop and t is not asyncio.current_task()]
    if mine:
        await asyncio.wait(mine, timeout=TIMEOUT_S + 1 if timeout is None else timeout)


def snap(ex, symbol: str) -> asyncio.Task | None:
    """Start the live mainnet capture now, right after a fill; `record(..., snap=)` attaches it to the trade later."""
    return _spawn(_book(ex, symbol))


def record(tr, trade_id: int | None, leg: str, kind: str, order: dict | str | int | None = None, *,
           px: float | None = None, qty: float | None = None, ms: int | None = None, snap: asyncio.Task | None = None,
           trigger: float | None = None) -> None:
    """Benchmark one fill of journal trade `trade_id` in the background. `order`: the ccxt order (its id, average,
    filled and lastTradeTimestamp fill in what isn't passed) or just its id. Nothing without a trade or an order id.
    Never raises."""
    try:
        o = order if isinstance(order, dict) else {"id": order}
        if not trade_id or o.get("id") in (None, ""):
            return
        f = {"leg": leg, "type": kind, "order": str(o["id"]), "px": _num(o.get("average") if px is None else px),
             "qty": _num(o.get("filled") if qty is None else qty),
             "ms": int(ms or o.get("lastTradeTimestamp") or o.get("timestamp") or 0) or None,
             **({"trigger": float(trigger)} if _num(trigger) else {})}
        _spawn(_job(tr, int(trade_id), [f], snap))
    except Exception as e:  # noqa: BLE001 - research data: never in an order's way
        log.warning("fill reference for trade #%s not scheduled: %s: %s", trade_id, type(e).__name__, e)


def record_fills(tr, trade_id: int | None, leg: str, kind: str, rows: list[dict], order: dict | None = None,
                 trigger: float | None = None) -> None:
    """Fills from trade history (ccxt userTrades rows), one reference per order; none read: `order` as in record().
    Never raises."""
    try:
        fills = _by_order(leg, kind, rows, trigger)
        if not fills:
            record(tr, trade_id, leg, kind, order, trigger=trigger)
        elif trade_id:
            _spawn(_job(tr, int(trade_id), fills, None))
    except Exception as e:  # noqa: BLE001 - research data: never in a fill's way
        log.warning("fill references for trade #%s not scheduled: %s: %s", trade_id, type(e).__name__, e)


def record_episode(tr, trade: dict[str, Any], ep: dict[str, Any], kinds: dict[str, str] | None = None) -> None:
    """The backstop at finalize: every order of the trade's position (episodes.span: sells, buys) not yet referenced.
    Entries are the trade's own entry type; exits found in history are `exchange`, with the trade's stop as trigger,
    unless `kinds` names the order's type ({order id: "target"}: a resting target, #65). Never raises."""
    try:
        feats = trade.get("entry_features") if isinstance(trade.get("entry_features"), dict) else {}
        kind = "limit" if feats.get("entry_type") == "limit" else "market"
        kinds = kinds or {}
        buys = ep.get("buys") or []
        fills = (_by_order("entry", kind, ep.get("sells") or [])
                 + _by_order("exit", "exchange", [b for b in buys if str(b.get("order")) not in kinds],
                             trade.get("stop_price"))
                 + [f for t in sorted(set(kinds.values())) for f in _by_order(
                     "exit", t, [b for b in buys if kinds.get(str(b.get("order"))) == t])])
        if fills:
            _spawn(_job(tr, int(trade["id"]), fills, None))
    except Exception as e:  # noqa: BLE001 - research data: never in a close's way
        log.warning("fill references for trade #%s not scheduled: %s: %s", trade.get("id"), type(e).__name__, e)


def _by_order(leg: str, kind: str, rows: list[dict], trigger: float | None = None) -> list[dict[str, Any]]:
    by: dict[str, list[dict]] = {}
    for x in rows:
        if x.get("order") is not None:
            by.setdefault(str(x["order"]), []).append(x)
    out = []
    for oid, xs in by.items():
        q = sum(float(x["amount"]) for x in xs)
        if q > 0:
            out.append({"leg": leg, "type": kind, "order": oid, "qty": q,
                        "px": sum(float(x["price"]) * float(x["amount"]) for x in xs) / q,
                        "ms": min(int(x["timestamp"]) for x in xs),
                        **({"trigger": float(trigger)} if _num(trigger) else {})})
    return sorted(out, key=lambda f: f["ms"])


async def _job(tr, tid: int, fills: list[dict[str, Any]], snap: asyncio.Task | None) -> None:
    j, vol = tr.j, False
    try:
        row = j.get_trade(tid)
        if not row:
            return
        refs = row.get("fill_refs") if isinstance(row.get("fill_refs"), dict) else {}
        have = {f.get("order") for f in refs.get("fills") or []}
        for f in fills:
            got, snap = snap, None   # a snap belongs to the one fill it was started for
            if f["order"] in have:
                continue
            if not f["px"]:
                ref = {"why": "no_fill_price"}
            else:
                ref = await got if got is not None else await capture(tr.ex, row["symbol"], f["ms"])
            if "why" in ref:
                _miss(j, tid, f["order"], ref["why"])
                continue
            _add(j, tid, {**f, **ref})
            if f["leg"] == "entry" and not vol and not refs.get("vol24_usd"):
                vol = await _vol(tr.ex, j, tid, row["symbol"])
    except Exception as e:  # noqa: BLE001 - research data: logged, never raised into anyone's way
        log.warning("fill reference for trade #%s not recorded: %s: %s", tid, type(e).__name__, e)


async def capture(ex, symbol: str, ms: int | None = None) -> dict[str, Any]:
    """The mainnet reference for a fill at exchange time `ms`: {"src": ..., ...}, or {"why": ...}. Never raises."""
    age = None if ms is None else time.time() * 1000 - ms
    if age is None or age <= FRESH_MS:
        return await _book(ex, symbol)
    if age > HORIZON_MS:
        return {"why": "too_old"}
    return await _print(ex, symbol, int(ms))


async def _book(ex, symbol: str) -> dict[str, Any]:
    raw, pub = to_raw(symbol), ex.public
    try:
        b, m = await asyncio.wait_for(asyncio.gather(pub.fapiPublicGetTickerBookTicker({"symbol": raw}),
                                                     pub.fapiPublicGetPremiumIndex({"symbol": raw}),
                                                     return_exceptions=True), TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"why": "timeout"}
    out: dict[str, Any] = {"src": "book"}
    stamps = []
    if isinstance(b, dict) and _num(b.get("bidPrice")) and _num(b.get("askPrice")):
        out.update(bid=float(b["bidPrice"]), ask=float(b["askPrice"]))
        stamps.append(b.get("time"))
    if isinstance(m, dict) and _num(m.get("markPrice")):
        out["mark"] = float(m["markPrice"])
        stamps.append(m.get("time"))
    if len(out) == 1:
        log.info("mainnet book for %s unreadable: %r / %r", symbol, b, m)
        return {"why": "error"}
    stamps = [int(s) for s in stamps if s]
    return {**out, "ref_ms": max(stamps) if stamps else None}


async def _print(ex, symbol: str, ms: int) -> dict[str, Any]:
    try:
        last = await asyncio.wait_for(_last_print(ex.public, to_raw(symbol), ms), TRADE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"why": "timeout"}
    except Exception as e:  # noqa: BLE001 - counted, not raised
        log.info("mainnet prints for %s unreadable: %s: %s", symbol, type(e).__name__, e)
        return {"why": "error"}
    if last is None:
        return {"why": "no_print"}
    return {"src": "trade", "ref_ms": int(last["T"]), "last": float(last["p"]), "at": "bid" if last.get("m") else "ask"}


async def _last_print(pub, raw: str, ms: int) -> dict | None:
    """The last aggTrade at or before ms (endTime is inclusive; rows come oldest first, 1000 at most)."""
    for w in AGG_WINDOWS_MS:
        start, rows = ms - w, []
        for _ in range(AGG_PAGES):
            page = await pub.fapiPublicGetAggTrades({"symbol": raw, "startTime": start, "endTime": ms, "limit": 1000})
            rows += page
            if len(page) < 1000:
                break
            start = int(page[-1]["T"])
        if rows:
            return max(rows, key=lambda r: (int(r["T"]), int(r.get("a") or 0)))
    return None


async def _vol(ex, j, tid: int, symbol: str) -> bool:
    """The mainnet 24h quote volume, once per trade (json_insert never overwrites it)."""
    try:
        v = _num((await asyncio.wait_for(ex.public.fetch_ticker(symbol), TIMEOUT_S)).get("quoteVolume"))
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - the report falls back to entry_features.quote_volume
        return False
    if v:
        j.db.execute(f"UPDATE trades SET fill_refs = json_insert({DOC}, '$.vol24_usd', ?) WHERE id=?", (v, tid))
        j.db.commit()
    return True


def _add(j, tid: int, fill: dict[str, Any]) -> None:
    j.db.execute(f"UPDATE trades SET fill_refs = json_insert({DOC}, '$.fills[#]', json(?)) WHERE id=? AND {_NEW}",
                 (json.dumps(fill), tid, fill["order"]))
    j.db.commit()


def _miss(j, tid: int, order: str, why: str) -> None:
    log.info("trade #%s: no mainnet reference for order %s (%s)", tid, order, why)
    j.db.execute(f"UPDATE trades SET fill_refs = json_set({DOC}, '$.missed.' || json_quote(?), ?) WHERE id=? AND "
                 f"{_NEW}", (order, why, tid, order))
    j.db.commit()
