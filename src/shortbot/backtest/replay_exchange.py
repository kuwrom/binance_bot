"""ReplayExchange: the exchange surface that the live scanner, DataHub and market map use (exchange.Exchanges),
served from the replay cache AS OF the market clock (shortbot.clock). The live pipeline then runs unchanged on
history: scan_shorts(ReplayExchange(...)) IS the real scanner, signals, tape classifier and setup detectors (#29).

Point in time: every read is bounded by clock.now_ms() at the SQL level (a bar is read only once it has closed),
and bars come back like the REST endpoints return them, the forming bar last (replay_bars). Not replayable (no
archive): the order book, and the network feeds offline_feeds() blocks; they fail like a live fetch failure, so they
land in data_missing and never veto (only funding, open_interest, market_info and candles_1h are critical)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from .. import clock
from ..config import RiskLimits
from ..exchange import NO_DELIVERY, to_raw, to_symbol
from . import replay_bars as RB
from .replay_store import Store

MIN, HOUR, DAY = RB.MIN, RB.HOUR, RB.DAY
FINE_LOOKBACK = 25 * HOUR + 10 * MIN      # 1m window per symbol: 5m x 288 = 24 h, the forming day, 1m x 60
HOURS_LOOKBACK = 200 * HOUR               # 1h x 170 (market map), 4h x 18
DAYS_LOOKBACK = 210 * DAY                 # 1d x 200 (lifecycle, controller)
TF_STEP = {"5m": 5 * MIN, "15m": 15 * MIN, "30m": 30 * MIN, "1h": HOUR, "2h": 2 * HOUR, "4h": 4 * HOUR, "1d": DAY}


class ReplayUnavailable(RuntimeError):
    """No point-in-time history for this input."""


class ReplayExchange:
    def __init__(self, store: Store, limits_at: Callable[[int], RiskLimits] | None = None,
                 db_path: Any = None, universe: str = "prod", delist_lead_days: float = 7.0, oi_lag_ms: int = 0):
        if universe not in ("prod", "demo"):
            raise ValueError("universe must be prod or demo")
        self.store, self.meta = store, store.meta()
        self._limits_at = limits_at or (lambda t: RiskLimits())
        self.settings = SimpleNamespace(db_path=db_path)
        self.universe, self.lead_ms, self.oi_lag_ms = universe, int(delist_lead_days * DAY), oi_lag_ms
        self.public = _Public(self)
        self.trade = None
        self._t: int | None = None
        self._cache: dict[tuple, Any] = {}

    # ------------------------------------------------------------ the moment
    def now(self) -> int:
        t = clock.now_ms()
        if t != self._t:
            self._t, self._cache = t, {}
        return t

    def _memo(self, key: tuple, fn: Callable[[int], Any]) -> Any:
        t = self.now()
        if key not in self._cache:
            self._cache[key] = fn(t)
        return self._cache[key]

    @property
    def limits(self) -> RiskLimits:
        return self._limits_at(self.now())

    # ------------------------------------------------------------ universe and metadata
    def listed(self, raw: str, t: int) -> bool:
        m = self.meta.get(raw)
        if not m or (m.get("onboard_ms") or 0) > t or (m.get("delivery_ms") and t >= m["delivery_ms"]):
            return False
        return self.universe == "prod" or bool(m.get("demo"))

    def scheduled(self, raw: str, t: int) -> int | None:
        """deliveryDate as known at t: Binance announces a delisting some days ahead; we only know the date."""
        d = (self.meta.get(raw) or {}).get("delivery_ms")
        return d if d and t >= d - self.lead_ms else None

    def _market(self, raw: str, t: int) -> dict[str, Any]:
        m = self.meta[raw]
        return {"symbol": to_symbol(raw), "id": raw, "linear": True, "quote": "USDT", "active": True,
                "limits": {"cost": {"min": m.get("min_notional") or 5.0}},
                "info": {"symbol": raw, "contractType": "PERPETUAL", "underlyingType": "COIN", "status": "TRADING",
                         "quoteAsset": "USDT", "onboardDate": str(m.get("onboard_ms") or 0),
                         "deliveryDate": str(self.scheduled(raw, t) or NO_DELIVERY),
                         "underlyingSubType": m.get("tags") or []}}

    async def coin_perps(self) -> dict[str, dict]:
        t = self.now()
        return {to_symbol(r): self._market(r, t) for r in sorted(self.meta) if self.listed(r, t)}

    async def market_info(self, symbol: str) -> dict:
        raw, t = to_raw(symbol), self.now()
        if raw not in self.meta or (self.meta[raw].get("onboard_ms") or 0) > t:   # live: any market in exchangeInfo
            raise ValueError(f"{to_symbol(raw)} is not a Binance USDⓈ-M market at {t}")
        m, d = self.meta[raw], self.scheduled(raw, t)
        on = m.get("onboard_ms")
        return {"symbol": to_symbol(raw), "listing_age_days": round((t - on) / 86_400_000, 1) if on else None,
                "delisting_scheduled": d is not None, "delivery_date_ms": d, "tags": m.get("tags") or [],
                "min_notional": m.get("min_notional") or 5.0}

    async def account_tradable(self, symbol: str) -> bool:
        return self.universe == "prod" or bool((self.meta.get(to_raw(symbol)) or {}).get("demo"))

    # ------------------------------------------------------------ bars (bounded by the clock in SQL)
    def fine(self, kind: str, raw: str) -> list[tuple]:
        return self._memo(("fine", kind, raw), lambda t: self.store.fine(kind, raw, t - FINE_LOOKBACK, t - MIN + 1))

    def _hours(self, kind: str, raw: str) -> list[tuple]:
        return self._memo(("hours", kind, raw), lambda t: self.store.hourly(kind, raw, t - HOURS_LOOKBACK, t - HOUR + 1))

    def _days(self, raw: str) -> list[tuple]:
        return self._memo(("days", raw), lambda t: self.store.daily(raw, t - DAYS_LOOKBACK, t - DAY + 1))

    def klines(self, kind: str, raw: str, tf: str, limit: int) -> list[tuple]:
        step, t = RB.tf_ms(tf), self.now()
        if step == DAY and kind == "perp":
            return RB.klines(self.fine(kind, raw), tf, t, limit, self._days(raw), DAY)
        if step >= HOUR and kind == "perp":
            return RB.klines(self.fine(kind, raw), tf, t, limit, self._hours(kind, raw), HOUR)
        return RB.klines(self.fine(kind, raw), tf, t, limit)

    async def flow_klines(self, symbol: str, interval: str = "5m", limit: int = 288) -> list[list[float]]:
        return [[b[0], b[1], b[2], b[3], b[4], b[5], b[8]] for b in self.klines("perp", to_raw(symbol), interval, limit)]

    async def premium_klines(self, symbol: str, interval: str = "1h", limit: int = 24) -> list[list[float]]:
        return [[b[0], b[1], b[2], b[3], b[4]] for b in self.klines("prem", to_raw(symbol), interval, limit)]

    async def spot_symbol(self, perp_symbol: str) -> tuple[str | None, float]:
        sp = (self.meta.get(to_raw(perp_symbol)) or {}).get("spot")
        if not sp:
            return None, 1.0
        recent = [b for b in self.fine("spot", sp[0]) if b[0] >= self.now() - HOUR]
        return (sp[0], float(sp[1])) if recent else (None, float(sp[1]))

    async def spot_flow_klines(self, raw_spot: str, interval: str = "1h", limit: int = 24) -> list[list[float]]:
        return [[b[0], b[1], b[2], b[3], b[4], b[5], b[8]] for b in self.klines("spot", raw_spot, interval, limit)]

    def tickers(self) -> dict[str, dict]:
        def build(t: int) -> dict[str, dict]:
            end = t // MIN * MIN
            start = end - DAY
            hours = self.store.hourly_all("perp", start, end - HOUR + 1)
            edge_a = self.store.fine_all("perp", start, -(-start // HOUR) * HOUR)
            edge_b = self.store.fine_all("perp", end // HOUR * HOUR, end)
            out = {}
            for raw in self.meta:
                if not self.listed(raw, t):
                    continue
                tk = RB.ticker(edge_a.get(raw, []) + edge_b.get(raw, []), t, hours.get(raw, []))
                if tk:
                    out[raw] = {"symbol": to_symbol(raw), "info": {}, **tk}
            return out
        return self._memo(("tickers",), build)

    # ------------------------------------------------------------ funding and positioning
    def _settles(self, raw: str) -> list[tuple[int, float]]:
        return self._memo(("settles", raw), lambda t: self.store.funding(raw, t - 30 * DAY, t + 1))

    def funding_now(self, raw: str) -> dict[str, Any]:
        s, t = self._settles(raw), self.now()
        if not s:
            return {}
        iv = RB.funding_interval_h([x[0] for x in s])
        est = RB.predicted_funding(self.fine("prem", raw), t, s[-1][0], iv)
        mark = self.fine("mark", raw) or self.fine("perp", raw)
        return {"symbol": to_symbol(raw), "fundingRate": est if est is not None else s[-1][1], "interval_h": iv,
                "fundingTimestamp": s[-1][0] + iv * HOUR, "markPrice": mark[-1][4] if mark else None,
                "estimated": est is not None}

    async def funding_intervals(self) -> dict[str, dict]:
        t = self.now()
        return {r: {"interval_h": RB.funding_interval_h([x[0] for x in self._settles(r)]), "cap": 0.0}
                for r in self.meta if self.listed(r, t) and len(self._settles(r)) >= 2}

    def _metrics(self, raw: str, hours: int) -> list[dict[str, Any]]:
        return self._memo(("metrics", raw, hours),
                          lambda t: self.store.metrics(raw, t - hours * HOUR, t - self.oi_lag_ms + 1))

    async def oi_history(self, symbol: str, period: str = "1h", limit: int = 25) -> list[dict]:
        step = TF_STEP.get(period, HOUR)
        rows = [r for r in self._metrics(to_raw(symbol), (limit + 2) * step // HOUR + 2)
                if r["ts"] % step == 0 and r.get("oi") is not None]
        return [{"ts": r["ts"], "oi": r["oi"], "oi_value": r["oi_val"]} for r in rows[-limit:]]

    async def taker_ratio(self, symbol: str, period: str = "1h", limit: int = 12) -> list[float]:
        return RB.taker_ratios(self.fine("perp", to_raw(symbol)), self.now(), limit)

    async def long_short_ratios(self, symbol: str, period: str = "1h") -> dict[str, float | None]:
        rows = self._metrics(to_raw(symbol), 2)
        last = rows[-1] if rows else {}
        return {"top_trader_position_ls": last.get("ls_top"), "global_account_ls": last.get("ls_global")}


class _Public:
    """The ccxt client surface (ex.public) the pipeline calls."""

    def __init__(self, rex: ReplayExchange):
        self.x = rex

    async def fetch_tickers(self, symbols: list[str] | None = None, params: dict | None = None) -> dict[str, dict]:
        return {to_symbol(r): tk for r, tk in self.x.tickers().items()}

    async def fetch_ticker(self, symbol: str, params: dict | None = None) -> dict:
        tk = self.x.tickers().get(to_raw(symbol))
        if not tk:
            raise ReplayUnavailable(f"no ticker for {symbol} at {self.x.now()}")
        return tk

    async def fetch_ohlcv(self, symbol: str, timeframe: str = "1m", since: int | None = None, limit: int | None = None,
                          params: dict | None = None) -> list[list[float]]:
        kind = "mark" if (params or {}).get("price") == "mark" else "perp"
        bars = self.x.klines(kind, to_raw(symbol), timeframe, limit or 500)
        return [[b[0], b[1], b[2], b[3], b[4], 0.0 if kind == "mark" else b[5]] for b in bars]

    async def fetch_funding_rates(self, symbols: list[str] | None = None, params: dict | None = None) -> dict:
        t = self.x.now()
        out = {}
        for raw in self.x.meta:
            if self.x.listed(raw, t) and (f := self.x.funding_now(raw)):
                out[to_symbol(raw)] = {**f, "fundingDatetime": _iso(f["fundingTimestamp"])}
        return out

    async def fetch_funding_rate_history(self, symbol: str, since: int | None = None, limit: int | None = None,
                                         params: dict | None = None) -> list[dict]:
        s = self.x._settles(to_raw(symbol))[-(limit or 100):]
        return [{"symbol": to_symbol(symbol), "fundingRate": r, "timestamp": ts, "datetime": _iso(ts)} for ts, r in s]

    async def fetch_order_book(self, symbol: str, limit: int | None = None, params: dict | None = None) -> dict:
        raise ReplayUnavailable("order book: no point-in-time history")


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
