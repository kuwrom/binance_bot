"""What a close buys back, decided INSIDE the symbol lock against the live position (task #21; STX 2026-10-01: a
session halved the runner seconds after the guardian took target 1, because `fraction` is of the CURRENT position).

- expect_contracts (any close): the size the caller saw. A different live size means something changed since they
  looked: refused, with what changed.
- of_original: `fraction` is the cumulative share of the ORIGINAL (peak) size that should be gone by the end, as plan
  targets count it (manager.plan_actions). Idempotent: a target already taken, by anyone, closes nothing more.
- a partial close of the CURRENT size with no stated view is refused while a size change on this trade is younger
  than RECENT_CHANGE_S (the guardian's target, another session's partial, a fill that joined, an add, a widen cut).
A full close needs no view: flat is flat, whoever got there first.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import ccxt.async_support as ccxt

RECENT_CHANGE_S = 300
SIZE_EVENTS = ("partial_close", "close_incomplete", "tranche_fill", "sibling_fill", "add", "widen_stop",
               "target_fill")   # a resting plan target filled on the exchange (targets.py, #65)


def _g(x: float) -> str:
    return f"{x:g}"


def still_due(fraction: float, orig: float, size: float) -> float:
    """fraction x orig - (orig - size) in decimal: what the plan wants gone by now, minus what is gone (50 - 37.6 is
    12.399999999999999 in binary floats, and the step floor would make it 12.3)."""
    d = Decimal(str(orig))
    return float(Decimal(str(fraction)) * d - (d - Decimal(str(size))))


def floor_amount(t, symbol: str, x: float) -> float:
    """x truncated to the symbol's step size; 0.0 below one step (ccxt 4.5.84 base/exchange.py amount_to_precision
    raises InvalidOrder on a '0' result instead)."""
    if x <= 0:
        return 0.0
    try:
        return float(t.amount_to_precision(symbol, x))
    except ccxt.InvalidOrder:
        return 0.0


def recent_change(j, symbol: str, trade: dict | None, now: datetime | None = None) -> dict[str, Any] | None:
    """The newest size-changing decision on this trade within RECENT_CHANGE_S, if any."""
    since = ((now or datetime.now(timezone.utc)) - timedelta(seconds=RECENT_CHANGE_S)).isoformat(timespec="seconds")
    opened = (trade or {}).get("opened_at") or ""   # a previous trade's exits on this symbol are not this one's
    rows = j._rows(f"SELECT ts, action, reasoning, snapshot FROM decisions WHERE symbol=? AND ts > ? AND ts >= ? AND "
                   f"action IN ({','.join('?' * len(SIZE_EVENTS))}) ORDER BY id DESC LIMIT 1",
                   (symbol, since, opened, *SIZE_EVENTS))
    return rows[0] if rows else None


def _what(ev: dict[str, Any]) -> str:
    amt = (json.loads(ev["snapshot"] or "{}") or {}).get("amount")
    return f"{ev['action']} '{ev['reasoning']}' at {ev['ts']}" + (f" ({_g(amt)} closed)" if amt else "")


def sized(tr, symbol: str, size: float, trade: dict | None, fraction: float, expect: float | None,
          of_original: bool) -> float | dict[str, Any]:
    """The amount to buy back now, or the refusal to return (nothing is sent)."""
    from .trading import _f
    step = _f(tr.t.market(symbol)["precision"]["amount"])
    tol = step / 2 if step else 1e-9
    if expect is not None and abs(size - expect) > tol:
        ev = recent_change(tr.j, symbol, trade)
        return {"ok": False, "position": size, "error": f"position is {_g(size)}, you expected {_g(expect)}: it "
                f"changed since you looked{' (last: ' + _what(ev) + ')' if ev else ''}. Re-read it and decide again"}
    if of_original:
        orig = _f((trade or {}).get("amount"))
        if not orig:
            return {"ok": False, "error": "of_original needs the journal trade's original size; none found"}
        want = still_due(fraction, orig, size)   # what the plan wants gone by now, minus what is already gone
        if want >= size - tol:
            return size
        amount = floor_amount(tr.t, symbol, want)
        if amount <= 0:
            return {"ok": False, "noop": True, "position": size, "error": f"nothing to close: {fraction:.0%} of the "
                    f"original {_g(orig)} is already closed (position {_g(size)})"}
        return amount
    if fraction == 1:
        return size
    if expect is None and (ev := recent_change(tr.j, symbol, trade)):
        head = ("a resting target filled since you looked" if ev["action"] == "target_fill" else
                "target already taken by the guardian since you looked" if ev["action"] == "partial_close"
                and ev["reasoning"].startswith("plan/target") else "the position changed since you looked")
        return {"ok": False, "position": size, "error": f"{head}: {_what(ev)}; position is now {_g(size)}. To close "
                f"{fraction:.0%} of the CURRENT {_g(size)}, pass expect_contracts={_g(size)}; to take a plan share, "
                "pass of_original (fraction of the original size)"}
    amount = min(floor_amount(tr.t, symbol, size * fraction), size)
    if amount <= 0:
        return {"ok": False, "error": "fraction too small for the exchange step size"}
    return amount
