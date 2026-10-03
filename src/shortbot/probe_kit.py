"""The probes' toolkit (#55, probes.py). Every exchange call a probe makes goes through `Probe.call`, which records it:
label, time, the raw answer (a ccxt order or position keeps its unified essentials and Binance's own `info`), or the
error class and Binance code. Orders carry probe client ids (sb-x<number><base>-<nonce>, clientid.KINDS) and are
remembered, so the cleanup cancels what the probe placed. Sizes are the symbol's minimum: MIN_NOTIONAL x 1.05 at the
last price on the step grid (reduce-only orders are exempt from MIN_NOTIONAL, so a stop for the position fits too)."""

from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime, timezone
from typing import Any

from . import clientid, errors
from . import killswitch as ks
from .exchange import to_raw
from .stops import is_close_position

LEVERAGE = 2
SAFETY_PCT = 10.0      # every probe position gets a closePosition stop this far above the price, at once
RESP_CHARS = 4000      # a recorded answer is cut here: the journal row stays readable
TRIG = {"trigger": True}
PROBE = f"{clientid.PREFIX}-{clientid.KINDS['probe']}"   # client id prefix of every probe order
_KEEP = ("id", "clientOrderId", "symbol", "status", "type", "side", "amount", "filled", "price", "average", "last",
         "triggerPrice", "reduceOnly", "timestamp", "lastTradeTimestamp", "contracts", "entryPrice", "markPrice")


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)   # tests move the fake's exchange clock instead


class ProbeError(RuntimeError):
    """A call the probe can't go on without failed (its error class is in the message): the cleanup runs."""


def mask(v: Any) -> str:
    """A secret (the listenKey) as recorded: its first 4 characters and its length."""
    s = str(v or "")
    return f"{s[:4]}…({len(s)} chars)" if s else s


def raw(res: Any, secret: tuple[str, ...] = ()) -> Any:
    if isinstance(res, list):
        return [raw(x, secret) for x in res]
    if isinstance(res, dict):
        if isinstance(res.get("info"), dict):
            res = {**{k: res[k] for k in _KEEP if k in res}, "info": res["info"]}
        res = {k: mask(v) if k in secret else v for k, v in res.items()}
    s = json.dumps(res, default=str)
    return res if len(s) <= RESP_CHARS else s[:RESP_CHARS] + "…"


def outcome(ok: bool, res: Any) -> dict[str, Any]:
    """{"ok": True} or {"ok": False, "error": class, "code": Binance code, "msg"}: what a call answered."""
    if ok:
        return {"ok": True}
    return {"ok": False, "error": type(res).__name__, "code": errors.code(res), "msg": str(res)[:300]}


def say(ok: bool, res: Any) -> str:
    """One phrase for a verdict: 'accepted' or 'OrderNotFound -2011'."""
    if ok:
        return "accepted"
    c = errors.code(res)
    return f"{type(res).__name__}{f' {c}' if c is not None else ''}"


def algo_status(q: Any) -> str:
    """A conditional order's raw algoStatus (Query-Algo-Order), else ccxt's status."""
    q = q if isinstance(q, dict) else {}
    return str((q.get("info") or {}).get("algoStatus") or q.get("status") or "")


class Probe:
    def __init__(self, tr, symbol: str, name: str, number: str):
        self.tr, self.t, self.sym, self.raw = tr, tr.t, symbol, to_raw(symbol)
        self.name, self.number = name, number
        self.calls: list[dict[str, Any]] = []
        self.placed: dict[str, set[str]] = {"order": set(), "algo": set()}
        self.countdown = False          # countdownCancelAll armed: the cleanup turns it off
        self.safety: str | None = None  # the probe position's closePosition stop
        self.qty = self.price = self.tick = 0.0

    # ------------------------------------------------------------ recording
    async def call(self, label: str, fn, *args, secret: tuple[str, ...] = (), **kw) -> tuple[bool, Any]:
        row: dict[str, Any] = {"call": label, "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
        t0 = time.monotonic()
        try:
            res = await fn(*args, **kw)
        except Exception as e:  # noqa: BLE001 - recorded: the error is the finding
            self.calls.append({**row, **outcome(False, e), "ms": round((time.monotonic() - t0) * 1000)})
            return False, e
        self.calls.append({**row, "ok": True, "ms": round((time.monotonic() - t0) * 1000), "resp": raw(res, secret)})
        return True, res

    async def need(self, label: str, fn, *args, **kw) -> Any:
        ok, res = await self.call(label, fn, *args, **kw)
        if not ok:
            raise ProbeError(f"{label}: {type(res).__name__}: {res}") from res
        return res

    def cid(self) -> str:
        return clientid.new("probe", f"{self.number}{clientid.tag(self.sym)}")

    def ours(self, o: dict, algo: bool) -> bool:
        return str(o.get("id")) in self.placed["algo" if algo else "order"] or str(
            o.get("clientOrderId") or "").startswith(PROBE)

    # ------------------------------------------------------------ sizes and prices
    async def setup(self) -> None:
        tk = await self.need("ticker", self.t.fetch_ticker, self.sym)
        m = self.t.market(self.sym)
        self.price, self.tick = float(tk["last"]), float(m["precision"]["price"])
        step = float(m["precision"]["amount"])
        min_cost = float(((m.get("limits") or {}).get("cost") or {}).get("min") or 5.0)
        steps = math.ceil(round(min_cost * 1.05 / self.price / step, 6))
        self.qty = float(self.t.amount_to_precision(self.sym, steps * step))

    def px(self, pct: float) -> float:
        return float(self.t.price_to_precision(self.sym, self.price * (1 + pct / 100)))

    async def mark(self) -> float:
        res = await self.need("mark price (premiumIndex)", self.t.fapiPublicGetPremiumIndex, {"symbol": self.raw})
        return float(res["markPrice"])

    # ------------------------------------------------------------ orders
    async def order(self, label: str, type_: str, side: str, amount: float | None, price: float | None = None,
                    params: dict | None = None, algo: bool = False) -> tuple[bool, Any, str]:
        if side == "sell" and not (params or {}).get("reduceOnly"):
            self.still_active()   # an opening order (a SELL limit may fill): the kill switch again
        cid = self.cid()
        ok, res = await self.call(label, self.t.create_order, self.sym, type_, side, amount, price,
                                  {**(params or {}), "clientOrderId": cid})
        if ok and isinstance(res, dict) and res.get("id") is not None:
            self.placed["algo" if algo else "order"].add(str(res["id"]))
        return ok, res, cid

    async def stop(self, label: str, trigger: float, close: bool = True, qty: float | None = None,
                   working: str = "MARK_PRICE") -> tuple[bool, Any, str]:
        """A BUY STOP_MARKET (Algo service): closePosition, or reduce-only for `qty` (a pre-arm's shape)."""
        params = {"triggerPrice": trigger, "workingType": working,
                  **({"closePosition": True} if close else {"reduceOnly": True})}
        return await self.order(label, "STOP_MARKET", "buy", None if close else qty, None, params, algo=True)

    async def limit(self, label: str, side: str, price: float, qty: float, reduce: bool = False):
        params = {"timeInForce": "GTC", **({"reduceOnly": True} if reduce else {})}
        return await self.order(label, "limit", side, qty, price, params)

    async def cancel(self, label: str, oid: str, algo: bool = True) -> dict[str, Any]:
        ok, res = await self.call(label, self.t.cancel_order, oid, self.sym, TRIG if algo else {})
        return outcome(ok, res)

    async def algo_open(self, label: str) -> list[dict] | None:
        ok, res = await self.call(label, self.t.fetch_open_orders, self.sym, params=TRIG)
        return res if ok else None

    async def position(self, label: str) -> dict | None:
        return await self.need(label, self.tr.position, self.sym)

    def still_active(self) -> None:
        """Before every opening order: a trip from another process since the start stops the probe (cleanup runs)."""
        if no := ks.state_refusal(self.tr.j, f"probe {self.name}"):
            raise ProbeError(no["error"])

    async def prepare(self) -> None:
        await self.need(f"prepare: one-way, isolated, {LEVERAGE}x", self.tr.prepare, self.sym, LEVERAGE)

    async def open_short(self) -> float:
        """A minimum short through the desk's own primitive (open_market: the client id settles an ambiguous answer),
        then at once a closePosition safety stop at +SAFETY_PCT. Without one resting the probe stops (cleanup closes)."""
        self.still_active()
        await self.prepare()
        await self.need("open: market SELL (desk open_market)", self.tr.open_market, self.sym, self.qty, self.cid())
        pos = await self.tr.verified_position(self.sym)
        if not pos:
            raise ProbeError("the short isn't visible after its fill")
        ok, s, _ = await self.stop(f"safety stop: closePosition +{SAFETY_PCT:g}%", self.px(SAFETY_PCT))
        if ok:
            self.safety = str(s["id"])
        elif not any(is_close_position(o) for o in await self.algo_open("safety stop refused: what rests?") or []):
            raise ProbeError(f"no safety stop: {say(ok, s)}")
        return float(pos["contracts"])

    async def watch_algo(self, ids: dict[str, str], delays=(0.5, 1, 2, 4, 8)) -> dict[str, dict[str, Any]]:
        """After the position went flat: how long each conditional order stayed on the open list (None: still there
        at the end), and its final algoStatus."""
        out: dict[str, dict[str, Any]] = {k: {"gone_after_s": None, "status": None} for k in ids}
        waited = 0.0
        for d in delays:
            await _sleep(d)
            waited += d
            listed = await self.algo_open(f"open conditional orders {waited:g} s after flat")
            if listed is None:
                continue
            here = {str(o.get("id")) for o in listed}
            for k, i in ids.items():
                if out[k]["gone_after_s"] is None and i not in here:
                    out[k]["gone_after_s"] = waited
            if all(v["gone_after_s"] is not None for v in out.values()):
                break
        for k, i in ids.items():
            ok, q = await self.call(f"query {k} stop {i}", self.t.fetch_order, i, self.sym, TRIG)
            out[k]["status"] = algo_status(q) if ok else say(ok, q)
        return out
