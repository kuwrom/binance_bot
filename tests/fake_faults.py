"""Fault injection on top of the strict fake (fakes.py), for the #46 plumbing suite: tests/test_fault_matrix*.py,
test_duplicate_fills.py, test_crash_restart.py.

- `FaultyCcxt.arm(method, fault, kind)`: ONE call of create_order, cancel_order, edit_order, fetch_order or
  fetch_my_trades meets one of FAULTS, picked by the client-id kind of the order it concerns (clientid.kind_of: open, entry, stop, bridge, prearm,
  close, target; "any" matches every call). Each fault answers the exact error ccxt 4.5.84 raises for it
  (fake_errors.py, checked against ccxt's own handler in test_errors.py):
  - 1007_ran / 1007_unsent: -1007 "execution status unknown" (error-code page), after the request ran / without it
  - 503_unknown: HTTP 503 "Unknown error..." after it ran (general-info doc, case A: status unknown)
  - 503_unavailable: HTTP 503 "Service Unavailable." (case B: a failure, nothing ran)
  - 1008: -1008 throttled (case C: a failure, nothing ran; reduce-only and close-position orders are exempt on Binance)
  - 1021_after: -1021 after the request ran (seen live on a cancel: ONDO #5)
  A send that did not run still reached Binance: it is recorded in `orders` as the base fake records a refusal.
  MISSES, armed like a fault but kept out of FAULTS (and so out of the matrix): 2013_resting, a fetch_order of a
  conditional order (Query Algo Order) answers -2013 "Order does not exist." while the order still rests and is listed
  (demo probe 8, 2026-10-04 13:30Z: 1 of 4 lookups by clientAlgoId missed; test_algo_lookup_miss.py)
- `dup_trades` / `dup_income`: every userTrades / income answer carries each row twice (a page served twice).
- `View(fake)`: one process's client on the shared exchange. `crash_after(method, kind)`: that call runs on the
  exchange, then the process dies (Crash, a BaseException the desk never catches, as kill -9 leaves no handler to run):
  every later call of the view raises Crash, and so does every journal write of the journal it was given (`kills`).
  A new Trader / Guardian on a new View and a new Journal connection then sees the same book and journal file."""

from __future__ import annotations

import inspect
from typing import Any

from shortbot import clientid
from tests import fake_errors as err
from tests.fake_engine import ProdView
from tests.fakes import FakeCcxt

FAULTS = ("1007_ran", "1007_unsent", "503_unknown", "503_unavailable", "1008", "1021_after")
RAN = ("1007_ran", "503_unknown", "1021_after")   # the request executed, then its answer was lost or refused
MISSES = ("2013_resting",)   # a conditional order's lookup answers not-found while it rests (fetch_order only)
METHODS = {"create_order": "POST", "cancel_order": "DELETE", "edit_order": "PUT", "fetch_order": "GET",
           "fetch_my_trades": "GET"}


def fault_error(fault: str, method: str = "create_order", algo: bool = False) -> Exception:
    """The error ccxt raises for `fault` on `method` (algo: the Algo service's endpoint)."""
    path = {"fetch_my_trades": "userTrades"}.get(method, "algoOrder" if algo else "order")
    if fault in ("1007_ran", "1007_unsent"):
        return err.status_unknown()
    if fault in ("503_unknown", "503_unavailable"):
        return err.unavailable(path, fault == "503_unknown", METHODS[method])
    if fault == "1008":
        return err.throttled()
    if fault == "1021_after":
        return err.clock_drift()
    if fault == "2013_resting":
        return err.no_such_order()
    raise ValueError(f"unknown fault {fault!r}")


def kind_of_id(fake: FakeCcxt, i: Any) -> str:
    """The client-id kind of an order id on `fake` (a limit, a market order or a conditional one)."""
    o = fake.limits.get(str(i)) or fake.market_orders.get(str(i)) or fake.algo.get(str(i)) or {}
    return clientid.kind_of(o.get("cid") or "")


def _cid(params: dict | None) -> str | None:
    p = params or {}
    return p.get("clientOrderId") or p.get("clientAlgoId") or p.get("newClientOrderId") or p.get("origClientOrderId")


class FaultyCcxt(FakeCcxt):
    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self.armed: list[dict[str, Any]] = []
        self.fired: list[tuple[str, str, str]] = []   # (method, kind, fault) as they fired
        self.dup_trades = self.dup_income = False

    def arm(self, method: str, fault: str, kind: str = "any", nth: int = 1) -> None:
        """The nth call of `method` about an order of client-id `kind` meets `fault`, once."""
        assert method in METHODS and (fault in FAULTS or fault in MISSES and method == "fetch_order"), (method, fault)
        self.armed.append({"method": method, "fault": fault, "kind": kind, "left": nth})

    def _take(self, method: str, kind: str) -> str | None:
        for a in self.armed:
            if a["method"] == method and a["kind"] in ("any", kind):
                a["left"] -= 1
                if a["left"] == 0:
                    self.armed.remove(a)
                    self.fired.append((method, kind, a["fault"]))
                    return str(a["fault"])
                return None
        return None

    # ------------------------------------------------------------ order calls
    async def create_order(self, s, type_, side, amount, price=None, params=None):
        p = params or {}
        f = self._take("create_order", clientid.kind_of(_cid(p) or ""))
        if f is None:
            return await super().create_order(s, type_, side, amount, price, params)
        e = fault_error(f, "create_order", algo=any(p.get(k) is not None for k in ("triggerPrice", "stopPrice")))
        if f not in RAN:   # refused before it ran, through the base fake's own refusal path (recorded, audited)
            self.refuse.insert(0, e)
            return await super().create_order(s, type_, side, amount, price, params)
        await super().create_order(s, type_, side, amount, price, params)
        raise e

    async def cancel_order(self, i, s, params=None):
        trig = any((params or {}).get(k) for k in ("stop", "trigger", "conditional"))
        f = self._take("cancel_order", kind_of_id(self, i))
        if f is None:
            return await super().cancel_order(i, s, params)
        e = fault_error(f, "cancel_order", algo=trig)
        if f in RAN:
            await super().cancel_order(i, s, params)
        raise e

    async def edit_order(self, i, s, type_, side, amount, price, params=None):
        """Modify Order (PUT /fapi/v1/order): one that ran is applied, then its answer is lost or refused."""
        f = self._take("edit_order", kind_of_id(self, i))
        if f is None:
            return await super().edit_order(i, s, type_, side, amount, price, params)
        e = fault_error(f, "edit_order")
        if f in RAN:
            await super().edit_order(i, s, type_, side, amount, price, params)
        raise e

    async def fetch_order(self, i, s, params=None):
        cid = _cid(params)
        f = self._take("fetch_order", clientid.kind_of(cid) if cid else kind_of_id(self, i))
        if f is not None:
            assert f not in MISSES or (params or {}).get("trigger"), f"{f}: a Query Algo Order answer"
            raise fault_error(f, "fetch_order", algo=bool((params or {}).get("trigger")))
        return await super().fetch_order(i, s, params)

    # ------------------------------------------------------------ history
    async def fetch_my_trades(self, s, since=None, limit=None, params=None):
        f = self._take("fetch_my_trades", "any")
        if f is not None:
            raise fault_error(f, "fetch_my_trades")
        rows = await super().fetch_my_trades(s, since, limit, params)
        return [dict(x) for r in rows for x in (r, r)] if self.dup_trades else rows

    async def fapiPrivateGetIncome(self, req=None):
        rows = await super().fapiPrivateGetIncome(req)
        return [dict(x) for r in rows for x in (r, r)] if self.dup_income else rows


# ------------------------------------------------------------ processes (kill -9)
class Crash(BaseException):
    """The process died here (kill -9): nothing of it runs on."""


class View:
    """One process's client over the shared exchange `fake`. Attributes are the fake's (one book); calls go through
    while the process lives."""

    def __init__(self, fake: FakeCcxt, name: str = "proc") -> None:
        self._fake, self.name, self.dead = fake, name, False
        self._crash: dict[str, Any] | None = None
        self._kills: list[Any] = []
        self.prod = ProdView(self)   # the process's keyless prod client dies with it too

    def kills(self, journal: Any) -> View:
        """`journal` (this process's connection) writes nothing once the process is dead."""
        self._kills.append(journal)
        return self

    def crash_after(self, method: str, kind: str = "any", nth: int = 1) -> None:
        """The nth call of `method` about an order of client-id `kind` runs on the exchange; then the process dies."""
        self._crash = {"method": method, "kind": kind, "left": nth}

    def die(self) -> None:
        self.dead = True
        for j in self._kills:
            j.w._q.clear()   # its queued writes are lost with it

            def dead(*a: Any, **k: Any) -> Any:
                raise Crash(f"{self.name} is dead: journal write refused")
            j.w._unit = dead

    def _kind(self, method: str, a: tuple, k: dict) -> str:
        f = self._fake
        if method == "create_order":
            params = a[5] if len(a) > 5 else k.get("params")
            return clientid.kind_of(_cid(params) or "")
        if method == "edit_order":
            return kind_of_id(f, a[0])
        if method in ("cancel_order", "fetch_order"):
            params = a[2] if len(a) > 2 else k.get("params")
            cid = _cid(params)
            return clientid.kind_of(cid) if cid else kind_of_id(f, a[0])
        return "any"

    def _due(self, method: str, a: tuple, k: dict) -> bool:
        c = self._crash
        if c is None or c["method"] != method or c["kind"] not in ("any", self._kind(method, a, k)):
            return False
        c["left"] -= 1
        if c["left"]:
            return False
        self._crash = None
        return True

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._fake, name)
        if not callable(attr):
            return attr
        if inspect.iscoroutinefunction(attr):
            async def call(*a: Any, **k: Any) -> Any:
                if self.dead:
                    raise Crash(f"{self.name} is dead: {name} never sent")
                out = await attr(*a, **k)
                if self._due(name, a, k):
                    self.die()
                    raise Crash(f"{self.name} killed after {name} ran")
                return out
            return call

        def sync(*a: Any, **k: Any) -> Any:
            if self.dead:
                raise Crash(f"{self.name} is dead: {name}")
            return attr(*a, **k)
        return sync
