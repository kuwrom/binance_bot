"""The exchange side of the fake (mixed into FakeCcxt, fakes.py): prices that move, resting orders that fill or
trigger on their own, GTD expiry, isolated margin, fills and income. Short-only: the book only ever holds SELL
limits above the market and BUY stops above the market, so only an upward move can fill or trigger anything.
Also ProdView: the separate keyless prod client (`ex.public`), whose prices can diverge from the demo book."""

from __future__ import annotations

import time

from tests import fake_errors as err

SYM = "PUMP/USDT:USDT"
FEE = 0.0005
FORGET_MS = 3 * 86_400_000   # Query-Order: an unfilled CANCELED/EXPIRED order is not found once 3 days old
# ccxt 4.5.84 parse_order_status() for algoStatus (Query-Algo-Order doc / ALGO_UPDATE statuses)
ALGO_STATUS = {"NEW": "open", "TRIGGERING": "open", "TRIGGERED": "closed", "FINISHED": "closed",
               "CANCELED": "canceled", "REJECTED": "rejected", "EXPIRED": "expired"}


def raw(symbol: str) -> str:
    return symbol.split(":")[0].replace("/", "")


class Engine:
    """State set up by FakeCcxt.__init__: sym[s] = {price, qty, entry, lev, tick, step, min, qv}; limits; stops and
    prearms (open conditional orders: id -> trigger); algo (every conditional order's meta); fills; income."""

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

    # ------------------------------------------------------------ prices
    def set_price(self, s: str, p: float) -> None:
        """Walk the last (= mark) price of `s` to p. On the way up, in price order, a SELL limit fills at its price
        and a BUY STOP_MARKET triggers at price >= triggerPrice (New-Algo-Order doc) and fills at its trigger.
        At one level the limit fills before the stop, so a pre-armed stop sees the position it protects."""
        b = self.sym[s]
        old = b["price"]
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
        o["status"] = "closed" if o["filled"] >= o["amount"] - 1e-12 else "open"

    def _fill(self, s: str, side: str, amount: float, order: str | None = None) -> None:
        """A trade at the current price, stamped by the exchange clock, as ccxt parses a userTrades row
        (Account-Trade-List doc), with its income rows (Get-Income-History doc: symbol, incomeType, income)."""
        b = self.sym[s]
        px, ms = b["price"], self.now_ms()
        fee, pnl = px * amount * FEE, (b["entry"] - px) * amount if side == "buy" else 0.0
        self.fills.append({"id": str(len(self.fills) + 1), "symbol": s, "order": order, "side": side, "price": px,
                           "amount": amount, "timestamp": ms, "takerOrMaker": "taker",
                           "fee": {"cost": fee, "currency": "USDT"},
                           "info": {"orderId": order, "side": side.upper(), "realizedPnl": str(pnl),
                                    "commission": str(fee), "time": ms}})
        self.income.append({"symbol": raw(s), "incomeType": "COMMISSION", "income": str(-fee), "asset": "USDT",
                            "time": ms})
        if side == "sell":
            b["entry"] = (b["entry"] * b["qty"] + px * amount) / (b["qty"] + amount)
            b["qty"] += amount
            return
        self.income.append({"symbol": raw(s), "incomeType": "REALIZED_PNL", "income": str(pnl), "asset": "USDT",
                            "time": ms})
        b["qty"] = round(max(0.0, b["qty"] - amount), 8)
        if not b["qty"]:
            # a closePosition stop goes with the position. SECONDARY source: testnet probes 2026-09-24
            # (github.com/bcup02/trade-alerts PR #65); a reduce-only stop stays and is rejected if it triggers
            for i in [i for i in self.stops if self._algo_sym(i) == s]:
                self._retire(i, "EXPIRED")

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


class ProdView:
    """`ex.public`: the keyless prod client. Market data follows the demo book unless a prod price is set
    (`fake.prod.price = x`, `fake.prod.prices[sym] = x`); private calls fail as ccxt fails without keys."""
    PRIVATE = {"create_order", "cancel_order", "cancel_all_orders", "edit_order", "fetch_order", "fetch_open_orders",
               "fetch_positions", "fetch_balance", "fetch_my_trades", "fetch_position_mode", "set_position_mode",
               "set_margin_mode", "set_leverage"}

    def __init__(self, fake):
        self._fake, self.prices = fake, {}

    price = property(lambda self: self.prices.get(SYM, self._fake.price),
                     lambda self, p: self.prices.__setitem__(SYM, p))

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
