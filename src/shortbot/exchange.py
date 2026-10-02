"""Two ccxt clients: `public` (prod market data, no keys) and `trade` (demo or live, keyed).

Demo trading has no fapiData endpoints, so all analytics always read from prod public data.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import ccxt.async_support as ccxt

from .config import Settings
from .risk import bracket_for

log = logging.getLogger("shortbot.exchange")
NO_DELIVERY = 4133404800000  # Binance's placeholder deliveryDate for perpetuals
MARKETS_TTL = 3600
# the trading client's markets: a perp listed since start, a tick/step change or a delisting shows within the TTL; a
# symbol the client doesn't know reloads at once, at most once a FORCED_RELOAD_MIN_S (a typo must not hammer it)
TRADE_MARKETS_TTL = 1800
FORCED_RELOAD_MIN_S = 60
RELOAD_RETRY_S = 300          # after a failed refresh the cached copy serves until then
BRACKETS_TTL = 6 * 3600       # leverage brackets per symbol (GET /fapi/v1/leverageBracket, weight 1)


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)  # indirection so tests can skip real waits


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
        self._trade_next_load = 0.0   # when the trading client's markets are due a refresh
        self._trade_tried_at = 0.0    # last load attempt, success or not
        self._brackets: dict[str, tuple[float, list[dict]]] = {}   # symbol -> (read at, bracket rows)
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

    async def trade_markets(self, symbol: str | None = None) -> None:
        """Only what order placement needs (the trading client's own markets). Exits must never depend on the prod
        analytics metadata. ccxt loads them once (load_markets without reload returns the cache), so a long-running
        guardian or MCP server refreshes them every TRADE_MARKETS_TTL, and at once for a `symbol` they don't hold: a
        perp listed since start is a BadSymbol to ccxt and its position comes keyed by the raw id. The first load is
        retried and raises; a failed refresh keeps the cached copy (one try: a protective pass never waits three)."""
        t = self.require_trade()
        unknown = not t.markets or (symbol is not None and to_symbol(symbol) not in t.markets)
        if self._trade_lock.locked() and not unknown:
            return   # a refresh is in flight and brings this caller nothing it needs: the cached copy serves
        if not self._trade_lock.locked() and not self._trade_markets_due(t, symbol):
            return
        asked = time.time()
        async with self._trade_lock:  # one load even when 25 scan tasks ask at once
            if t.markets and (self._trade_tried_at >= asked or not self._trade_markets_due(t, symbol)):
                return
            self._trade_tried_at = time.time()
            if not t.markets:
                await self._load_trade_markets(t)
            else:
                try:
                    await t.load_markets(reload=True)
                except Exception as e:  # noqa: BLE001 - stale markets beat none; retried after RELOAD_RETRY_S
                    log.warning("trade markets refresh failed, keeping the cached copy: %s: %s", type(e).__name__, e)
                    self._trade_next_load = time.time() + RELOAD_RETRY_S
                    return
            self._trade_next_load = time.time() + TRADE_MARKETS_TTL

    def _trade_markets_due(self, t, symbol: str | None) -> bool:
        now = time.time()
        unknown = symbol is not None and to_symbol(symbol) not in t.markets
        return (not t.markets or now >= self._trade_next_load
                or (unknown and now - self._trade_tried_at >= FORCED_RELOAD_MIN_S))

    async def _load_trade_markets(self, t) -> None:
        for i in range(3):
            try:
                await t.load_markets()
                return
            except Exception:
                if i == 2:
                    raise
                await _sleep(2)

    async def maint_margin(self, symbol: str, notional: float) -> tuple[float, float] | None:
        """(maintenance margin rate, maintenance amount) of the account's leverage bracket holding `notional` on
        `symbol`, for the liquidation estimate (risk.est_liquidation_distance_pct). GET /fapi/v1/leverageBracket for the
        one symbol (weight 1; ccxt's fetch_leverage_tiers reads every symbol and drops markets it hasn't loaded),
        cached BRACKETS_TTL. A failed read serves the stale copy; None without keys or a copy (the estimate then
        assumes the conservative fallback rate)."""
        if not self.trade:
            return None
        sym, raw = to_symbol(symbol), to_raw(symbol)
        hit = self._brackets.get(sym)
        if not hit or time.time() - hit[0] > BRACKETS_TTL:
            try:
                r = await self.trade.fapiPrivateGetLeverageBracket({"symbol": raw})
                rows = r if isinstance(r, list) else [r]   # the doc's schema: an array or an object
                row = next((x for x in rows if x.get("symbol") == raw and x.get("brackets")), None)
                if row:
                    hit = self._brackets[sym] = (time.time(), row["brackets"])
            except Exception as e:  # noqa: BLE001 - the estimate falls back (stale, else the conservative rate)
                log.warning("leverage brackets %s: %s: %s", raw, type(e).__name__, e)
        return bracket_for(hit[1], notional) if hit else None

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
