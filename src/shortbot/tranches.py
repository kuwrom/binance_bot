"""Split entries (experiment ladder_live_v1, docs/LADDER.md): one plan, one stop, one 1R budget, placed as 2 resting
limit legs (e.g. 50% at the level, 50% at the deeper sweep level). Resting limits fill most often when we are wrong
(adverse selection); splitting halves that damage and improves the average when the deep leg fills.

- sizing: leg i risks share_i x budget, so the loss at the shared stop is <= 1R even if both legs fill
- first leg to fill creates the trade (plan pinned: ladder_live, full_size = both legs, r1_usdt = both risks)
- the other leg stays live (NOT cancelled like an OCO sibling); when it fills it joins the trade as an add
- the other leg is cancelled when the trade closes (only this trade's legs: `cancel_legs`), or by the normal rules
  (TTL, cancel_below, fast approach)
"""

from __future__ import annotations

import json
import uuid
from typing import Any

MAX_LEGS = 2


def expand(p: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """A proposal with `tranches: [{limit_price, share}, ...]` -> one leg proposal per tranche (shares sum <= 1)."""
    legs = p["tranches"]
    if not 1 < len(legs) <= MAX_LEGS:
        raise ValueError(f"a split entry has 2..{MAX_LEGS} legs")
    shares = [float(t.get("share", 1 / len(legs))) for t in legs]
    if sum(shares) > 1 + 1e-9 or min(shares) <= 0:
        raise ValueError("leg shares must be > 0 and sum to <= 1 (they split ONE risk budget)")
    tid = uuid.uuid4().hex[:8]
    base = {k: v for k, v in p.items() if k != "tranches"}
    return tid, [{**base, "limit_price": t["limit_price"], "risk_share": s, "tranche": tid, "leg": i + 1}
                 for i, (t, s) in enumerate(zip(legs, shares))]


def finalize(db, placed: list[dict[str, Any]]) -> dict[str, Any]:
    """Pin the tranche's combined size and risk in every leg's plan, so whichever leg fills first opens a trade that
    already knows its full budget (ladder exposure x = contracts / full_size; 1R = both legs at the stop)."""
    full = round(sum(x["amount"] for x in placed), 8)
    r1 = round(sum(x["risk_usdt"] for x in placed), 4)
    for x in placed:
        (raw,) = db.execute("SELECT plan FROM entries WHERE id=?", (x["entry_id"],)).fetchone()
        plan = {**json.loads(raw or "{}"), "ladder_live": "v1", "full_size": full, "r1_usdt": r1}
        db.execute("UPDATE entries SET plan=? WHERE id=?", (json.dumps(plan), x["entry_id"]))
    db.commit()
    return {"full_size": full, "r1_usdt": r1}


def _entries(journal, where: str, args: tuple) -> list[dict[str, Any]]:
    """This journal's mode only (filtered here, so a table not migrated yet still counts as demo)."""
    try:
        rows = journal._rows(f"SELECT * FROM entries WHERE {where} ORDER BY id", args)
    except Exception:  # noqa: BLE001 - no entries table yet
        return []
    return [r for r in rows if (r.get("mode") or "demo") == journal.mode]


def pending_legs(journal, symbol: str) -> list[dict[str, Any]]:
    return _entries(journal, "status='pending' AND tranche IS NOT NULL AND symbol=?", (symbol,))


def own_entries(journal, trade_id: int) -> list[dict[str, Any]]:
    """The entry rows that filled into this trade (its first fill, later legs, a sibling that joined it)."""
    return _entries(journal, "trade_id=?", (trade_id,))


def own_legs(journal, trade_id: int) -> list[dict[str, Any]]:
    """Resting legs of the split entry that opened this trade. A pending leg has no trade_id until it fills: its
    tranche ties it to the trade. Another group's legs on the same symbol are not this trade's."""
    return _entries(journal, "status='pending' AND tranche IN (SELECT tranche FROM entries WHERE trade_id=? AND "
                             "tranche IS NOT NULL)", (trade_id,))


async def cancel_legs(trader, trade_id: int, reason: str) -> dict[str, list[dict[str, Any]]]:
    """Called when a trade is closed: ITS resting legs must not open a fresh, planless position. Returns the legs by
    withdraw's outcome: unfilled (cancelled), filled (dead, but (partly) filled first: the entry poll adopts it) and
    unknown (the cancel failed: the limit may still rest, so its pre-armed stop must stay)."""
    from .entries import withdraw
    got: dict[str, list[dict[str, Any]]] = {"unfilled": [], "filled": [], "unknown": []}
    for e in own_legs(trader.j, trade_id):
        async with trader.symbol_lock(e["symbol"], f"trade #{trade_id} closed: withdraw its legs"):
            got[await withdraw(trader.t, e, trader.alert)].append(e)
    for e in got["unfilled"]:   # written after the awaits, one commit each: no write transaction spans an exchange call
        trader.j.db.execute("UPDATE entries SET status='cancelled', reason=? WHERE id=? AND status='pending'",
                            (reason, e["id"]))
        trader.j.db.commit()
    return got


def open_trade_id(desk, e: dict[str, Any]) -> int | None:
    """The open trade this fill adds to: one an earlier leg of its split entry opened, or one a same-symbol OCO
    sibling opened (both filled before the cancel landed: one position, one stop, one journal row)."""
    for r in desk._rows("group_id=? AND symbol=? AND status='filled' AND trade_id IS NOT NULL",
                        (e["group_id"], e["symbol"])):
        t = desk.j.get_trade(r["trade_id"])
        if t and t.get("status") == "open":
            return r["trade_id"]
    return None


def parent_closed(desk, e: dict[str, Any]) -> bool:
    """True when a leg of this tranche already filled and its trade is closed: the remaining leg must die."""
    for r in desk._rows("tranche=? AND status='filled' AND trade_id IS NOT NULL", (e["tranche"],)):
        t = desk.j.get_trade(r["trade_id"])
        if t and t.get("status") == "closed":
            return True
    return False


def join(desk, e: dict[str, Any], pos: dict[str, Any], tid: int,
         filled_at: str | None = None) -> tuple[dict[str, Any], tuple[str, bool] | None]:
    """A later fill on this group's open trade is an add (the closePosition stop already covers the whole size): a
    split entry's next leg, or an OCO sibling whose cancel came too late (then the group is over its budget).
    filled_at: when it filled on the exchange (entries_fills.first_fill_ms), not when the poll saw it. Journal only
    (inside the symbol lock, entries._on_fill): returns (event, (alert, urgent) or None), alerted after the lock."""
    fill, qty = float(pos["entryPrice"]), float(pos["contracts"])
    # amount = peak size; a fill that landed before the first one was adopted is already in it (count it once)
    peak = max(float((desk.j.get_trade(tid) or {}).get("amount") or 0), qty)
    owners = desk._rows("trade_id=?", (tid,))
    leg = e.get("tranche") and any(r["tranche"] == e["tranche"] for r in owners)
    if not desk._settle(e["id"], "pending", status="filled", trade_id=tid, filled_at=filled_at or _now(),
                        fill_price=e["limit_price"], filled_qty=e["amount"]):
        return {"entry_id": e["id"], "event": "resolved_elsewhere"}, None
    desk.j.update_trade(tid, entry_price=fill, amount=peak, notional=round(fill * qty, 2))
    if not leg:
        desk.j.log_decision("sibling_fill", f"entry #{e['id']} filled before its cancel landed: joined trade #{tid}",
                            e["symbol"], snapshot={"trade_id": tid, "avg_entry": fill, "contracts": qty})
        return ({"entry_id": e["id"], "event": "sibling_joined", "trade_id": tid, "avg": fill, "qty": qty},
                (f"sibling #{owners[0]['id']} also filled: group over its single-bet budget. Entry #{e['id']} filled "
                 f"{e['amount']} @ {e['limit_price']}: position {qty} avg {fill}, one stop", True))
    desk.j.log_decision("tranche_fill", f"leg {e['leg']} of tranche {e['tranche']} joined trade #{tid}",
                        e["symbol"], snapshot={"trade_id": tid, "avg_entry": fill, "contracts": qty})
    return ({"entry_id": e["id"], "event": "tranche_joined", "trade_id": tid, "avg": fill, "qty": qty},
            (f"tranche leg {e['leg']} filled {e['amount']} @ {e['limit_price']}: position {qty} avg {fill}, same stop",
             False))


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
