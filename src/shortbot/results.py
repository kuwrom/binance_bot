"""Typed intent results (#36 gateway step 3, docs/GATEWAY.md §4.5): what an intent did, exact by what it sent.

A Result is the status, `sent` (False: nothing reached Binance; True: something did; None: maybe), the intent's side,
the body (today's result, unchanged: what the MCP tools, the CLI and the web console return) and, for a raised error,
its type (errors.wrap, #37). The gateway builds one per run from the body or the error and the run's ledger
(intents.Ledger: every primitive records its sends); the intent's row stores it (intents.finish) and gives it back
(`from_row`). Statuses:
- refused: nothing was sent (the kill switch, a precondition, risk, budget, vetoes, stale code, a read that failed,
  a bug before any send). The caller re-reads and decides again.
- failed: something was sent and nothing it changed is left changed: a definite exchange refusal, or a step after a
  send failed and was undone (a stop swap whose new stop was refused, the old one restored).
- partial: part of it is done: a market order part-filled (a close that filled short), or an answer that refuses yet
  says what it did and kept (_kept below).
- unknown: something it sent is still in doubt (a send under a client id, or a cancel or an edit: intent_changes.py),
  whatever the body or the error says (#86), or the body itself answers `unknown`: the resolver settles it from the
  exchange (resolver.py). Never re-sent.
- busy (a lock was held, nothing sent: it runs again under its id), interrupted (cancelled mid-run), done.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import errors
from .symlock import AccountBusy, SymbolBusy

if TYPE_CHECKING:
    from .intents import Ledger


def _kept(body: dict[str, Any]) -> bool:
    """An `ok: false` answer that still did something and kept it: flatten closed some (`closed`), a group cancelled
    some (`cancelled`), a stop swap left no stop (`unprotected`: the old one is gone), a plan step took an action
    (`done`)."""
    closed, n, done = body.get("closed"), body.get("cancelled"), body.get("done")
    return bool(body.get("unprotected") or (isinstance(closed, list) and closed)
                or (isinstance(n, int) and not isinstance(n, bool) and n > 0)
                or (isinstance(done, list) and any(isinstance(d, dict) and isinstance(d.get("result"), dict)
                                                   and d["result"].get("ok") for d in done)))


def declares_doubt(body: Any) -> bool:
    """A body that itself names what it couldn't settle: a send of unknown outcome (`unknown`), a group's orders that
    may still rest (`cancel_failed`), a withdrawal's rows left for the fill poll, or withdraw's own "unknown". The
    resolver judges such an intent by what its doubted sends did; any other body is definite about its goal."""
    if body == "unknown":
        return True
    return isinstance(body, dict) and bool(body.get("unknown") or body.get("cancel_failed")
                                           or body.get("left_for_fill_poll"))


@dataclass(frozen=True)
class Result:
    status: str
    sent: bool | None
    body: Any = None
    error: errors.OrderError | None = None
    side: str = "short"

    @classmethod
    def of(cls, body: Any, led: Ledger, side: str = "short") -> Result:
        """A returned body: its `busy` flag; unknown while anything the run sent is in doubt (or the body says so);
        then its `ok` flag and what the run sent."""
        if isinstance(body, dict) and body.get("ok") is False and body.get("busy"):
            return cls("busy", False, body, side=side)
        if led.doubt or (isinstance(body, dict) and body.get("unknown")):
            return cls("unknown", None, body, side=side)
        if isinstance(body, dict) and body.get("ok") is False:
            if not led.sent:
                return cls("refused", False, body, side=side)
            return cls("partial" if led.has("part_filled") or _kept(body) else "failed", True, body, side=side)
        return cls("partial" if led.has("part_filled") else "done", led.sent, body, side=side)

    @classmethod
    def failure(cls, e: BaseException, led: Ledger, side: str = "short") -> Result:
        """A raised error: busy or cancelled, else by what the run sent: nothing (refused), a send still in doubt
        (unknown), something that ran (partial), or only refusals (failed)."""
        err = errors.wrap(e)
        if not led.sent:
            err.sent = False
        if not isinstance(e, Exception):   # cancelled: the run stopped wherever it was
            return cls("interrupted", None if led.sent else False, None, err, side)
        if isinstance(e, (SymbolBusy, AccountBusy)):
            return cls("busy", False, None, err, side)
        if not led.sent:
            return cls("refused", False, None, err, side)
        if led.doubt:
            return cls("unknown", None, None, err, side)
        return cls("partial" if led.ran() else "failed", True, None, err, side)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Result:
        """An intent's stored result (intents.get)."""
        return cls(row["status"], None if row["sent"] is None else bool(row["sent"]), row["result"],
                   None if row["error"] is None else errors.OrderError.from_dict(row["error"]), row["side"])
