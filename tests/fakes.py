"""A strict in-memory Binance USDⓈ-M fake that answers like ccxt 4.5.84 does, with the same error classes and codes
(fake_errors.py), so the desk's failure paths run against real behaviour:
- several symbols (`add_symbol`), each with its own price, position and resting orders
- conditional orders live on the Algo service (change log 2025-11-06, live 2025-12-09): listed, fetched and
  cancelled only with params trigger=True; ONE closePosition stop per symbol and side (-4130); -2021 for a stop
  that would trigger at once
- moving a price (`fake.price = x`, `set_price(sym, x)`) fills resting limits and triggers stops on the way, with
  closePosition semantics (fake_engine.py); `advance(s)` expires GTD limits and forgets orders that died unfilled
  3 days ago (-2013); `fill_limit` / `trigger_stop` force one
- rejections: reduce-only without a position (-2022), tick size (-4014), min notional (-4164), margin (-2019),
  GTD under 10 min (-5040), a client id that an OPEN order already uses (-4116)
- fault injection: answers lost after the order ran (-1007 / 503), 503 before it ran, -1021 around cancels
- `fake.prod` is the separate keyless prod client (`ex.public`): its prices can diverge from the demo book
- fills and income (REALIZED_PNL / COMMISSION) so PnL reconciliation can be tested; userTrades in 7-day windows (fake_engine)
- positions, margin type, Multi-Assets mode, ADL quantile and Binance's own closes (fake_account.py)"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

import ccxt

from shortbot.config import RiskLimits, Settings
from tests import fake_errors as err
from tests.fake_account import Account
from tests.fake_engine import ALGO_STATUS, SYM, Engine, ProdView, raw

STOP_TYPES = ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET")


def _trigger(params) -> bool:   # ccxt cancel/fetch/open-orders: stop|trigger|conditional select the Algo endpoints
    return any((params or {}).get(k) for k in ("stop", "trigger", "conditional"))


def _on_tick(px: float, tick: float) -> bool:
    return Decimal(str(px)) % Decimal(str(tick)) == 0


class FakeCcxt(Account, Engine):
    def __init__(self, price=1.0, equity=10_000.0, hedged=False):
        self.equity, self.hedged = equity, hedged
        self.sym: dict[str, dict] = {}        # the exchange's listing
        self.markets: dict[str, dict] = {}    # the client's loaded markets (load_markets, fake_engine.py)
        self.market_loads, self.brackets = 0, {}   # brackets: per-symbol leverage brackets (default BRACKETS)
        self.stops: dict[str, float] = {}     # open closePosition stops: id -> trigger
        self.prearms: dict[str, float] = {}   # open reduce-only stops placed before a position exists
        self.algo: dict[str, dict] = {}       # every conditional order: symbol, qty, trigger, client id, algoStatus
        self.limits: dict[str, dict] = {}     # limit orders, any status
        self.market_orders: dict[str, dict] = {}
        self.orders: list[tuple] = []         # every create_order call: (type, side, amount, params)
        self.fills: list[dict] = []
        self.income: list[dict] = []
        self._id = 0
        self.clock_s = 0.0                    # exchange clock offset (advance)
        self.fail_stop = 0          # number of upcoming closePosition stop placements to fail
        self.drift_on_cancel = 0    # cancels that EXECUTE but then answer -1021 (seen live on ONDO #5)
        self.drift_before_cancel = 0  # cancels rejected with -1021 before executing
        self.fail_entry_after_fill = False
        self.fail_close = 0
        self.ack_lost = 0           # orders that EXECUTE, then answer -1007 (execution status unknown)
        self.ack_lost_503 = 0       # orders that EXECUTE, then answer 503 "Unknown error" (status unknown)
        self.unavailable_503 = 0    # orders refused with 503 "Service Unavailable." (nothing ran)
        self.ack_lost_unsent = 0    # orders that answer -1007 and did NOT run: "unknown" cuts both ways
        self.multi_assets, self.adl_answer, self.force_orders = False, None, []   # fake_account.py
        self.prod = ProdView(self)
        self.add_symbol(SYM, price)

    def add_symbol(self, s, price, tick=0.0001, step=0.1, min_notional=5.0, quote_volume=100e6):
        """A perp on the demo account. ccxt TICK_SIZE precision: prices round to `tick`, amounts truncate to `step`;
        `min_notional` is the MIN_NOTIONAL filter. lev 20 is a fake default (the real one varies by symbol). Listed
        after the client's first load_markets, the client knows it only after a reload."""
        self.sym[s] = {"price": price, "qty": 0.0, "entry": 0.0, "lev": 20, "tick": tick, "step": step,
                       "min": min_notional, "qv": quote_volume}
        if not self.market_loads:
            self.markets[s] = {"active": True}

    def _spec(self, s) -> dict:
        if s not in self.sym or s not in self.markets:   # ccxt market(): a symbol the client hasn't loaded
            raise ccxt.BadSymbol(f"binanceusdm does not have market symbol {s}")
        return self.sym[s]

    # SYM's book, as single-symbol tests use it; assigning `price` moves the market (fills and triggers)
    price = property(lambda self: self.sym[SYM]["price"], lambda self, p: self.set_price(SYM, p))
    position = property(lambda self: self.sym[SYM]["qty"], lambda self, q: self.sym[SYM].__setitem__("qty", q))
    entry = property(lambda self: self.sym[SYM]["entry"], lambda self, e: self.sym[SYM].__setitem__("entry", e))

    def _next_id(self) -> str:
        self._id += 1
        return str(self._id)

    # --- market data / account
    async def fetch_ticker(self, s):
        b = self._spec(s)
        return {"symbol": s, "last": b["price"], "quoteVolume": b["qv"]}

    async def fetch_ohlcv(self, s, tf="1h", since=None, limit=30):
        px = self._spec(s)["price"]
        return [[k, px, px * 1.02, px * 0.98, px, 1] for k in range(limit)]

    async def fetch_balance(self):
        return {"info": {"totalMarginBalance": str(self.equity), "totalWalletBalance": str(self.equity),
                         "availableBalance": str(round(self.available(), 8))}}

    async def fetch_position_mode(self, symbol=None):
        return {"hedged": self.hedged}

    async def set_position_mode(self, hedged):
        if hedged == self.hedged:
            raise err.position_mode_already_set()
        self.hedged = hedged

    async def set_leverage(self, lev, s):
        self._spec(s)["lev"] = lev
        return {"leverage": lev, "symbol": raw(s)}

    def amount_to_precision(self, s, a):
        step = Decimal(str(self._spec(s)["step"]))
        return str((Decimal(str(a)) / step).to_integral_value(ROUND_DOWN) * step)

    def price_to_precision(self, s, p):
        tick = Decimal(str(self._spec(s)["tick"]))
        return str((Decimal(str(p)) / tick).to_integral_value(ROUND_HALF_UP) * tick)

    def market(self, s):
        b = self._spec(s)
        return {"id": raw(s), "symbol": s, "limits": {"cost": {"min": b["min"]}},
                "precision": {"amount": b["step"], "price": b["tick"]}}

    # --- order views (the fields ccxt's parse_order fills)
    def _order_view(self, i):
        o = self.limits.get(i) or self.market_orders[i]
        # ccxt 4.5.84 parse_order: lastTradeTimestamp = updateTime only for 'closed', or 'open' with a fill; None for
        # canceled/expired orders, even (partly) filled ones
        last = o["last_ts"] if o["status"] == "closed" or (o["status"] == "open" and o["filled"]) else None
        return {"id": i, "clientOrderId": o["cid"], "symbol": o["symbol"], "type": o["type"], "side": o["side"],
                "price": o["price"], "amount": o["amount"], "filled": o["filled"], "average": o["avg"],
                "remaining": o["amount"] - o["filled"], "status": o["status"], "reduceOnly": o["reduce"],
                "timestamp": o["ts"], "lastTradeTimestamp": last}

    def _algo_view(self, i):
        a = self.algo.get(i, {})
        close = i in self.stops or (i not in self.prearms and a.get("close", True))
        p = self.stops.get(i, self.prearms.get(i, a.get("trigger")))
        st = a.get("status", "NEW")
        # exact shape ccxt 4.5.84 returns for a Binance algo stop (verified against demo, 2026-09-27); pre-armed
        # reduce-only entry stops are listed too on live (trigger orders), but WITHOUT closePosition
        return {"id": i, "clientOrderId": a.get("cid"), "symbol": a.get("symbol", SYM), "triggerPrice": p,
                "stopPrice": p, "side": "buy", "type": "market", "reduceOnly": True, "amount": a.get("qty"),
                "status": ALGO_STATUS[st], "info": {"orderType": "STOP_MARKET", "algoType": "CONDITIONAL",
                                                    "closePosition": close, "reduceOnly": True,
                                                    "triggerPrice": str(p), "side": "BUY", "algoStatus": st,
                                                    "actualOrderId": a.get("actual", "")}}

    def _open_cid(self, cid) -> bool:
        return any(o["cid"] == cid and o["status"] == "open" for o in self.limits.values()) or any(
            self.algo.get(i, {}).get("cid") == cid for i in [*self.stops, *self.prearms])

    # --- orders
    async def fetch_open_orders(self, s, params=None):
        self._spec(s)   # ccxt binance.py fetch_open_orders: self.market(symbol)
        if _trigger(params):   # GET /fapi/v1/openAlgoOrders
            return [self._algo_view(i) for i in [*self.stops, *self.prearms] if self._algo_sym(i) == s]
        return [self._order_view(i) for i, o in self.limits.items() if o["symbol"] == s and o["status"] == "open"]

    async def create_order(self, s, type_, side, amount, price=None, params=None):
        params = params or {}
        self.orders.append((type_, side, amount, params))
        self._spec(s)
        trig = next((params[k] for k in ("triggerPrice", "stopPrice") if params.get(k) is not None), None)
        if trig is None and type_.upper() in STOP_TYPES:   # ccxt sends it to /fapi/v1/order, which refuses it
            raise err.switch_algo()
        path = "order" if trig is None else "algoOrder"   # ccxt: a trigger price routes to POST /fapi/v1/algoOrder
        if self.unavailable_503:
            self.unavailable_503 -= 1
            raise err.unavailable(path, executed=False)
        if self.ack_lost_unsent:
            self.ack_lost_unsent -= 1
            raise err.status_unknown()
        cid = params.get("clientAlgoId") or params.get("newClientOrderId") or params.get("clientOrderId")
        if cid and self._open_cid(cid):   # New-Order doc: unique among OPEN orders only (a filled one is reusable)
            raise err.duplicate_client_id()
        if trig is not None:
            o = self._new_stop(s, side, amount, float(trig), params, cid)
        elif type_ == "limit":
            o = self._new_limit(s, side, amount, price, params, cid)
        else:
            o = self._new_market(s, side, amount, params, cid)
        if self.ack_lost:
            self.ack_lost -= 1
            raise err.status_unknown()
        if self.ack_lost_503:
            self.ack_lost_503 -= 1
            raise err.unavailable(path, executed=True)
        return o

    def _new_stop(self, s, side, amount, trig, params, cid):
        assert side == "buy", "short-only fake: buy stops only"
        close = bool(params.get("closePosition"))
        if close:
            if self.fail_stop:   # a definite refusal: ccxt never raises a bare, code-less ExchangeError for Binance
                self.fail_stop -= 1
                raise err.max_stop_orders()
            if any(self._algo_sym(i) == s for i in self.stops):
                raise err.one_close_position_stop()
        if trig <= self.sym[s]["price"]:   # a BUY STOP_MARKET triggers at price >= triggerPrice
            raise err.would_trigger()
        i = self._next_id()
        # placed without a margin or position check (change log 2025-11-06); reduce-only is enforced at trigger
        (self.stops if close else self.prearms)[i] = trig
        self.algo[i] = {"symbol": s, "qty": None if close else amount, "trigger": trig, "cid": cid,
                        "status": "NEW", "close": close, "ts": self.now_ms()}
        return self._algo_view(i)

    def _new_limit(self, s, side, amount, price, params, cid):
        b = self.sym[s]
        reduce = bool(params.get("reduceOnly"))
        if not _on_tick(price, b["tick"]):
            raise err.tick_size()
        if reduce and (side != "buy" or b["qty"] <= 0):
            raise err.reduce_only_rejected()
        if not reduce and amount * price < b["min"]:
            raise err.min_notional(b["min"])
        gtd = None
        if params.get("timeInForce") == "GTD":
            gtd = int(params.get("goodTillDate") or 0)
            if gtd <= self.now_ms() + 600_000:
                raise err.gtd_too_soon()
        if not reduce:
            self._check_margin(s, amount * price)
        i = self._next_id()
        self.limits[i] = {"symbol": s, "side": side, "type": "limit", "price": price, "amount": amount,
                          "filled": 0.0, "avg": None, "status": "open", "gtd": gtd, "cid": cid, "reduce": reduce,
                          "ts": self.now_ms(), "last_ts": None}
        if side == "sell" and price <= b["price"]:   # marketable: GTD is not post-only (GTX), so it takes at once
            self._exec_limit(i, amount)
        return self._order_view(i)

    def _new_market(self, s, side, amount, params, cid):
        b = self.sym[s]
        if side == "buy" and self.fail_close:
            self.fail_close -= 1
            raise ccxt.NetworkError("timeout")
        reduce = bool(params.get("reduceOnly"))
        if reduce:
            if side != "buy" or b["qty"] <= 0:
                raise err.reduce_only_rejected()
            # inferred: a reduce-only market order bigger than the position closes just the position (binance.com
            # FAQ "Summary of Failed Orders": reduce-only market orders >= the position size get priority)
            amount = min(amount, b["qty"])
        elif amount * b["price"] < b["min"]:
            raise err.min_notional(b["min"])
        elif side == "sell":
            self._check_margin(s, amount * b["price"])
        else:   # one-way mode nets positions: this would open a LONG, which the short-only fake does not model
            assert amount <= b["qty"] + 1e-12, f"fake: a BUY of {amount} without reduceOnly flips {s} long"
        i = self._record_market(s, side, amount, reduce, cid)
        if side == "sell" and self.fail_entry_after_fill:
            raise ccxt.RequestTimeout("timed out after fill")
        return self._order_view(i)

    async def cancel_order(self, i, s, params=None):
        if self.drift_before_cancel:
            self.drift_before_cancel -= 1
            raise err.clock_drift()
        if _trigger(params):   # DELETE /fapi/v1/algoOrder by algoId
            if i not in self.stops and i not in self.prearms:
                # finished, cancelled or unknown to the Algo service. SECONDARY source: testnet probes 2026-09-24
                # (github.com/bcup02/trade-alerts PR #65) saw -2011 on a repeat cancel
                raise err.unknown_order()
            self._retire(i, "CANCELED")
            resp = {"id": i, "status": None, "info": {"algoId": i, "code": "200", "msg": "success"}}
        else:   # DELETE /fapi/v1/order: open regular orders only. An algo id here is -2011 too (PetroSa2 #650)
            o = self.limits.get(i)
            if not o or o["status"] != "open":
                # Binance: a filled (or already cancelled) order is unknown to cancel (MOVR 2026-10-01)
                raise err.unknown_order()
            o["status"] = "canceled"
            resp = {"id": i, "status": "canceled", "filled": o["filled"]}   # the cancel carries executedQty
        if self.drift_on_cancel:
            self.drift_on_cancel -= 1
            raise err.clock_drift()
        return resp

    async def fetch_order(self, i, s, params=None):
        params = params or {}
        cid = params.get("clientOrderId") or params.get("origClientOrderId") or params.get("clientAlgoId")
        if _trigger(params):   # GET /fapi/v1/algoOrder by algoId or clientAlgoId
            pool = {k: self.algo.get(k, {}) for k in {*self.algo, *self.stops, *self.prearms}}
            hits = [k for k, a in pool.items() if (a.get("cid") == cid if cid else k == i)]
            if not hits:
                raise err.no_such_order()
            return self._algo_view(max(hits, key=int))
        pool = {**self.limits, **self.market_orders}
        hits = [k for k, o in pool.items() if (o["cid"] == cid if cid else k == i)]
        if not hits:   # incl. an algo id on the regular endpoint (freqtrade #12681: -2013)
            raise err.no_such_order()
        return self._order_view(max(hits, key=int))

    async def edit_order(self, i, s, type_, side, amount, price, params=None):
        o = self.limits.get(i)
        if o is None:   # incl. an untriggered algo order: change log 2025-11-06, they cannot be modified (inferred)
            raise err.no_such_order()
        if not _on_tick(price, self.sym[s]["tick"]):
            raise err.tick_size()
        o["price"] = price
        return self._order_view(i)

    async def cancel_all_orders(self, s, params=None):
        if _trigger(params):   # DELETE /fapi/v1/algoOpenOrders: every conditional order on the symbol
            for i in [i for i in [*self.stops, *self.prearms] if self._algo_sym(i) == s]:
                self._retire(i, "CANCELED")
        else:
            for o in self.limits.values():
                if o["symbol"] == s and o["status"] == "open":
                    o["status"] = "canceled"
        return {"code": 200, "msg": "The operation of cancel all open order is done."}

    async def fapiPrivateGetIncome(self, req):
        return [i for i in self.income if i["time"] >= req.get("startTime", 0)
                and req.get("symbol") in (None, i["symbol"]) and req.get("incomeType") in (None, i["incomeType"])]

    async def fapiPrivateGetOpenOrders(self, req=None):
        """Raw GET /fapi/v1/openOrders (Current-All-Open-Orders doc: all symbols without `symbol`, weight 40; regular
        orders only, conditional ones are on /fapi/v1/openAlgoOrders). Fields and string numbers as in the New-Order
        doc's order object. Binance generates a client id when none was sent (its format is undocumented)."""
        s = (req or {}).get("symbol")
        return [{"orderId": int(i), "clientOrderId": o["cid"] or f"autogen{i}", "symbol": raw(o["symbol"]),
                 "side": o["side"].upper(), "type": "LIMIT", "origType": "LIMIT", "positionSide": "BOTH",
                 "status": "PARTIALLY_FILLED" if o["filled"] else "NEW", "price": str(o["price"]),
                 "origQty": str(o["amount"]), "executedQty": str(o["filled"]), "reduceOnly": o["reduce"],
                 "closePosition": False, "timeInForce": "GTD" if o["gtd"] else "GTC", "goodTillDate": o["gtd"] or 0,
                 "time": o["ts"], "updateTime": o["last_ts"] or o["ts"]}
                for i, o in self.limits.items() if o["status"] == "open" and s in (None, raw(o["symbol"]))]

    # --- test controls
    def fill_limit(self, i, qty=None):
        """Fill a resting limit at its own price, and only that: nothing else on the book reacts."""
        o = self.limits[i]
        self.sym[o["symbol"]]["price"] = o["price"]
        self._exec_limit(i, qty or o["amount"] - o["filled"])

    def trigger_stop(self, symbol=SYM):
        """Simulate the exchange stop firing at the current price."""
        i = next((i for i in self.stops if self._algo_sym(i) == symbol), None)
        if i is not None:
            self._trigger(i)
        elif self.sym[symbol]["qty"]:
            self._fill(symbol, "buy", self.sym[symbol]["qty"])


class FakeEx:
    def __init__(self, fake, limits: RiskLimits | None = None):
        self.settings = Settings("demo", "k", "s", ":memory:", "", "", limits or RiskLimits())
        self.fake = fake
        self.public = fake.prod   # prod market data: a separate client from the demo trade one
        self.trade = fake

    limits = property(lambda self: self.settings.limits)
    experiment = None

    def require_trade(self):
        return self.fake

    async def markets(self):
        return {}

    async def trade_markets(self, symbol=None):
        return None

    async def maint_margin(self, symbol, notional):
        return None

    async def resync_time(self):
        self.resyncs = getattr(self, "resyncs", 0) + 1


async def ok_prechecks(ex, symbol, ticker):
    return {"vetoes": [], "regime": "favorable", "features": {"rsi_1h": 88}}


async def neutral_budget(trader, symbol, regime, conviction="B", setup=None):
    eq = trader.t.equity
    return {"risk_usd": eq * trader.ex.limits.risk_per_trade_pct / 100, "blocked": False, "reasons": [],
            "multipliers": {}}
