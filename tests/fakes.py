"""A stricter in-memory Binance USDⓈ-M fake: raises the same ccxt error classes the real exchange maps to,
allows ONE closePosition stop per side (-4130), rejects stops that would trigger immediately (-2021),
records fills and income (REALIZED_PNL / COMMISSION) so PnL reconciliation can be tested."""

from __future__ import annotations

import time

import ccxt

from shortbot.config import RiskLimits, Settings

SYM = "PUMP/USDT:USDT"
FEE = 0.0005


class FakeCcxt:
    def __init__(self, price=1.0, equity=10_000.0, hedged=False):
        self.price, self.equity, self.hedged = price, equity, hedged
        self.position = 0.0
        self.entry = 0.0
        self.stops: dict[str, float] = {}
        self.orders: list[tuple] = []
        self.fills: list[dict] = []
        self.income: list[dict] = []
        self._id = 0
        self.fail_stop = 0          # number of upcoming stop placements to fail
        self.drift_on_cancel = 0    # cancels that EXECUTE but then answer -1021 (seen live on ONDO #5)
        self.drift_before_cancel = 0  # cancels rejected with -1021 before executing
        self.fail_entry_after_fill = False
        self.fail_close = 0
        self.limits: dict[str, dict] = {}    # resting limit orders
        self.prearms: dict[str, float] = {}  # reduce-only stops placed before a position exists

    # --- market data / account
    async def fetch_ticker(self, s):
        return {"last": self.price, "quoteVolume": 100e6}

    async def fetch_balance(self):
        return {"info": {"totalMarginBalance": str(self.equity), "totalWalletBalance": str(self.equity),
                         "availableBalance": str(self.equity)}}

    async def fetch_positions(self):
        if not self.position:
            return []
        return [{"symbol": SYM, "contracts": self.position, "side": "short", "entryPrice": self.entry,
                 "markPrice": self.price, "notional": -self.position * self.price,
                 "unrealizedPnl": (self.entry - self.price) * self.position, "liquidationPrice": self.entry * 1.45}]

    async def fetch_position_mode(self, symbol=None):
        return {"hedged": self.hedged}

    async def set_position_mode(self, hedged):
        if hedged == self.hedged:
            raise ccxt.ExchangeError('binanceusdm {"code":-4059,"msg":"No need to change position side."}')
        self.hedged = hedged

    async def set_margin_mode(self, m, s):
        # live demo 2026-10-01 (MOVR split entry): with an open order on the symbol Binance answers -4067 before
        # it would say "no need to change" (-4046)
        if any(o["status"] == "open" for o in self.limits.values()):
            raise ccxt.ExchangeError('binanceusdm {"code":-4067,"msg":"Position side cannot be changed if there '
                                     'exists open orders."}')
        raise ccxt.ExchangeError('binanceusdm {"code":-4046,"msg":"No need to change margin type."}')

    async def set_leverage(self, lev, s):
        pass

    def amount_to_precision(self, s, a):
        return f"{int(a * 10) / 10:.1f}"

    def price_to_precision(self, s, p):
        return f"{p:.4f}"

    markets = {SYM: {"active": True}}

    def market(self, s):
        return {"limits": {"cost": {"min": 5}}, "precision": {"amount": 0.1}}

    # --- orders
    async def fetch_open_orders(self, s, params=None):
        # exact shape ccxt 4.5.84 returns for a Binance algo stop (verified against demo, 2026-09-27)
        return [{"id": i, "triggerPrice": p, "stopPrice": p, "side": "buy", "type": "market", "reduceOnly": True,
                 "status": "open", "info": {"orderType": "STOP_MARKET", "algoType": "CONDITIONAL",
                                            "closePosition": True, "reduceOnly": True, "triggerPrice": str(p),
                                            "side": "BUY", "algoStatus": "NEW"}} for i, p in self.stops.items()] + [
            # pre-armed reduce-only entry stops are listed too on live (trigger orders), but WITHOUT closePosition
            {"id": i, "triggerPrice": p, "stopPrice": p, "side": "buy", "type": "market", "reduceOnly": True,
             "status": "open", "info": {"orderType": "STOP_MARKET", "algoType": "CONDITIONAL", "closePosition": False,
                                        "reduceOnly": True, "triggerPrice": str(p), "side": "BUY", "algoStatus": "NEW"}}
            for i, p in self.prearms.items()]

    def _fill(self, side, amount):
        px = self.price
        self.fills.append({"side": side, "price": px, "amount": amount, "timestamp": time.time() * 1000})
        self.income.append({"incomeType": "COMMISSION", "income": str(-px * amount * FEE),
                            "time": int(time.time() * 1000)})
        if side == "sell":
            self.entry = (self.entry * self.position + px * amount) / (self.position + amount)
            self.position += amount
        else:
            self.income.append({"incomeType": "REALIZED_PNL", "income": str((self.entry - px) * amount),
                                "time": int(time.time() * 1000)})
            self.position = round(max(0.0, self.position - amount), 8)
            if not self.position:
                self.stops.clear()  # Binance cancels closePosition stops when flat

    async def create_order(self, s, type_, side, amount, price=None, params=None):
        params = params or {}
        self.orders.append((type_, side, amount, params))
        if type_ == "limit":
            self._id += 1
            self.limits[str(self._id)] = {"price": price, "amount": amount, "filled": 0.0, "status": "open"}
            return {"id": str(self._id)}
        if type_ == "STOP_MARKET" and params.get("reduceOnly") and not params.get("closePosition"):
            self._id += 1
            self.prearms[str(self._id)] = params["triggerPrice"]
            return {"id": str(self._id)}
        if type_ == "STOP_MARKET":
            if self.fail_stop:
                self.fail_stop -= 1
                raise ccxt.ExchangeError("stop rejected")
            if self.stops:
                raise ccxt.ExchangeError('{"code":-4130,"msg":"An open stop or take profit order with '
                                         'GTE and closePosition in the direction is existing."}')
            if params["triggerPrice"] <= self.price:
                raise ccxt.OrderImmediatelyFillable('{"code":-2021,"msg":"Order would immediately trigger."}')
            self._id += 1
            self.stops[str(self._id)] = params["triggerPrice"]
            return {"id": str(self._id)}
        if side == "buy" and self.fail_close:
            self.fail_close -= 1
            raise ccxt.NetworkError("timeout")
        self._fill(side, amount)
        if side == "sell" and self.fail_entry_after_fill:
            raise ccxt.RequestTimeout("timed out after fill")
        return {"average": self.price}

    async def cancel_order(self, i, s, params=None):
        drift = ccxt.InvalidNonce('binanceusdm {"code":-1021,"msg":"Timestamp for this request is outside of the '
                                  'recvWindow."}')
        if self.drift_before_cancel:
            self.drift_before_cancel -= 1
            raise drift
        self.stops.pop(i, None)
        self.prearms.pop(i, None)
        if i in self.limits and self.limits[i]["status"] != "open":
            # Binance: a filled (or already cancelled) order is unknown to cancel (MOVR 2026-10-01)
            raise ccxt.OrderNotFound('binanceusdm {"code":-2011,"msg":"Unknown order sent."}')
        if i in self.limits:
            self.limits[i]["status"] = "canceled"
        if self.drift_on_cancel:
            self.drift_on_cancel -= 1
            raise drift
        if i in self.limits:   # the cancel response carries what had already filled (executedQty)
            return {"id": i, "status": "canceled", "filled": self.limits[i]["filled"]}

    async def fetch_order(self, i, s):
        o = self.limits[i]
        return {"id": i, "status": o["status"], "filled": o["filled"]}

    async def edit_order(self, i, s, type_, side, amount, price):
        self.limits[i]["price"] = price
        return {"id": i}

    async def fetch_ohlcv(self, s, tf="1h", since=None, limit=30):
        return [[k, self.price, self.price * 1.02, self.price * 0.98, self.price, 1] for k in range(limit)]

    def fill_limit(self, i, qty=None):
        """Simulate price reaching a resting limit sell."""
        o = self.limits[i]
        q = qty or o["amount"]
        self.price = o["price"]
        self._fill("sell", q)
        o["filled"] += q
        o["status"] = "closed" if o["filled"] >= o["amount"] else "open"

    async def cancel_all_orders(self, s, params=None):
        self.stops.clear()

    async def fetch_my_trades(self, s, since=None, limit=None):
        return [f for f in self.fills if f["timestamp"] >= (since or 0) - 5000]

    async def fapiPrivateGetIncome(self, req):
        return [i for i in self.income if i["time"] >= req.get("startTime", 0) - 5000]

    def trigger_stop(self):
        """Simulate the exchange stop firing at the current price."""
        self._fill("buy", self.position)


class FakeEx:
    def __init__(self, fake, limits: RiskLimits | None = None):
        self.settings = Settings("demo", "k", "s", ":memory:", "", "", limits or RiskLimits())
        self.fake = fake
        self.public = fake
        self.trade = fake

    limits = property(lambda self: self.settings.limits)
    experiment = None

    def require_trade(self):
        return self.fake

    async def markets(self):
        return {}

    async def trade_markets(self):
        return None

    async def resync_time(self):
        self.resyncs = getattr(self, "resyncs", 0) + 1


async def ok_prechecks(ex, symbol, ticker):
    return {"vetoes": [], "regime": "favorable", "features": {"rsi_1h": 88}}


async def neutral_budget(trader, symbol, regime, conviction="B", setup=None):
    eq = trader.t.equity
    return {"risk_usd": eq * trader.ex.limits.risk_per_trade_pct / 100, "blocked": False, "reasons": [],
            "multipliers": {}}
