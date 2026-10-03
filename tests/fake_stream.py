"""Binance's USDⓈ-M user-data stream for the fake (fakes.py), #54: what a connection on
<host>/private/ws?listenKey=K pushes when the account's orders and positions change.

`Feed(fake).poll()` diffs the fake's books (limits, market orders, conditional orders, positions) against its last poll
and returns the events Binance pushes for each change. Shapes: the payloads demo pushed (probe_result 387, 2026-10-02:
ORDER_TRADE_UPDATE NEW and CANCELED of a limit, ALGO_UPDATE NEW and CANCELED of a reduce-only stop) and the official
SDK's models (binance-connector-python, derivatives_trading_usds_futures websocket_streams/models:
order_trade_update_o.py, algo_update_o.py, account_update_a.py, listen_key_expired.py):
- a limit or market order: NEW; one TRADE per fill (`l` the fill, `z` the total; X PARTIALLY_FILLED or FILLED); then
  CANCELED or EXPIRED when it leaves the book unfilled or part-filled;
- a conditional order: NEW; a trigger: TRIGGERING, TRIGGERED (`ai`: the order it fills under), FINISHED (status
  meanings: Binance.Net Enums/AlgoOrderStatus.cs); CANCELED; EXPIRED (a closePosition stop at flat, probe_result 390);
  REJECTED with `rm` "Reduce Only reject" (the ccxt 4.5.84 docstring's sample);
- the triggered order's own client id is undocumented: the fake's `autogen<i>`, as fapiPrivateGetOpenOrders;
- a position change: ACCOUNT_UPDATE, reason ORDER, `pa` negative for a short.
Not modelled: TRADE_LITE, MARGIN_CALL, ACCOUNT_CONFIG_UPDATE (the guardian only counts them).

`Hub.connect(url)` stands in for websockets.connect as user_stream.py awaits it; a `Socket` pushes only while its URL
holds the account's active key (`fake.listen_key`): a made-up or deleted key still connects on `/private` and stays
silent (measured 2026-10-02, probe_stream.py). Faults: `drop` (a predicate: lose the event), `dup` (each twice),
`reverse` (each batch in reverse order), `silent` (nothing pushed; changes meanwhile are lost, as on Binance),
`fail_connect` (refusals), `hang_connect`, `close_all()` (the server drops every connection)."""

from __future__ import annotations

import asyncio
import json

from tests.fake_engine import SYM, raw

DONE = {"canceled": "CANCELED", "expired": "EXPIRED"}


def order_event(fake, i: str, x: str, status: str, last: float = 0.0) -> dict:
    o = fake.limits.get(i) or fake.market_orders[i]
    ms, limit = fake.now_ms(), o["type"] == "limit"
    return {"e": "ORDER_TRADE_UPDATE", "T": ms, "E": ms + 1, "o": {
        "s": raw(o["symbol"]), "c": o["cid"] or f"autogen{i}", "S": o["side"].upper(),
        "o": "LIMIT" if limit else "MARKET", "f": "GTD" if o.get("gtd") else "GTC", "q": str(o["amount"]),
        "p": str(o["price"] or 0), "ap": str(o["avg"] or 0), "sp": "0", "x": x, "X": status, "i": int(i),
        "l": str(last), "z": str(o["filled"]), "L": str(o["avg"] or 0) if last else "0", "n": "0", "N": "USDT",
        "T": ms, "t": 0, "b": "0", "a": "0", "m": limit, "R": o["reduce"], "wt": "CONTRACT_PRICE", "ot": "LIMIT" if limit else "MARKET", "ps": "BOTH",
        "cp": False, "rp": "0", "pP": False, "si": 0, "ss": 0, "V": "EXPIRE_MAKER", "pm": "NONE",
        "gtd": o.get("gtd") or 0, "er": "0"}}


def algo_event(fake, i: str, status: str) -> dict:
    a, ms = fake.algo.get(i, {}), fake.now_ms()
    trig = fake.stops.get(i, fake.prearms.get(i, a.get("trigger")))
    o = {"caid": a.get("cid") or f"autogen{i}", "aid": int(i), "at": "CONDITIONAL", "o": "STOP_MARKET",
         "s": raw(a.get("symbol", SYM)), "S": "BUY", "ps": "BOTH", "f": "GTC", "q": str(a.get("qty") or 0), "X": status,
         "ai": str(a.get("actual") or "") if status in ("TRIGGERED", "FINISHED") else "", "tp": str(trig), "p": "0",
         "V": "EXPIRE_MAKER", "wt": "MARK_PRICE", "pm": "NONE", "cp": bool(i in fake.stops or a.get("close")),
         "pP": False, "R": True, "tt": 0, "gtd": 0, "ia": False}
    if status == "REJECTED":
        o["rm"] = "Reduce Only reject"
    return {"e": "ALGO_UPDATE", "T": ms, "E": ms + 1, "o": o}


def account_event(fake, s: str) -> dict:
    b, ms = fake.sym[s], fake.now_ms()
    return {"e": "ACCOUNT_UPDATE", "E": ms + 1, "T": ms, "a": {
        "m": "ORDER", "B": [{"a": "USDT", "wb": str(fake.equity), "cw": str(fake.equity), "bc": "0"}],
        "P": [{"s": raw(s), "pa": str(-b["qty"]), "ep": str(b["entry"] if b["qty"] else 0), "cr": "0", "up": "0",
               "mt": "isolated", "iw": "0", "ps": "BOTH"}]}}


def expired_event(fake, key: str) -> dict:
    return {"e": "listenKeyExpired", "E": fake.now_ms(), "listenKey": key}


def foreign_order(fake, s: str = SYM, cid: str = "web_8xUe2Ab", status: str = "NEW") -> dict:
    """An order placed outside the desk (the Binance app or web): Binance's own client id."""
    ms = fake.now_ms()
    return {"e": "ORDER_TRADE_UPDATE", "T": ms, "E": ms + 1, "o": {
        "s": raw(s), "c": cid, "S": "SELL", "o": "LIMIT", "f": "GTC", "q": "10", "p": "1.2", "ap": "0", "sp": "0",
        "x": "NEW" if status == "NEW" else "TRADE", "X": status, "i": 990001, "l": "0", "z": "0", "L": "0", "T": ms,
        "t": 0, "m": False, "R": False, "ot": "LIMIT", "ps": "BOTH", "cp": False, "rp": "0"}}


class Feed:
    """Changes since the last poll, as events (the fake's state at construction is the baseline)."""

    def __init__(self, fake):
        self.fake = fake
        self.orders = {i: (o["status"], o["filled"]) for i, o in self._orders()}
        self.algos = {i: self._algo(i) for i in self._algo_ids()}
        self.qty = {s: b["qty"] for s, b in fake.sym.items()}

    def _orders(self):
        return sorted([*self.fake.limits.items(), *self.fake.market_orders.items()], key=lambda kv: int(kv[0]))

    def _algo_ids(self):
        return sorted({*self.fake.algo, *self.fake.stops, *self.fake.prearms}, key=int)

    def _algo(self, i: str) -> str:
        return self.fake.algo.get(i, {}).get("status", "NEW")

    def poll(self) -> list[dict]:
        out = []
        for i in self._algo_ids():
            st, was = self._algo(i), self.algos.get(i)
            if st == was:
                continue
            if was is None:
                out.append(algo_event(self.fake, i, "NEW"))
            if st == "FINISHED" and was != "TRIGGERED":
                out += [algo_event(self.fake, i, "TRIGGERING"), algo_event(self.fake, i, "TRIGGERED")]
            if st != "NEW":
                out.append(algo_event(self.fake, i, st))
            self.algos[i] = st
        for i, o in self._orders():
            now, was = (o["status"], o["filled"]), self.orders.get(i)
            if now == was:
                continue
            if was is None:
                out.append(order_event(self.fake, i, "NEW", "NEW"))
                was = ("open", 0.0)
            if o["filled"] > was[1]:
                st = "FILLED" if o["status"] == "closed" else "PARTIALLY_FILLED"
                out.append(order_event(self.fake, i, "TRADE", st, o["filled"] - was[1]))
            if o["status"] in DONE and was[0] != o["status"]:
                out.append(order_event(self.fake, i, DONE[o["status"]], DONE[o["status"]]))
            self.orders[i] = now
        for s, b in self.fake.sym.items():
            if b["qty"] != self.qty.get(s, 0.0):
                out.append(account_event(self.fake, s))
                self.qty[s] = b["qty"]
        return out


class Socket:
    def __init__(self, hub: Hub, url: str):
        self.hub, self.url, self.feed = hub, url, Feed(hub.fake)
        self.queue: list[dict] = []
        self.closed = False

    def _valid(self) -> bool:
        key = self.hub.fake.listen_key
        return bool(key) and self.url.endswith(f"listenKey={key}")

    def _pull(self) -> None:
        batch = self.feed.poll()   # always: what happens while silent is lost, as on Binance
        if self.hub.silent or not self._valid():
            return
        if self.hub.reverse:
            batch.reverse()
        for ev in batch:
            if self.hub.drop and self.hub.drop(ev):
                self.hub.dropped.append(ev)
                continue
            self.queue += [ev, ev] if self.hub.dup else [ev]

    async def recv(self) -> str:
        while True:
            if self.closed:
                raise ConnectionError("fake: the server closed the connection")
            if not self.queue:
                self._pull()
            if self.queue:
                ev = self.queue.pop(0)
                self.hub.delivered.append(ev)
                return json.dumps(ev)
            await asyncio.sleep(0.002)

    async def close(self) -> None:
        self.closed = True


class Hub:
    def __init__(self, fake):
        self.fake = fake
        self.urls: list[str] = []
        self.sockets: list[Socket] = []
        self.delivered: list[dict] = []
        self.dropped: list[dict] = []
        self.drop = None
        self.dup = self.reverse = self.silent = self.hang_connect = False
        self.fail_connect = 0

    async def connect(self, url: str) -> Socket:
        self.urls.append(url)
        if self.hang_connect:
            await asyncio.Event().wait()
        if self.fail_connect:
            self.fail_connect -= 1
            raise ConnectionRefusedError("fake: connection refused")
        s = Socket(self, url)
        self.sockets.append(s)
        return s

    def push(self, ev: dict) -> None:
        """A frame every open connection receives as is (listenKeyExpired, a foreign order)."""
        for s in self.sockets:
            if not s.closed:
                s.queue.append(ev)

    def close_all(self) -> None:
        for s in self.sockets:
            s.closed = True

    def live(self) -> list[Socket]:
        return [s for s in self.sockets if not s.closed]
