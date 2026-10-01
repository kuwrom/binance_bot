"""Two ccxt clients: `public` (prod market data, no keys) and `trade` (demo or live, keyed).

Demo trading has no fapiData endpoints, so all analytics always read from prod public data.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import ccxt.async_support as ccxt

from .config import Settings

NO_DELIVERY = 4133404800000  # Binance's placeholder deliveryDate for perpetuals
MARKETS_TTL = 3600


def to_symbol(s: str) -> str:
    """Accept 'SOL', 'SOLUSDT', 'SOL/USDT' or 'SOL/USDT:USDT' and return the ccxt perp symbol."""
    s = s.upper().strip()
    if ":" in s:
        return s
    if "/" in s:
        return f"{s}:USDT"
    base = s[:-4] if s.endswith("USDT") else s
    return f"{base}/USDT:USDT"


def to_raw(symbol: str) -> str:
    """'SOL/USDT:USDT' -> 'SOLUSDT' for raw fapi endpoints."""
    return to_symbol(symbol).split(":")[0].replace("/", "")


class Exchanges:
    def __init__(self, settings: Settings):
        self.settings = settings
        # market data only: retry transient network failures (never on the trade client: no duplicate orders)
        self.public = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 20000,
                                        "options": {"maxRetriesOnFailure": 3, "maxRetriesOnFailureDelay": 1000}})
        self.trade: ccxt.binanceusdm | None = None
        if settings.api_key and settings.api_secret:
            self.trade = ccxt.binanceusdm({
                "apiKey": settings.api_key, "secret": settings.api_secret,
                "enableRateLimit": True, "timeout": 20000,
                # clock drift (e.g. after the Mac sleeps) causes -1021; wider window + resync_time() on error
                "options": {"adjustForTimeDifference": True, "recvWindow": 10000},
            })
            if not settings.is_live:
                self.trade.enable_demo_trading(True)
        self._spot: ccxt.binance | None = None       # spot market data (lazy), for perp-vs-spot signals
        self._spot_symbols: set[str] | None = None
        self._markets_at = 0.0
        self._lock = asyncio.Lock()
        self._trade_lock = asyncio.Lock()
        self.params = None  # ResearchParams when running as the AI analyst; None = fixed baseline

    @property
    def limits(self):
        """Effective risk limits: AI research overrides if enabled, else the fixed .env baseline."""
        return self.params.effective() if self.params else self.settings.limits

    @property
    def experiment(self) -> str | None:
        return self.params.experiment() if self.params and self.params.active else None

    async def close(self) -> None:
        await self.public.close()
        if self.trade:
            await self.trade.close()
        if self._spot:
            await self._spot.close()

    def require_trade(self) -> ccxt.binanceusdm:
        if not self.trade:
            prefix = "LIVE" if self.settings.is_live else "DEMO"
            raise RuntimeError(f"No trading keys: set BINANCE_{prefix}_API_KEY / _SECRET in .env")
        return self.trade

    async def markets(self) -> dict[str, Any]:
        """Public (prod) markets for analytics. A failed refresh keeps the cached copy."""
        async with self._lock:
            if time.time() - self._markets_at > MARKETS_TTL:
                try:
                    await self.public.load_markets(reload=True)
                    self._markets_at = time.time()
                except Exception:
                    if not self.public.markets:
                        raise
        return self.public.markets

    async def resync_time(self) -> None:
        """Re-measure the offset to Binance server time (fixes -1021 'outside of the recvWindow')."""
        if self.trade:
            await self.trade.load_time_difference()

    async def account_tradable(self, symbol: str) -> bool | None:
        """True/False if trading keys are configured, None when scanning without an account."""
        if not self.trade:
            return None
        try:
            await self.trade_markets()
        except Exception:
            return None
        m = self.trade.markets.get(to_symbol(symbol))
        return bool(m and m.get("active", True))

    async def trade_markets(self) -> None:
        """Only what order placement needs (the trading client's own markets). Exits must never
        depend on the prod analytics metadata; retried because exchangeInfo is large and can time out."""
        t = self.require_trade()
        if t.markets:
            return
        async with self._trade_lock:  # one load even when 25 scan tasks ask at once
            if t.markets:
                return
            await self._load_trade_markets(t)

    async def _load_trade_markets(self, t) -> None:
        for i in range(3):
            try:
                await t.load_markets()
                return
            except Exception:
                if i == 2:
                    raise
                await asyncio.sleep(2)

    async def coin_perps(self) -> dict[str, dict]:
        """Crypto USDT perpetuals (no TradFi/stock perps, quarterlies, settling) that are also listed on the
        connected account: with demo keys the universe is Binance DEMO futures (fewer perps than live).
        Analytics still come from live public data; demo doesn't publish OI/ratio statistics."""
        mk = await self.markets()
        perps = {
            s: m for s, m in mk.items()
            if m.get("linear") and m.get("quote") == "USDT"
            and m["info"].get("contractType") == "PERPETUAL"
            and m["info"].get("underlyingType") == "COIN"
            and m["info"].get("status") == "TRADING"
        }
        if self.trade:
            await self.trade_markets()  # raises if the account's market list can't load: don't guess
            perps = {s: m for s, m in perps.items() if (self.trade.markets.get(s) or {}).get("active", False)}
        return perps

    async def market_info(self, symbol: str) -> dict:
        mk = await self.markets()
        symbol = to_symbol(symbol)
        if symbol not in mk:
            raise ValueError(f"{symbol} is not a Binance USDⓈ-M market")
        info = mk[symbol]["info"]
        onboard = int(info.get("onboardDate") or 0)
        delivery = int(info.get("deliveryDate") or NO_DELIVERY)
        return {
            "symbol": symbol,
            "listing_age_days": round((time.time() * 1000 - onboard) / 86_400_000, 1) if onboard else None,
            "delisting_scheduled": delivery != NO_DELIVERY,
            "delivery_date_ms": delivery if delivery != NO_DELIVERY else None,
            "tags": info.get("underlyingSubType") or [],
            "min_notional": mk[symbol]["limits"]["cost"]["min"],
        }

    # raw public analytics (prod only) ---------------------------------------------------------
    async def oi_history(self, symbol: str, period: str = "1h", limit: int = 25) -> list[dict]:
        rows = await self.public.fapiDataGetOpenInterestHist(
            {"symbol": to_raw(symbol), "period": period, "limit": limit})
        return [{"ts": int(r["timestamp"]), "oi": float(r["sumOpenInterest"]),
                 "oi_value": float(r["sumOpenInterestValue"])} for r in rows]

    async def taker_ratio(self, symbol: str, period: str = "1h", limit: int = 12) -> list[float]:
        rows = await self.public.fapiDataGetTakerlongshortRatio(
            {"symbol": to_raw(symbol), "period": period, "limit": limit})
        return [float(r["buySellRatio"]) for r in rows]

    async def long_short_ratios(self, symbol: str, period: str = "1h") -> dict[str, float | None]:
        args = {"symbol": to_raw(symbol), "period": period, "limit": 1}
        top, glob = await asyncio.gather(
            self.public.fapiDataGetTopLongShortPositionRatio(args),
            self.public.fapiDataGetGlobalLongShortAccountRatio(args),
            return_exceptions=True)
        pick = lambda r: float(r[-1]["longShortRatio"]) if isinstance(r, list) and r else None  # noqa: E731
        return {"top_trader_position_ls": pick(top), "global_account_ls": pick(glob)}

    async def funding_intervals(self) -> dict[str, dict]:
        """Symbols with non-default funding interval or adjusted caps, keyed by raw symbol."""
        rows = await self.public.fapiPublicGetFundingInfo()
        return {r["symbol"]: {"interval_h": int(r.get("fundingIntervalHours") or 8),
                              "cap": float(r.get("adjustedFundingRateCap") or 0)} for r in rows}

    # tape data ---------------------------------------------------------------------------------
    async def flow_klines(self, symbol: str, interval: str = "5m", limit: int = 288) -> list[list[float]]:
        """Perp klines WITH taker-buy volume: [ts, o, h, l, c, vol, taker_buy_vol]. CVD/imbalance without aggTrades."""
        rows = await self.public.fapiPublicGetKlines({"symbol": to_raw(symbol), "interval": interval,
                                                      "limit": min(limit, 1500)})
        return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[9])]
                for r in rows]

    async def premium_klines(self, symbol: str, interval: str = "1h", limit: int = 24) -> list[list[float]]:
        """Premium index (perp vs index) klines: [ts, o, h, l, c]; close = premium as a fraction."""
        rows = await self.public.fapiPublicGetPremiumIndexKlines({"symbol": to_raw(symbol), "interval": interval,
                                                                  "limit": limit})
        return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])] for r in rows]

    @property
    def spot(self) -> ccxt.binance:
        if self._spot is None:
            self._spot = ccxt.binance({"enableRateLimit": True, "timeout": 20000,
                                       "options": {"maxRetriesOnFailure": 3, "maxRetriesOnFailureDelay": 1000}})
        return self._spot

    async def spot_symbol(self, perp_symbol: str) -> tuple[str | None, float]:
        """Spot raw symbol for a perp and the price multiplier (1000PEPE perp = 1000 x PEPE spot)."""
        if self._spot_symbols is None:
            info = await self.spot.publicGetExchangeInfo({"permissions": "SPOT"})
            self._spot_symbols = {x["symbol"] for x in info["symbols"] if x.get("status") == "TRADING"}
        base = to_symbol(perp_symbol).split("/")[0]
        mult = 1.0
        for prefix, m in (("1000000", 1e6), ("1000", 1e3), ("1M", 1e6)):
            if base.startswith(prefix) and len(base) > len(prefix):
                base, mult = base[len(prefix):], m
                break
        raw = f"{base}USDT"
        return (raw, mult) if raw in self._spot_symbols else (None, mult)

    async def spot_flow_klines(self, raw_spot: str, interval: str = "1h", limit: int = 24) -> list[list[float]]:
        """Spot klines with taker-buy volume: [ts, o, h, l, c, vol, taker_buy_vol]."""
        rows = await self.spot.publicGetKlines({"symbol": raw_spot, "interval": interval, "limit": limit})
        return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[9])]
                for r in rows]
