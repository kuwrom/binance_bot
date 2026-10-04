"""Whom an intent runs for, in a process that runs intents for several (#36 step 6, docs/GATEWAY.md §4.1).

The order gateway runs the sessions' intents under the research parameters and the guardian's under the .env baseline,
in one process with one Trader. A guardian-origin intent runs inside `guardian(alert)`, a context every await inside it
inherits (asyncio copies it into the tasks it starts):
- limits: Exchanges.limits is the desk baseline (.env + the human's desk limits: desk_limits.py) and
  Exchanges.experiment None, so "the AWS guardian always uses the baseline" holds in a shared process;
- alerts: Trader.alert goes to `alert`, the guardian's Notifier.as_trader_alert (its non-urgent alerts reach Telegram
  too, as they do from the guardian; a session's reach the journal only);
- symbol locks: a busy symbol answers SymbolBusy after symlock.GUARDIAN_WAIT_S, as in the guardian: skipped this pass,
  never waited out.
Outside it (every other process, every session intent) nothing changes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

Alert = Callable[[str, str, str | None, bool], Awaitable[Any]]


@dataclass(frozen=True)
class _Origin:
    alert: Alert | None


_CUR: ContextVar[_Origin | None] = ContextVar("shortbot_origin", default=None)


@contextmanager
def guardian(alert: Alert | None) -> Iterator[None]:
    """What runs inside acts for the guardian (module doc). `alert`: where Trader.alert goes (None: the Trader's own)."""
    token = _CUR.set(_Origin(alert))
    try:
        yield
    finally:
        _CUR.reset(token)


def is_guardian() -> bool:
    return _CUR.get() is not None


def alert_sink() -> Alert | None:
    cur = _CUR.get()
    return cur.alert if cur is not None else None
