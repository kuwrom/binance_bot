"""The cancels and edits an intent sends (#86, docs/GATEWAY.md §4.3), recorded against the order each one targets.

Binance gives neither an id of its own (developers.binance.com, USDⓈ-M trade endpoints, checked 2026-10-04):
- Cancel Order, DELETE /fapi/v1/order: "Either orderId or origClientOrderId must be sent"; it answers the order
  (status CANCELED). Cancel Algo Order, DELETE /fapi/v1/algoOrder: "Either algoId or clientAlgoId must be sent"; it
  answers only {algoId, clientAlgoId, code "200", msg "success"}, no status.
- Modify Order, PUT /fapi/v1/order: "Either orderId or origClientOrderId must be sent, and the orderId will prevail
  if both are sent". A price or quantity a filter refuses is "rejected and the order will stay as it is"; the order is
  cancelled when partly filled and the new quantity <= executedQty, or when GTX and the new price would execute.
- Query Order / Query Algo Order (by id): an unfilled CANCELED or EXPIRED order is forgotten 3 days after its creation,
  any order after 90 days; updateTime (ccxt 4.5.84: lastUpdateTimestamp) is when it last changed.
So a cancel or an edit is known only through the order it targets: each is a row here (`ref`, a local key), linked
just before it is sent (intents.change), with what Binance answered; one whose answer was lost stays without an
outcome until the resolver reads that order (`looked_up`, reads only: a change is never sent again). The requests are
unchanged: by the order's id, as before.

Outcomes. A cancel: cancelled (Binance answered it, or the order left the book after it was sent and before any later
cancel of ours), off_book (-2011, or found dead or unknown with nothing tying it to this cancel: either way it no
longer rests), missed (still resting GRACE_S after the send, or it left when a later cancel of ours was sent),
filled / triggered (it traded, or the stop fired, first), rejected (a definite refusal). An edit: edited (at the price
asked), missed, cancelled (Binance cancelled the order), filled, off_book, rejected. FULL: the change holds."""

from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from . import audit, clientid, errors, intents

if TYPE_CHECKING:
    from .journal import Journal

GRACE_S = 90.0     # entry_intents.GRACE_S: longer than any request takes; still resting by then = it never ran
SKEW_S = 2.0       # Binance's clock against ours: closer than this, which of two cancels took the order is unknown
FULL = clientid.CHANGED
DEAD = ("canceled", "expired", "rejected")

SCHEMA = """
CREATE TABLE IF NOT EXISTS intent_changes (
    ref TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES intents (id), op TEXT NOT NULL, symbol TEXT NOT NULL,
    order_id TEXT NOT NULL, trigger INTEGER NOT NULL, want REAL, note TEXT, at TEXT NOT NULL, outcome TEXT
);
CREATE INDEX IF NOT EXISTS intent_changes_intent ON intent_changes (intent_id);
CREATE INDEX IF NOT EXISTS intent_changes_order ON intent_changes (symbol, order_id);
CREATE TRIGGER IF NOT EXISTS intent_changes_no_delete BEFORE DELETE ON intent_changes
    BEGIN SELECT RAISE(ABORT, 'an intent''s change link never changes'); END;
CREATE TRIGGER IF NOT EXISTS intent_changes_settled BEFORE UPDATE ON intent_changes
    WHEN OLD.outcome IS NOT NULL OR NEW.ref IS NOT OLD.ref OR NEW.intent_id IS NOT OLD.intent_id
      OR NEW.op IS NOT OLD.op OR NEW.symbol IS NOT OLD.symbol OR NEW.order_id IS NOT OLD.order_id
      OR NEW.trigger IS NOT OLD.trigger OR NEW.want IS NOT OLD.want OR NEW.note IS NOT OLD.note OR NEW.at IS NOT OLD.at
    BEGIN SELECT RAISE(ABORT, 'an intent''s change link never changes and its outcome is set once'); END;
"""


def new_ref(op: str, order_id: Any) -> str:
    return f"{op[0]}-{order_id}-{secrets.token_hex(4)}"


async def change(j: Journal, op: str, symbol: str, order_id: Any, request: Awaitable[dict[str, Any]],
                 trigger: bool = False, want: float | None = None,
                 note: dict[str, Any] | None = None) -> dict[str, Any]:
    """One cancel (`op` "cancel") or edit ("edit", to price `want`) of order `order_id`, by a Trader primitive: linked
    to the running intent just before it is sent (posted: it never holds up the request), then what Binance answered
    (`answered`, `refused`); one that may have run stays in doubt for the resolver. `note`: what the caller sent it
    for (an entry's status and reason: the fill poll labels the row by it if the answer was lost)."""
    ref = new_ref(op, order_id)
    if (led := intents.ledger()) is not None:
        led.sent = True
        led.changes.setdefault(ref, None)
    if (iid := audit.intent_id()) is not None:
        j.post(link, ref, iid, op, symbol, str(order_id), trigger, want, note, intents.now_ms())
    try:
        o = await request
    except Exception as e:
        if (got := refused(e)) is not None:
            _settle(j, ref, got)
        raise
    _settle(j, ref, answered(op, o))
    return o


def _settle(j: Journal, ref: str, outcome: str) -> None:
    if (led := intents.ledger()) is not None and ref in led.changes:
        led.changes[ref] = outcome
    if audit.intent_id() is not None:
        j.post(record, ref, outcome)


def link(j: Journal, ref: str, intent_id: str, op: str, symbol: str, order_id: str, trigger: bool,
         want: float | None, note: dict[str, Any] | None, at: str) -> None:
    j.db.execute("INSERT INTO intent_changes (ref, intent_id, op, symbol, order_id, trigger, want, note, at)"
                 " VALUES (?,?,?,?,?,?,?,?,?)", (ref, intent_id, op, symbol, str(order_id), int(trigger), want,
                                                 None if note is None else json.dumps(note, sort_keys=True), at))
    j.db.commit()


def record(j: Journal, ref: str, outcome: str) -> bool:
    """A change's outcome, once (False: it had one)."""
    cur = j.db.execute("UPDATE intent_changes SET outcome=? WHERE ref=? AND outcome IS NULL", (outcome, ref))
    j.db.commit()
    return cur.rowcount == 1


def _rows(j: Journal, where: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
    out = j._rows(f"SELECT * FROM intent_changes WHERE {where} ORDER BY at, rowid", args)
    for r in out:
        r["note"] = json.loads(r["note"]) if r["note"] else None
    return out


def of_intent(j: Journal, intent_id: str) -> list[dict[str, Any]]:
    return _rows(j, "intent_id=?", (intent_id,))


def latest(j: Journal, symbol: str, order_id: Any, op: str = "cancel") -> dict[str, Any] | None:
    """The newest change of ours of this order (any intent), or None."""
    got = _rows(j, "symbol=? AND order_id=? AND op=?", (symbol, str(order_id), op))
    return got[-1] if got else None


def steps(j: Journal, intent_id: str) -> list[dict[str, Any]]:
    """An intent's sends and changes in the order they left, each with what it did (None: still in doubt): the summary
    its resolution is told with (resolver.py) and a resubmitted id is answered with (gateway.answer)."""
    acts = [(o["at"], {"step": o["kind"], "ref": o["client_id"], "outcome": o["outcome"]})
            for o in intents.orders(j, intent_id)]
    acts += [(c["at"], {"step": c["op"], "ref": c["order_id"], "outcome": c["outcome"]}) for c in of_intent(j, intent_id)]
    return [a for _, a in sorted(acts, key=lambda a: a[0])]


def later_at(j: Journal, c: dict[str, Any]) -> float | None:
    """When the next cancel of ours of the same order was sent, if one was."""
    row = j.db.execute("SELECT MIN(at) FROM intent_changes WHERE symbol=? AND order_id=? AND op='cancel' AND at > ?",
                       (c["symbol"], c["order_id"], c["at"])).fetchone()
    return _ts(row[0]) if row and row[0] else None


def answered(op: str, o: dict[str, Any] | None) -> str:
    """A change Binance answered: a cancel took the order off the book; an edit moved it (or Binance cancelled it)."""
    if op == "edit":
        return "cancelled" if (o or {}).get("status") in DEAD else "edited"
    return "cancelled"


def refused(e: BaseException) -> str | None:
    """A change that raised: -2011 / -2013 (off the book), a definite refusal, or None while its outcome is unknown."""
    kind = errors.classify(e)
    if kind is errors.Gone:
        return "off_book"
    return None if kind.unknown else "rejected"


def _ts(at: str) -> float:
    return datetime.fromisoformat(at).timestamp()


def looked_up(c: dict[str, Any], o: dict[str, Any] | None, later: float | None, now: float) -> str | None:
    """What change `c` did, from its order as Binance shows it now (`o`; None: unknown to Binance), the time a later
    cancel of ours of the same order was sent (`later`) and now. None: it can't be told yet (it may still land)."""
    if o is None:
        return "off_book"
    st, age = o.get("status"), now - _ts(c["at"])
    if c["op"] == "edit":
        if c["want"] is not None and o.get("price") is not None and abs(float(o["price"]) - c["want"]) < 1e-12:
            return "edited"
        if st == "open":
            return "missed" if age >= GRACE_S else None
        return "filled" if st == "closed" else "cancelled" if st in DEAD else None
    if st == "open":
        return "missed" if age >= GRACE_S else None
    if st == "closed":
        return "triggered" if c["trigger"] else "filled"
    upd = o.get("lastUpdateTimestamp")
    if st != "canceled" or not upd:
        return "off_book"   # expired (its GTD) or rejected: not this cancel; or no time to tell by
    left, sent = float(upd) / 1000, _ts(c["at"])
    if left < sent - SKEW_S:
        return "off_book"   # gone before this cancel was sent
    if later is None or left <= later - SKEW_S:
        return "cancelled"  # it left after this cancel was sent, before any later one of ours
    return "off_book" if later - sent < SKEW_S else "missed"   # the later one took it (sent too close: can't tell)
