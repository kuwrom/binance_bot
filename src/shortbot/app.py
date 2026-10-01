"""Shared lazy singletons for the MCP server and CLI."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .exchange import Exchanges
from .journal import Journal
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


def get_app(research: bool = True) -> App:
    """research=True: AI analyst (MCP/CLI), risk limits tunable via params.py.
    research=False: the always-on guardian, fixed .env baseline limits only."""
    global _app
    if _app is None:
        s = Settings.from_env()
        ex = Exchanges(s)
        j = Journal(s.db_path)
        params = ResearchParams(s, j) if research else None
        ex.params = params
        _app = App(s, ex, j, Trader(ex, j), params)
    return _app


async def close_app() -> None:
    global _app
    if _app:
        await _app.ex.close()
        _app = None
