"""The exchange side of the fake (mixed into FakeCcxt, fakes.py): prices that move, resting orders that fill or
trigger on their own, GTD expiry, isolated margin, fills and income. Short-only: the book only ever holds SELL
limits above the market and BUY stops above the market, so only an upward move can fill or trigger anything.
Also ProdView: the separate keyless prod client (`ex.public`), whose prices can diverge from the demo book."""

from __future__ import annotations

import time

from tests import fake_errors as err
from tests.fake_reduce import ReduceOnly

SYM = "PUMP/USDT:USDT"
FEE = 0.0005
WEEK_MS = 7 * 86_400_000
FORGET_MS = 3 * 86_400_000   # Query-Order: an unfilled CANCELED/EXPIRED order is not found once 3 days old
# ccxt 4.5.84 parse_order_status() for algoStatus (Query-Algo-Order doc / ALGO_UPDATE statuses)
ALGO_STATUS = {"NEW": "open", "TRIGGERING": "open", "TRIGGERED": "closed", "FINISHED": "closed",
               "CANCELED": "canceled", "REJECTED": "rejected", "EXPIRED": "expired"}
# GET /fapi/v1/leverageBracket rows (Notional-and-Leverage-Brackets doc fields) of a 50x alt, as ccxt's own sample
# (binance.py fetch_leverage_tiers: SUSHIUSDT 50x, 0.01); cum = floor x (rate - previous rate) + previous cum
BRACKETS = ({"bracket": 1, "initialLeverage": 50, "notionalCap": 50_000, "notionalFloor": 0, "maintMarginRatio": 0.01,
             "cum": 0.0},
            {"bracket": 2, "initialLeverage": 25, "notionalCap": 250_000, "notionalFloor": 50_000,
             "maintMarginRatio": 0.02, "cum": 500.0},
            {"bracket": 3, "initialLeverage": 10, "notionalCap": 1_000_000, "notionalFloor": 250_000,
             "maintMarginRatio": 0.05, "cum": 8000.0})


def raw(symbol: str) -> str:
    return symbol.split(":")[0].replace("/", "")


class Engine(ReduceOnly):
    """State set up by FakeCcxt.__init__: sym[s] = {price, qty, entry, lev, tick, step, min, qv}; limits; stops and
    prearms (open conditional orders: id -> trigger); algo (every conditional order's meta); fills; income; markets
    (the client's loaded markets) and market_loads; brackets (per-symbol leverage brackets, default BRACKETS)."""

    # ------------------------------------------------------------ clock
    def now_ms(self) -> int:
        return int((time.time() + self.clock_s) * 1000)

    def advance(self, seconds: float) -> None:
        """Move the exchange clock: GTD limits past their goodTillDate expire on the exchange, keeping what filled
        (New-Order doc: GTD auto-cancels at goodTillDate). Orders that died unfilled are forgotten 3 days after their
        creation (Query-Order and Query-Algo-Order docs: "will not be found" when CANCELED or EXPIRED, NO filled
        trade, and created time + 3 days < current time; -2013). Only the exchange's clock moves, not the desk's."""
        self.clock_s += seconds
        for o in self.limits.values():
            if o["status"] == "open" and o["gtd"] and o["gtd"] <= self.now_ms():
                o["status"] = "expired"
        old = self.now_ms() - FORGET_MS
        for i in [i for i, o in self.limits.items() if o["status"] in ("canceled", "expired") and not o["filled"]
                  and o["ts"] < old]:
            del self.limits[i]
        for i in [i for i, a in self.algo.items() if a.get("status") in ("CANCELED", "EXPIRED") and not a.get("actual")
                  and a.get("ts", old) < old]:
            del self.algo[i]
        for s in [s for s, due in self.countdowns.items() if due and due <= self.now_ms()]:
            self.countdowns[s] = None   # countdownCancelAll fired: once, regular orders only (inferred)
            for o in self.limits.values():
                o["status"] = "canceled" if o["symbol"] == s and o["status"] == "open" else o["status"]

    # ------------------------------------------------------------ prices
    def set_price(self, s: str, p: float) -> None:
        """Walk the last (= mark) price of `s` to p. On the way up, in price order, a SELL limit fills at its price
        and a BUY STOP_MARKET triggers at price >= triggerPrice (New-Algo-Order doc) and fills at its trigger.
        At one level the limit fills before the stop, so a pre-armed stop sees the position it protects."""
        b = self.sym[s]
        old = b["price"]
        if p < old:   # down: resting BUY limits (a short's reduce-only targets) fill (fake_reduce.py)
            self._walk_down(s, old, p)
        if p > old:
            hits = [(o["price"], 0, i) for i, o in self.limits.items()
                    if o["symbol"] == s and o["status"] == "open" and o["side"] == "sell" and o["price"] <= p]
            hits += [(t, 1, i) for i, t in [*self.stops.items(), *self.prearms.items()]
                     if self._algo_sym(i) == s and t <= p]
            for level, kind, i in sorted(hits, key=lambda h: (h[0], h[1], int(h[2]))):
                b["price"] = max(level, old)
                if kind == 0 and self.limits[i]["status"] == "open":
                    self._exec_limit(i, self.limits[i]["amount"] - self.limits[i]["filled"])
                elif kind == 1 and (i in self.stops or i in self.prearms):
                    self._trigger(i)
        b["price"] = p

    def _trigger(self, i: str) -> None:
        """closePosition: buys back the WHOLE short (New-Algo-Order doc). Pre-armed reduce-only stop: buys back its
        own quantity, capped at the position; with no position it is REJECTED and nothing fills (ALGO_UPDATE
        `rm`: "Reduce Only reject"; change log 2025-11-06: no check before the conditional order triggers).
        The fill belongs to a NEW market order, not to the algo id (Query-Algo-Order doc: `actualOrderId`)."""
        s = self._algo_sym(i)
        b = self.sym[s]
        close = i in self.stops
        qty = b["qty"] if close else min(self.algo.get(i, {}).get("qty") or b["qty"], b["qty"])
        self._retire(i, "FINISHED" if qty > 0 else "REJECTED")
        if qty > 0:
            self.algo[i]["actual"] = self._record_market(s, "buy", qty, reduce=True, cid=None)

    def _record_market(self, s: str, side: str, qty: float, reduce: bool, cid: str | None) -> str:
        """A MARKET order that fills at once (and stays queryable by id or client id)."""
        i = self._next_id()
        self.market_orders[i] = {"symbol": s, "side": side, "type": "market", "price": None, "amount": qty,
                                 "filled": qty, "avg": self.sym[s]["price"], "status": "closed", "cid": cid,
                                 "reduce": reduce, "ts": self.now_ms()}
        self._fill(s, side, qty, i)
        self.market_orders[i]["last_ts"] = self.fills[-1]["timestamp"]
        return i

    def _algo_sym(self, i: str) -> str:
        return self.algo.get(i, {}).get("symbol", SYM)   # a stop a test planted directly sits on SYM

    async def fapiPrivateGetOpenAlgoOrders(self, req=None):
        """Raw GET /fapi/v1/openAlgoOrders (Current-All-Algo-Open-Orders doc: every symbol without `symbol`, weight 40).
        Fields as the official SDK's model (binance-connector-python, derivatives_trading_usds_futures
        current_all_algo_open_orders_response_inner.py): algoId an int, numbers as strings. Binance's own client id for
        an order sent without one is undocumented."""
        rows = []
        for i in [*self.stops, *self.prearms]:
            a, close = self.algo.get(i, {}), i in self.stops
            if (req or {}).get("symbol") in (None, raw(self._algo_sym(i))):
                rows.append({"algoId": int(i), "clientAlgoId": a.get("cid") or f"autogen{i}", "algoType": "CONDITIONAL",
                             "orderType": "STOP_MARKET", "symbol": raw(self._algo_sym(i)), "side": "BUY",
                             "positionSide": "BOTH", "timeInForce": "GTC", "quantity": str(a.get("qty") or 0),
                             "algoStatus": a.get("status", "NEW"), "actualOrderId": "", "actualPrice": "0",
                             "triggerPrice": str(self.stops.get(i, self.prearms.get(i))), "price": "0",
                             "workingType": "MARK_PRICE", "priceMatch": "NONE", "closePosition": close,
                             "priceProtect": False, "reduceOnly": True, "createTime": a.get("ts", 0),
                             "updateTime": a.get("ts", 0), "triggerTime": 0, "goodTillDate": 0})
        return rows

    def _retire(self, i: str, status: str) -> None:
        """A conditional order leaves the open list; its meta stays queryable (GET /fapi/v1/algoOrder)."""
        close = i in self.stops
        trig = self.stops.pop(i, None) if close else self.prearms.pop(i, None)
        a = self.algo.setdefault(i, {"symbol": SYM, "qty": None, "cid": None, "trigger": trig})
        a.update(status=status, close=close)

    # ------------------------------------------------------------ fills
    def _exec_limit(self, i: str, q: float) -> None:
        o = self.limits[i]
        px = self.sym[o["symbol"]]["price"]
        self._fill(o["symbol"], o["side"], q, i)
        o["avg"] = ((o["avg"] or 0.0) * o["filled"] + px * q) / (o["filled"] + q)
        o["filled"] += q
        o["last_ts"] = self.fills[-1]["timestamp"]
        # a reduce-only remainder expires when this fill closed the position (fake_reduce.py)
        o["status"] = "closed" if o["filled"] >= o["amount"] - 1e-12 else o["status"]

    def _fill(self, s: str, side: str, amount: float, order: str | None = None) -> None:
        """A trade at the current price, stamped by the exchange clock, as ccxt parses a userTrades row
        (Account-Trade-List doc), with its income rows (Get-Income-History doc: symbol, incomeType, income, time and
        the trade's tradeId; the same exchange time as the trade is the fake's assumption, demo probe #55 measures it)."""
        b = self.sym[s]
        px, ms, tid = b["price"], self.now_ms(), str(len(self.fills) + 1)
        fee, pnl = px * amount * FEE, (b["entry"] - px) * amount if side == "buy" else 0.0
        self.fills.append({"id": tid, "symbol": s, "order": order, "side": side, "price": px,
                           "amount": amount, "timestamp": ms, "takerOrMaker": "taker",
                           "fee": {"cost": fee, "currency": "USDT"},
                           "info": {"orderId": order, "side": side.upper(), "realizedPnl": str(pnl),
                                    "commission": str(fee), "time": ms}})
        self.income.append({"symbol": raw(s), "incomeType": "COMMISSION", "income": str(-fee), "asset": "USDT",
                            "time": ms, "tradeId": tid})
        if side == "sell":
            b["entry"] = (b["entry"] * b["qty"] + px * amount) / (b["qty"] + amount)
            b["qty"] += amount
            return
        self.income.append({"symbol": raw(s), "incomeType": "REALIZED_PNL", "income": str(pnl), "asset": "USDT",
                            "time": ms, "tradeId": tid})
        b["qty"] = round(max(0.0, b["qty"] - amount), 8)
        if not b["qty"]:
            self._ro_flat(s)
            # a closePosition stop goes with the position. SECONDARY source: testnet probes 2026-09-24
            # (github.com/bcup02/trade-alerts PR #65); a reduce-only stop stays and is rejected if it triggers
            for i in [i for i in self.stops if self._algo_sym(i) == s]:
                self._retire(i, "EXPIRED")

    async def fetch_my_trades(self, s, since=None, limit=None, params=None):
        """GET /fapi/v1/userTrades the way ccxt 4.5.84 asks for it (async_support/binance.py fetch_my_trades): since ->
        startTime, params until/endTime -> endTime, and given only a since a week old or more ccxt itself caps endTime
        at min(since + 7 d, now) by ITS clock. Account-Trade-List doc: startTime..endTime are hard bounds at most 7 days
        apart, neither = the last 7 days, limit 500 by default and 1000 at most (ccxt clamps it). Which rows a window
        holding more than `limit` answers is undocumented: the oldest here (the desk must not rely on it)."""
        p = params or {}
        end = p.get("until", p.get("endTime"))
        if since is not None and end is None and time.time() * 1000 - since >= WEEK_MS:
            end = min(since + WEEK_MS, int(time.time() * 1000))
        start = since if since is not None else (end if end is not None else self.now_ms()) - WEEK_MS
        if end is not None and end - start > WEEK_MS:
            raise err.lookup_interval_too_big()
        rows = [f for f in self.fills if f["symbol"] == s and start <= f["timestamp"] <= (end or float("inf"))]
        return rows[:min(limit or 500, 1000)]

    # ------------------------------------------------------------ raw order endpoints (demo probes, #55)
    def _by_raw(self, rid: str) -> str:
        s = next((k for k in self.sym if raw(k) == rid), None)
        if s is None:
            raise err.invalid_symbol()
        return s

    async def fapiPrivatePostCountdownCancelAll(self, req):
        """POST /fapi/v1/countdownCancelAll (Auto-Cancel-All-Open-Orders doc, weight 10): every open order of the symbol
        is cancelled once countdownTime ms pass without another call; 0 stops the timer; answers {symbol,
        countdownTime}. Inferred: regular orders only (allOpenOrders and algoOpenOrders are separate endpoints since the
        Algo migration); demo probe #55 measures whether conditional orders go too. Fires on `advance`."""
        s, ms = self._by_raw(req["symbol"]), int(req["countdownTime"])
        self.countdowns[s] = self.now_ms() + ms if ms else None
        return {"symbol": req["symbol"], "countdownTime": str(ms)}

    async def fapiPrivateGetAlgoOrder(self, req=None):
        """Raw GET /fapi/v1/algoOrder by algoId or clientAlgoId (Query-Algo-Order doc), fields as ccxt's info carries
        them. An unknown id: -2013, as the fake's fetch_order assumes (undocumented; demo probe #55 measures it)."""
        r = req or {}
        cid = r.get("clientAlgoId")
        hits = [k for k, a in self.algo.items() if (a.get("cid") == cid if cid else k == str(r.get("algoId")))]
        if not hits:
            raise err.no_such_order()
        v = self._algo_view(max(hits, key=int))
        return {"algoId": int(v["id"]), "clientAlgoId": v["clientOrderId"], "symbol": raw(v["symbol"]), **v["info"]}

    # ------------------------------------------------------------ margin
    def available(self) -> float:
        """Equity minus the isolated initial margin (notional / leverage) held by positions and resting opening
        orders. Simplified: no fees or open loss. Conditional orders hold none until they trigger."""
        used = sum(b["qty"] * b["entry"] / b["lev"] for b in self.sym.values())
        used += sum((o["amount"] - o["filled"]) * o["price"] / self.sym[o["symbol"]]["lev"]
                    for o in self.limits.values() if o["status"] == "open" and not o["reduce"])
        return self.equity - used

    def _check_margin(self, s: str, notional: float) -> None:
        if notional / self.sym[s]["lev"] > self.available() + 1e-9:
            raise err.margin_insufficient()

    def bracket(self, s: str, notional: float) -> tuple[float, float]:
        """(maintMarginRatio, cum) of the leverage bracket holding `notional` on `s`."""
        row = [r for r in self.brackets.get(s, BRACKETS) if notional >= r["notionalFloor"]][-1]
        return row["maintMarginRatio"], row["cum"]

    def isolated_wallet(self, s: str) -> float:
        """The isolated position's wallet: its initial margin Q*EP/leverage plus `wallet_adj` (margin added or removed,
        funding paid from it; a test sets it). Fees are not modelled."""
        b = self.sym[s]
        return b["qty"] * b["entry"] / b["lev"] + b.get("wallet_adj", 0.0)

    def liq_price(self, s: str) -> float:
        """The short's isolated liquidation price, Binance's one-way formula with TMM = UPNL = 0 (FAQ "How to Calculate
        Liquidation Price of USDⓈ-M Futures Contracts"): (WB + cum + Q*EP) / (Q*MMR + Q), WB = the isolated wallet
        (isolated_wallet), MMR and cum of the bracket holding the notional."""
        b = self.sym[s]
        mmr, cum = self.bracket(s, b["qty"] * b["entry"])
        return (self.isolated_wallet(s) + cum + b["qty"] * b["entry"]) / (b["qty"] * (1 + mmr))

    # ------------------------------------------------------------ markets
    async def load_markets(self, reload=False, params=None):
        """ccxt 4.5.84 (async_support/base/exchange.py load_markets_helper): the first call loads exchangeInfo, later
        ones return that snapshot unless reload=True. A perp listed since (`add_symbol`) stays unknown to the client
        until then: market() raises BadSymbol and its position comes back under the raw id (safe_market)."""
        if reload or not self.market_loads:
            self.market_loads += 1
            self.markets = {s: {"active": True} for s in self.sym}
        return self.markets

    async def fapiPrivateGetLeverageBracket(self, req=None):
        """GET /fapi/v1/leverageBracket (Notional-and-Leverage-Brackets doc, weight 1): every symbol without `symbol`.
        The doc's schema is an array or an object; this answers the array. A symbol not listed: -1121."""
        s = (req or {}).get("symbol")
        rows = [{"symbol": raw(k), "brackets": [dict(r) for r in self.brackets.get(k, BRACKETS)]}
                for k in self.sym if s in (None, raw(k))]
        if s and not rows:
            raise err.invalid_symbol()
        return rows


class ProdView:
    """`ex.public`: the keyless prod client. Market data follows the demo book unless a prod price is set
    (`fake.prod.price = x`, `fake.prod.prices[sym] = x`); private calls fail as ccxt fails without keys."""
    PRIVATE = {"create_order", "cancel_order", "cancel_all_orders", "edit_order", "fetch_order", "fetch_open_orders",
               "fetch_positions", "fetch_balance", "fetch_my_trades", "fetch_position_mode", "set_position_mode",
               "set_margin_mode", "set_leverage"}

    def __init__(self, fake):
        self._fake, self.prices = fake, {}
        self.spread = 0.0     # prod best ask - best bid, as a fraction of the price (bookTicker)
        self.tape: dict[str, list[dict]] = {}   # prod prints per symbol (aggTrades); none = one print at the price

    price = property(lambda self: self.prices.get(SYM, self._fake.price),
                     lambda self, p: self.prices.__setitem__(SYM, p))

    def _px(self, req) -> tuple[str, float]:
        s = next((k for k in self._fake.sym if raw(k) == (req or {}).get("symbol")), None)
        if s is None:
            raise err.invalid_symbol()
        return s, self.prices.get(s, self._fake.sym[s]["price"])

    def print_at(self, px: float, ms: int, buyer_maker: bool = True, s: str = SYM) -> None:
        """A prod trade at `ms` (exchange clock); buyer_maker: a seller hit the bid (aggTrades `m`)."""
        rows = self.tape.setdefault(s, [])
        rows.append({"a": len(rows) + 1, "p": str(px), "q": "1.0", "nq": "1.0", "f": len(rows) + 1, "l": len(rows) + 1,
                     "T": ms, "m": buyer_maker})
        rows.sort(key=lambda r: r["T"])

    async def fapiPublicGetTickerBookTicker(self, req=None):
        """GET /fapi/v1/ticker/bookTicker?symbol= (Symbol-Order-Book-Ticker doc, weight 2; shape as answered live
        2026-10-02): strings, `time` = the book's last update."""
        s, px = self._px(req)
        h = px * self.spread / 2
        return {"symbol": raw(s), "bidPrice": str(px - h), "bidQty": "10.0", "askPrice": str(px + h), "askQty": "10.0",
                "time": self._fake.now_ms(), "lastUpdateId": 1}

    async def fapiPublicGetPremiumIndex(self, req=None):
        """GET /fapi/v1/premiumIndex?symbol= (Mark-Price doc, weight 1; live 2026-10-02: `time` in whole seconds)."""
        s, px = self._px(req)
        return {"symbol": raw(s), "markPrice": str(px), "indexPrice": str(px), "estimatedSettlePrice": str(px),
                "lastFundingRate": "0.00010000", "interestRate": "0.00010000",
                "nextFundingTime": self._fake.now_ms() // 3_600_000 * 3_600_000 + 3_600_000,
                "time": self._fake.now_ms() // 1000 * 1000}

    async def fapiPublicGetAggTrades(self, req=None):
        """GET /fapi/v1/aggTrades (Compressed-Aggregate-Trades-List doc, weight 20): ascending from startTime,
        endTime inclusive, at most `limit` (500 default, 1000 max); startTime..endTime under 1 hour. With no tape
        set, one print at the current prod price at min(endTime, now)."""
        s, px = self._px(req)
        a, b = req.get("startTime"), req.get("endTime")
        if a is not None and b is not None and b - a >= 3_600_000:
            raise err.lookup_interval_too_big()
        rows = self.tape.get(s)
        if rows is None:
            t = min(b if b is not None else self._fake.now_ms(), self._fake.now_ms())
            rows = [{"a": 1, "p": str(px), "q": "1.0", "nq": "1.0", "f": 1, "l": 1, "T": t, "m": True}]
        rows = [r for r in rows if (a is None or r["T"] >= a) and (b is None or r["T"] <= b)]
        return [dict(r) for r in rows[:min(int(req.get("limit") or 500), 1000)]]

    async def fetch_ticker(self, s):   # through the fake's own method, so a test's stub of it still applies
        t = await self._fake.fetch_ticker(s)
        return {**t, "last": self.prices[s]} if s in self.prices else t

    async def fetch_ohlcv(self, s, tf="1h", since=None, limit=30):
        if s not in self.prices:
            return await self._fake.fetch_ohlcv(s, tf, since=since, limit=limit)
        px = self.prices[s]
        return [[k, px, px * 1.02, px * 0.98, px, 1] for k in range(limit)]

    def __getattr__(self, name):
        if name in self.PRIVATE or name.startswith(("fapiPrivate", "private")):
            async def denied(*a, **k):
                raise err.no_api_key()
            return denied
        return getattr(self._fake, name)
