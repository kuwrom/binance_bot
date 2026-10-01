"""Split entries (experiment ladder_live_v1, docs/LADDER.md): one plan, one stop, one 1R budget, placed as 2 resting
limit legs (e.g. 50% at the level, 50% at the deeper sweep level). Resting limits fill most often when we are wrong
(adverse selection); splitting halves that damage and improves the average when the deep leg fills.

- sizing: leg i risks share_i x budget, so the loss at the shared stop is <= 1R even if both legs fill
- first leg to fill creates the trade (plan pinned: ladder_live, full_size = both legs, r1_usdt = both risks)
- the other leg stays live (NOT cancelled like an OCO sibling); when it fills it joins the trade as an add
- the other leg is cancelled when the trade closes, or by the normal rules (TTL, cancel_below, fast approach)
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


def pending_legs(journal, symbol: str) -> list[dict[str, Any]]:
    try:
        return journal._rows("SELECT id, order_id, symbol FROM entries WHERE status='pending' AND tranche IS NOT NULL"
                             " AND symbol=?", (symbol,))
    except Exception:  # noqa: BLE001 - no entries table yet
        return []


async def cancel_legs(trader, symbol: str, reason: str) -> int:
    """Called when a trade is closed: resting legs of its split entry must not open a fresh, planless position."""
    n = 0
    for e in pending_legs(trader.j, symbol):
        try:
            await trader.t.cancel_order(e["order_id"], symbol)
        except Exception:  # noqa: BLE001 - already filled/expired: the entry poll handles it
            continue
        trader.j.db.execute("UPDATE entries SET status='cancelled', reason=? WHERE id=?", (reason, e["id"]))
        n += 1
    trader.j.db.commit()
    return n


def open_trade_id(desk, e: dict[str, Any]) -> int | None:
    """The trade an earlier leg of this tranche opened, if it is still open."""
    for r in desk._rows("tranche=? AND status='filled' AND trade_id IS NOT NULL", (e["tranche"],)):
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


async def join(desk, e: dict[str, Any], pos: dict[str, Any], tid: int) -> dict[str, Any]:
    """A later leg filled: it is an add to the open trade (the closePosition stop already covers the whole size)."""
    fill, qty = float(pos["entryPrice"]), float(pos["contracts"])
    peak = max(float((desk.j.get_trade(tid) or {}).get("amount") or 0) + float(e["amount"]), qty)
    desk.j.update_trade(tid, entry_price=fill, amount=peak, notional=round(fill * qty, 2))
    desk._update(e["id"], status="filled", trade_id=tid, filled_at=_now(), fill_price=e["limit_price"],
                 filled_qty=e["amount"])
    desk.j.log_decision("tranche_fill", f"leg {e['leg']} of tranche {e['tranche']} joined trade #{tid}",
                        e["symbol"], snapshot={"trade_id": tid, "avg_entry": fill, "contracts": qty})
    await desk.tr.alert("action_fill", f"tranche leg {e['leg']} filled {e['amount']} @ {e['limit_price']}: position "
                        f"{qty} avg {fill}, same stop", e["symbol"], False)
    return {"entry_id": e["id"], "event": "tranche_joined", "trade_id": tid, "avg": fill, "qty": qty}


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
