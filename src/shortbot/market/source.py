"""MarketSource: the read surface of an exchange that the scanner, DataHub and research labelling use.

Two classes provide it: exchange.Exchanges (live, ccxt) and backtest.replay_exchange.ReplayExchange (history
pinned to a moment, #29), which is how scan_shorts(ReplayExchange(...)) runs the real scanner on the past. mypy
checks both against this Protocol where they are passed (structural typing: neither names it). Add a member here
when the scan path starts reading a new one, and give ReplayExchange its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ..config import RiskLimits


class MarketSource(Protocol):
    @property
    def public(self) -> Any: ...      # ccxt's market-data client (Exchanges), or the replay's look-alike

    @property
    def settings(self) -> Any: ...    # .db_path: the journal beside which the stream store and the feeds live

    @property
    def limits(self) -> RiskLimits: ...

    async def coin_perps(self) -> dict[str, dict]: ...

    async def market_info(self, symbol: str) -> dict: ...

    async def account_tradable(self, symbol: str) -> bool | None: ...

    async def funding_intervals(self) -> dict[str, dict]: ...

    async def oi_history(self, symbol: str, period: str = "1h", limit: int = 25) -> list[dict]: ...

    async def taker_ratio(self, symbol: str, period: str = "1h", limit: int = 12) -> list[float]: ...

    async def long_short_ratios(self, symbol: str, period: str = "1h") -> dict[str, float | None]: ...

    async def flow_klines(self, symbol: str, interval: str = "5m", limit: int = 288) -> list[list[float]]: ...

    async def premium_klines(self, symbol: str, interval: str = "1h", limit: int = 24) -> list[list[float]]: ...

    async def spot_symbol(self, perp_symbol: str) -> tuple[str | None, float]: ...

    async def spot_flow_klines(self, raw_spot: str, interval: str = "1h", limit: int = 24) -> list[list[float]]: ...
