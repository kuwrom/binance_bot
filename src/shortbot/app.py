"""Shared lazy singletons for the MCP server and CLI."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .exchange import Exchanges
from .journal import Journal
from .notify import Notifier
from .params import ResearchParams
from .trading import Trader


@dataclass
class App:
    settings: Settings
    ex: Exchanges
    journal: Journal
    trader: Trader
    params: ResearchParams | None


_app: App | None = None
FLUSH_S = 30.0   # how long an exiting process waits for its queued journal writes


def get_app(research: bool = True) -> App:
    """research=True: AI analyst (MCP/CLI), risk limits tunable via params.py.
    research=False: the always-on guardian, fixed .env baseline limits only."""
    global _app
    if _app is None:
        s = Settings.from_env()
        from . import audit, write_guard
        write_guard.configure(audit.process_kind(), s)   # #36 step 7: where this process's exchange writes may go
        ex = Exchanges(s)
        j = Journal(s.db_path, s.mode)
        params = ResearchParams(s, j) if research else None
        ex.params = params
        # urgent session alerts (NAKED_POSITION, stop_failed) reach Telegram; the guardian swaps in its own adapter
        _app = App(s, ex, j, Trader(ex, j, alert=Notifier(s, j).as_session_alert), params)
    return _app


async def close_app() -> None:
    global _app
    if _app:
        from .fill_quality import drain
        await drain()   # a one-shot CLI's fill references (#35) finish before the clients close
        await _app.journal.w.drain(FLUSH_S)   # writes queued behind another process's write lock (#79) land first
        await _app.ex.close()
        _app = None
