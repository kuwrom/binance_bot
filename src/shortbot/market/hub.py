"""DataHub: the single place market data is fetched. Signals never call the exchange directly; they ask a
per-symbol MarketContext, which fetches lazily, memoises per scan, and records what failed (so vetoes can
fail closed). Adding a new data source = adding one getter here; every signal can then use it.

    hub = DataHub(ex)
    ctx = hub.context("SUI/USDT:USDT", ticker)      # cheap; nothing fetched yet
    c1h = await ctx.candles("1h", 60)                 # fetched once, cached on the context
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..exchange import Exchanges, to_raw, to_symbol

_FUNDING_TTL = 60


class DataHub:
    def __init__(self, ex: Exchanges, concurrency: int = 6):
        self.ex = ex
        self.sem = asyncio.Semaphore(concurrency)
        self._funding: tuple[float, dict, dict] | None = None
        self._funding_lock = asyncio.Lock()
        self.extensions: dict[str, Any] = {}   # optional feeds keyed by name
        try:  # guardian-recorded feeds (liquidations, social snapshots) live in the shared journal DB
            from .liquidations import LiquidationStore
            from .social import SocialStore
            self.extensions["liquidations"] = LiquidationStore(ex.settings.db_path)
            self.extensions["social"] = SocialStore(ex.settings.db_path)
            from .events import EventStore
            self.extensions["events"] = EventStore(ex.settings.db_path)
            from .calendar import MacroCalendar
            self.extensions["calendar"] = MacroCalendar(ex.settings.db_path)
            from .market_map import MapCache
            self.extensions["market_map"] = MapCache(ex.settings.db_path)
            from .supply import KVReader
            self.extensions["kv"] = KVReader(ex.settings.db_path)
        except Exception:  # noqa: BLE001 - tests / no DB: signals simply have no data
            pass

    def context(self, symbol: str, ticker: dict | None = None) -> "MarketContext":
        return MarketContext(self, to_symbol(symbol), ticker)

    async def funding_tables(self) -> tuple[dict, dict]:
        """(funding rates by ccxt symbol, funding intervals by raw symbol), shared by all contexts, cached 60 s."""
        async with self._funding_lock:
            if self._funding and time.time() - self._funding[0] < _FUNDING_TTL:
                return self._funding[1], self._funding[2]
            rates, intervals = await asyncio.gather(
                self.ex.public.fetch_funding_rates(), self.ex.funding_intervals(), return_exceptions=True)
            if isinstance(rates, BaseException):
                raise rates
            self._funding = (time.time(), rates, intervals if isinstance(intervals, dict) else {})
            return self._funding[1], self._funding[2]


class MarketContext:
    """Everything known about one symbol during one evaluation. Getters return None/[] on failure and add the
    input name to `missing`."""

    def __init__(self, hub: DataHub, symbol: str, ticker: dict | None):
        self.hub, self.ex, self.symbol = hub, hub.ex, symbol
        self._ticker = ticker
        self._memo: dict[tuple, Any] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}
        self.missing: set[str] = set()
        self.features: dict[str, Any] = {}   # merged first-wave features, visible to guard signals

    @property
    def base(self) -> str:
        return self.symbol.split("/")[0]

    async def _get(self, key: tuple, name: str, fetch: Callable[[], Awaitable[Any]], empty: Any = None) -> Any:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key not in self._memo:
                try:
                    async with self.hub.sem:
                        self._memo[key] = await fetch()
                except Exception:  # noqa: BLE001 - recorded; vetoes decide whether it matters
                    self._memo[key] = empty
                    self.missing.add(name)
                if not self._memo[key] and empty is not None:
                    self.missing.add(name)
            return self._memo[key]

    # ---------------------------------------------------------------- core feeds
    async def ticker(self) -> dict:
        if self._ticker is None:
            self._ticker = await self._get(("ticker",), "ticker", lambda: self.ex.public.fetch_ticker(self.symbol), {})
        return self._ticker or {}

    async def last(self) -> float:
        t = await self.ticker()
        if t.get("last"):
            return float(t["last"])
        c = await self.candles("1h", 60)
        return float(c[-1][4]) if c else 0.0

    async def candles(self, tf: str = "1h", limit: int = 60) -> list[list[float]]:
        return await self._get(("candles", tf, limit), f"candles_{tf}",
                               lambda: self.ex.public.fetch_ohlcv(self.symbol, tf, limit=limit), [])

    async def order_book(self, limit: int = 500) -> dict:
        """Perp order book (Binance limits 5/10/20/50/100/500/1000; 500 covers +-1% on alts). Not a critical input."""
        return await self._get(("order_book", limit), "order_book",
                               lambda: self.ex.public.fetch_order_book(self.symbol, limit=limit), {})

    async def oi_history(self, period: str = "1h", limit: int = 25) -> list[dict]:
        name = "open_interest" if period == "1h" else f"open_interest_{period}"
        return await self._get(("oi", period, limit), name, lambda: self.ex.oi_history(self.symbol, period, limit), [])

    async def taker_ratio(self, period: str = "1h", limit: int = 6) -> list[float]:
        name = "taker_ratio" if period == "1h" else f"taker_ratio_{period}"
        return await self._get(("taker", period, limit), name, lambda: self.ex.taker_ratio(self.symbol, period, limit), [])

    async def funding(self) -> dict[str, Any]:
        """{rate, interval_h, mark, next_ts}: rate is per funding interval."""
        async def fetch():
            rates, intervals = await self.hub.funding_tables()
            fr = rates.get(self.symbol, {})
            return {"rate": fr.get("fundingRate"), "mark": fr.get("markPrice"),
                    "next": fr.get("fundingDatetime"),
                    "interval_h": intervals.get(to_raw(self.symbol), {}).get("interval_h", 8)}
        f = await self._get(("funding",), "funding", fetch, {})
        if f and f.get("rate") is None:
            self.missing.add("funding")
        return f or {}

    async def funding_history(self, limit: int = 10) -> list[dict]:
        return await self._get(("fhist", limit), "funding_history",
                               lambda: self.ex.public.fetch_funding_rate_history(self.symbol, limit=limit), [])

    async def market_info(self) -> dict:
        return await self._get(("info",), "market_info", lambda: self.ex.market_info(self.symbol), {})

    async def account_tradable(self) -> bool | None:
        return await self._get(("acct",), "account_tradable", lambda: self.ex.account_tradable(self.symbol))

    async def long_short(self) -> dict:
        return await self._get(("ls",), "long_short", lambda: self.ex.long_short_ratios(self.symbol), {})

    async def news(self) -> dict:
        from .. import news as _news
        """Raw (cleaned) news for signals; whatever reaches the agent is wrapped (news.for_agent, signal features)."""
        return await self._get(("news",), "news", lambda: _news.news_for(self.base), {})

    async def listing_news(self) -> list[dict]:
        """New-listing announcements naming this coin (news.listing_announcements, one cached fetch per 6h for
        every coin). Futures titles name the contract ('XYZUSDT'), so that counts as a mention too."""
        from .. import news as _news

        async def fetch():
            raw = f"{_news.normalize_base(self.base)}USDT"
            return [a for a in await _news.listing_announcements()
                    if _news.mentions(a["title"], self.base) or raw in a["title"]]
        return await self._get(("listing_news",), "listing_news", fetch, None) or []

    # ---------------------------------------------------------------- tape feeds
    async def flow_candles(self, tf: str = "5m", limit: int = 288) -> list[list[float]]:
        """[ts, o, h, l, c, vol, taker_buy_vol] - CVD / taker imbalance / sweeps with volume."""
        return await self._get(("flow", tf, limit), f"flow_{tf}", lambda: self.ex.flow_klines(self.symbol, tf, limit), [])

    async def mark_candles(self, tf: str = "5m", limit: int = 96) -> list[list[float]]:
        """MARK-price klines (index-anchored; what our stops trigger on): [ts, o, h, l, c, 0]."""
        return await self._get(("mark", tf, limit), f"mark_{tf}", lambda: self.ex.public.fetch_ohlcv(
            self.symbol, tf, limit=limit, params={"price": "mark"}), [])

    async def premium(self, tf: str = "1h", limit: int = 24) -> list[list[float]]:
        """Premium index klines (perp vs index): close = premium fraction."""
        return await self._get(("prem", tf, limit), "premium", lambda: self.ex.premium_klines(self.symbol, tf, limit), [])

    async def spot_flow(self, tf: str = "1h", limit: int = 24) -> dict[str, Any]:
        """{'raw': spot symbol or None, 'mult': price multiplier, 'candles': [...]} - no spot pair is not an error."""
        async def fetch():
            raw, mult = await self.ex.spot_symbol(self.symbol)
            candles = await self.ex.spot_flow_klines(raw, tf, limit) if raw else []
            return {"raw": raw, "mult": mult, "candles": candles}
        return await self._get(("spot", tf, limit), "spot", fetch, None) or {"raw": None, "mult": 1.0, "candles": []}

    async def liquidations(self, since_ms: int) -> list[dict]:
        """Forced orders recorded by the guardian's stream recorder (empty if the recorder isn't running)."""
        store = self.hub.extensions.get("liquidations")
        if store is None:
            return []
        return await self._get(("liq", since_ms), "liquidations", lambda: store.recent(self.symbol, since_ms), [])

    async def social(self) -> dict[str, Any]:
        """Attention snapshot: global feeds (cached single calls) + this coin's recorded Square history."""
        async def fetch():
            from . import social as soc
            base = self.base.removeprefix("1000")
            out: dict[str, Any] = {}
            try:
                out["cg_rank"] = (await soc.coingecko_trending()).get(base)
            except Exception:  # noqa: BLE001
                pass
            try:
                hy = (await soc.binance_hype()).get(base) or {}
                out.update({"hype": hy.get("hype"), "sentiment": hy.get("sentiment"),
                            "wash_trading_tag": hy.get("wash_trading_tag"), "hype_summary": hy.get("summary")})
            except Exception:  # noqa: BLE001
                pass
            store = self.hub.extensions.get("social")
            if store is not None:
                since = int(time.time() * 1000) - 7 * 86_400_000
                out["square_posts_series"] = store.series(base, "posts_total", since)
                p7 = store.series(base, "posts_7d", since)
                out["square_posts_7d"] = p7[-1][1] if p7 else None
            return out
        return await self._get(("social",), "social", fetch, None) or {}

    async def unlocks(self) -> list[dict]:
        """Recorded unlock events for this coin (past 7 days through the next 60)."""
        store = self.hub.extensions.get("events")
        if store is None:
            return []
        return await self._get(("unlocks",), "unlocks", lambda: _as_async(store.upcoming(self.base)), [])

    async def macro(self) -> list[dict]:
        """Scheduled macro events from 2h ago to 48h ahead (market/calendar.py), with minutes_to."""
        cal = self.hub.extensions.get("calendar")
        if cal is None:
            return []
        return await self._get(("macro",), "macro", lambda: _as_async(cal.around()), [])


    async def market_map(self) -> dict | None:
        """The guardian's latest market map (<= 30 min old) or None: sectors, breadth, residual moves."""
        cache = self.hub.extensions.get("market_map")
        if cache is None:
            return None
        return await self._get(("market_map",), "market_map", lambda: _as_async(cache.get()), None)


    async def tags(self) -> list[str]:
        """Binance product tags for this coin (weekly cache from market/sectors.py), e.g. Monitoring, Seed, AI."""
        from .sectors import base_of
        kv = self.hub.extensions.get("kv")
        cache = (kv.get("sector_tags") if kv else None) or {}
        return (cache.get("tags") or {}).get(base_of(self.symbol), [])

    async def supply(self) -> dict | None:
        """CoinGecko supply facts (weekly cache, market/supply.py) or None."""
        from .sectors import base_of
        kv = self.hub.extensions.get("kv")
        cache = (kv.get("coin_supply") if kv else None) or {}
        return (cache.get("coins") or {}).get(base_of(self.symbol))


async def _as_async(value):
    return value
