"""Resting plan targets (#65, docs/PROPOSALS.md P1a): a trade's automatic targets rest on Binance as reduce-only GTC BUY
LIMIT orders at the target prices, the execution the backtests credit (backtest/rungs.py, learning #42). This module:
the book a trade should have (pure), the `target_orders` journal (write-ahead, #13) and the journalling of each fill.
The exchange side (place, cancel, settle, sweep) is targets_sync.py.

Sizing: fractions are of the ORIGINAL (peak) size and cumulative, as manager.plan_actions counts them. By target i
(nearest first) the plan wants need_i = cum_i x orig - (orig - cur) gone, floored to the step; the target rests
need_i - need_(i-1), and the one completing the plan (cum >= 1) everything left. Manual partials and adds count, the
book never exceeds the position (no reduce-only competition among our own orders), and nothing is ever carved out of a
target (learning #42: never rungs out of automatic targets). Not resting: targets already hit, targets the mark has
crossed (the guardian's market fallback owns those, as before #65) and plans whose targets only alert.

Binance (sources in tests/fake_reduce.py and clientid.py): GTC, not post-only (GTX), so a target placed when the book
is already through it takes at once at or better than its price instead of being rejected; plain LIMITs count toward
the 200 open orders of a symbol, not the 10 conditional ones (stop, bridges, pre-arms)."""

from __future__ import annotations

import json
import re
import time
from decimal import Decimal
from typing import Any, Callable

from . import fill_quality
from .close_intent import still_due
from .journal import now_iso

PREFIX = "sb-t"                                  # clientid.new("target", trade_id): sb-t<trade id>-<nonce>
LIVE = ("placing", "resting", "unknown")         # may be on the book
MAX_RESTING = 10                                 # nearest targets that rest at once (a plan rarely has more than 3)
SCHEMA = """
CREATE TABLE IF NOT EXISTS target_orders (
    id INTEGER PRIMARY KEY, trade_id INTEGER NOT NULL, symbol TEXT NOT NULL, target REAL NOT NULL, price REAL NOT NULL,
    qty REAL NOT NULL, client_id TEXT NOT NULL UNIQUE, order_id TEXT, status TEXT NOT NULL,
    filled REAL NOT NULL DEFAULT 0, avg REAL, fill_ms INTEGER, created_at TEXT NOT NULL, updated_at TEXT, why TEXT,
    history TEXT NOT NULL DEFAULT '[]', mode TEXT NOT NULL DEFAULT 'demo'
);
"""
# history: every status change [{at, status, why, ex}] (ex: the exchange's own status when it decided)


def is_target(cid: Any) -> bool:
    return str(cid or "").startswith(PREFIX)


def trade_ref(cid: Any) -> int | None:
    m = re.match(rf"^{PREFIX}(\d+)-", str(cid or ""))
    return int(m.group(1)) if m else None


def desired(trade: dict[str, Any], pos: dict[str, Any], floor: Callable[[float], float]) -> list[dict[str, Any]]:
    """[{target, qty, cum}] the trade's resting targets should be, nearest first (module doc). `pos`: {mark,
    contracts}; `floor`: an amount floored to the symbol's step (0.0 below one step)."""
    plan = trade.get("plan") or {}
    cur = float(pos.get("contracts") or 0)
    if not (plan.get("auto") or {}).get("targets") or cur <= 0:
        return []
    hit = set(trade.get("targets_hit") or [])
    orig = max(float(trade.get("amount") or 0), cur)
    out, cum, prev = [], 0.0, Decimal(0)
    for t in sorted(plan.get("targets") or [], key=lambda t: -float(t["price"])):
        cum = min(1.0, cum + float(t["fraction"]))
        need = Decimal(str(cur if cum >= 1.0 - 1e-9 else min(cur, floor(max(0.0, still_due(cum, orig, cur))))))
        qty, prev = need - prev, max(prev, need)
        if qty > 0 and t["price"] not in hit and float(pos["mark"]) > float(t["price"]):
            out.append({"target": float(t["price"]), "qty": float(qty), "cum": cum})
    return out[:MAX_RESTING]


# ------------------------------------------------------------ the journal
def ensure(j) -> None:
    if not getattr(j, "_targets_ready", False):   # once per Journal (connection)
        j.db.executescript(SCHEMA)
        j._targets_ready = True


def rows(j, where: str = "1", args: tuple = ()) -> list[dict[str, Any]]:
    ensure(j)
    return j._rows(f"SELECT * FROM target_orders WHERE ({where}) AND mode=? ORDER BY id", (*args, j.mode))


def live(j, symbol: str | None = None) -> list[dict[str, Any]]:
    q = f"status IN ({','.join('?' * len(LIVE))})"
    return rows(j, q + (" AND symbol=?" if symbol else ""), (*LIVE, *((symbol,) if symbol else ())))


async def add(j, trade_id: int, symbol: str, target: float, price: float, qty: float, cid: str) -> int:
    """The write-ahead row: journalled BEFORE the order is sent (a unit its caller awaits: the loop never waits)."""
    ensure(j)
    return await j.write(_add, trade_id, symbol, target, price, qty, cid)


def _add(j, trade_id: int, symbol: str, target: float, price: float, qty: float, cid: str) -> int:
    hist = json.dumps([{"at": now_iso(), "status": "placing", "why": f"{qty:g} @ {price:g}"}])
    cur = j.db.execute("INSERT INTO target_orders (trade_id, symbol, target, price, qty, client_id, status, created_at,"
                       " history, mode) VALUES (?,?,?,?,?,?,'placing',?,?,?)",
                       (trade_id, symbol, target, price, qty, cid, now_iso(), hist, j.mode))
    return int(cur.lastrowid)


async def move(j, row: dict[str, Any], status: str, why: str, ex: str | None = None, **fields: Any) -> bool:
    """Compare-and-set from the row's current status (another process may have moved it); the change is appended to
    its history. Returns whether this call moved it."""
    return await j.write(_move, row, status, why, ex, fields)


def _move(j, row: dict[str, Any], status: str, why: str, ex: str | None, fields: dict[str, Any]) -> bool:
    sets = ", ".join(f"{k}=?" for k in fields)
    entry = json.dumps({"at": now_iso(), "status": status, "why": why, **({"ex": ex} if ex else {})})
    cur = j.db.execute(f"UPDATE target_orders SET status=?, why=?, updated_at=?, {sets + ', ' if sets else ''}"
                       "history=json_insert(history, '$[#]', json(?)) WHERE id=? AND status=?",
                       (status, why, now_iso(), *fields.values(), entry, row["id"], row["status"]))
    if cur.rowcount:
        row.update(status=status, why=why, **fields)
    return bool(cur.rowcount)


async def note_fill(tr, row: dict[str, Any], o: dict[str, Any], final: bool) -> float:
    """Journal what order `o` (ccxt) of target row `row` filled since the row last saw it: the row (filled, avg,
    fill_ms), a `target_fill` decision (a size event, close_intent.SIZE_EVENTS), the mainnet reference once the order
    is final (fill_quality, one per order), and targets_hit once the target is complete. Returns the new quantity."""
    j, filled = tr.j, float(o.get("filled") or 0)
    new = round(filled - float(row.get("filled") or 0), 12)
    info = o.get("info") or {}
    ms = int(o.get("lastTradeTimestamp") or info.get("updateTime") or 0) or int(time.time() * 1000)
    avg = float(o.get("average") or 0) or float(row.get("avg") or 0) or row["price"]
    if new > 0 and not await j.write(_filled, row, o, filled, avg, ms, new):
        new = 0.0
    if final and filled > 0:
        fill_quality.record(tr, row["trade_id"], "exit", "target", {**o, "average": avg, "lastTradeTimestamp": ms})
    if final and filled >= float(row["qty"]) - 1e-12:
        await mark_hit(j, row["trade_id"], row["target"])
    return max(new, 0.0)


def _filled(j, row: dict[str, Any], o: dict[str, Any], filled: float, avg: float, ms: int, new: float) -> bool:
    cur = j.db.execute("UPDATE target_orders SET filled=?, avg=?, fill_ms=?, updated_at=? WHERE id=? AND filled=?",
                       (filled, avg, ms, now_iso(), row["id"], row.get("filled") or 0))
    if not cur.rowcount:   # another process saw it first: journalled once
        return False
    row.update(filled=filled, avg=avg, fill_ms=ms)
    j.log_decision("target_fill", f"target {row['target']:g}: {new:g} filled", row["symbol"], snapshot={
        "trade_id": row["trade_id"], "target": row["target"], "order": str(o.get("id")),
        "client_id": row["client_id"], "amount": new, "filled": filled, "qty": row["qty"], "px": avg,
        "ms": ms, "status": o.get("status"), "slip_bps": round((avg - row["target"]) / row["target"] * 1e4, 2)})
    return True


async def mark_hit(j, trade_id: int, price: float) -> None:
    """Add a target to the trade's targets_hit, re-read first (one unit): several writers never drop each other's."""
    await j.write(_mark_hit, trade_id, price)


def _mark_hit(j, trade_id: int, price: float) -> None:
    t = j.get_trade(trade_id)
    if t and price not in (t.get("targets_hit") or []):
        j.update_trade(t["id"], targets_hit=[*(t.get("targets_hit") or []), price])


def view(j, trade_id: int | None) -> list[dict[str, Any]]:
    """The trade's targets on (or maybe on) the book, as the journal last saw them."""
    if not trade_id:
        return []
    return [{k: r[k] for k in ("target", "qty", "filled", "status")} for r in live(j) if r["trade_id"] == trade_id]


def order_kinds(j, trade_id: int) -> dict[str, str]:
    """{order id: "target"} for the trade's target orders (fill_quality labels their fills)."""
    return {str(r["order_id"]): "target" for r in rows(j, "trade_id=? AND order_id IS NOT NULL", (trade_id,))}


def exit_label(j, trade_id: int, ep: dict[str, Any]) -> str | None:
    """The close reason when the trade's last exit fill (episodes: `buys`) was one of its resting targets."""
    last = max(ep.get("buys") or [], key=lambda b: int(b.get("timestamp") or 0), default=None)
    if last is not None and str(last.get("order")) in order_kinds(j, trade_id):
        return "plan targets: the last resting target filled on the exchange"
    return None
