"""Guarded order placement. Short-only: the only opening order is a market SELL, always followed by an
exchange-side stop-market (closePosition, mark price). Safety properties:
- a journal row ('opening') exists BEFORE the entry order, so crashes mid-entry are recoverable
- fills are verified from the live position; the stop is placed with retries; if it can't be placed
  the position is closed (with retries) and an urgent alert fires if even that fails
- stop swaps roll back to the old stop on failure; stops only ever tighten
- scanner vetoes + market regime are re-checked server-side; overriding them needs a logged reason
- every order-changing sequence on a symbol holds that symbol's lock (symlock.py): one at a time across the
  guardian, the MCP servers and CLI calls on this host. Opening also holds `lock`, this process's one asyncio lock
  (its portfolio checks), and always takes it first

This module: account, positions and the order primitives. The flows are mixed in from trading_open.py (open_short)
and trading_manage.py (close, move_stop, ensure_stop).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import ccxt.async_support as ccxt

from . import reconcile
from .exchange import Exchanges
from .journal import Journal
from .risk import AccountState
from .stops import STOP_TYPES, is_clock_drift, is_protective_stop, swap_stop  # noqa: F401 (re-export)
from .symlock import SymbolLocks, lock_dir_for
from .trading_manage import ManageFlow
from .trading_open import OpenFlow

STOP_PARAMS = {"closePosition": True, "workingType": "MARK_PRICE"}
Alert = Callable[[str, str, str | None, bool], Awaitable[Any]]
Prechecks = Callable[[Exchanges, str, dict], Awaitable[dict[str, Any]]]


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)  # indirection so tests can skip real waits


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


async def default_budget(trader: "Trader", symbol: str, regime: str, conviction: str = "B",
                         setup: str | None = None) -> dict[str, Any]:
    from .risk_budget import compute_budget, gather_inputs
    inp = await gather_inputs(trader, regime, setup=setup, symbol=symbol)
    return compute_budget(trader.ex.limits, inp, symbol, conviction)


async def default_prechecks(ex: Exchanges, symbol: str, ticker: dict) -> dict[str, Any]:
    from .scanner import entry_checks  # lazy: scanner imports are heavy
    return await entry_checks(ex, symbol, ticker)


class Trader(OpenFlow, ManageFlow):
    def __init__(self, ex: Exchanges, journal: Journal, alert: Alert | None = None,
                 prechecks: Prechecks = default_prechecks, budget=default_budget):
        self.ex, self.j = ex, journal
        self._alert = alert
        self.prechecks = prechecks
        self.budget = budget  # dynamic risk budget (risk_budget.py)
        self.lock = asyncio.Lock()   # open_short only (portfolio checks); per-symbol sequences use symbol_lock
        self.symlocks = SymbolLocks(lock_dir_for(journal.db))
        self._one_way_ok = False

    def symbol_lock(self, symbol: str, action: str = "", wait_s: float | None = None):
        """`async with tr.symbol_lock(sym, "close"):` around an order-changing sequence (symlock.py). Reentrant in
        one task; raises SymbolBusy (nothing sent) after `wait_s` (default LOCK_WAIT_S)."""
        return self.symlocks.hold(symbol, action, wait_s)

    @property
    def t(self) -> ccxt.binanceusdm:
        return self.ex.require_trade()

    async def alert(self, kind: str, msg: str, symbol: str | None = None, urgent: bool = False) -> None:
        if self._alert:
            await self._alert(kind, msg, symbol, urgent)
        else:
            self.j.log_alert(kind, msg, symbol)

    # ---------------------------------------------------------------- account / positions
    async def _raw_positions(self) -> list[dict]:
        try:
            rows = await self.t.fetch_positions()
        except ccxt.InvalidNonce:  # -1021 clock drift: resync once and retry
            await self.ex.resync_time()
            rows = await self.t.fetch_positions()
        return [p for p in rows if _f(p.get("contracts")) > 0]

    async def account(self, positions: list[dict] | None = None) -> dict:
        await self.ex.trade_markets()
        bal = await self.t.fetch_balance()
        info = bal.get("info", {})
        pos = positions if positions is not None else await self._raw_positions()
        unreal = sum(_f(p.get("unrealizedPnl")) for p in pos)
        day = await reconcile.day_pnl(self.t, unreal)
        return {"mode": self.ex.settings.mode,
                "equity": round(_f(info.get("totalMarginBalance"), _f(bal.get("total", {}).get("USDT"))), 2),
                "wallet": round(_f(info.get("totalWalletBalance")), 2),
                "available": round(_f(info.get("availableBalance")), 2), **day,
                "limits": self.ex.limits.__dict__, "experiment": self.ex.experiment}

    async def account_state(self) -> AccountState:
        pos = await self._raw_positions()
        acct = await self.account(pos)
        return AccountState(equity=acct["equity"], open_symbols=tuple(p["symbol"] for p in pos),
                            total_open_notional=sum(abs(_f(p.get("notional"))) for p in pos),
                            day_pnl=acct["day_pnl"])

    async def stop_orders(self, symbol: str, close_position_only: bool = False) -> list[dict]:
        """Protective buy stops. close_position_only: the one stop that covers the WHOLE position (a limit entry's
        reduce-only pre-armed stop covers only its own leg and may be cancelled with that leg)."""
        orders = await self.t.fetch_open_orders(symbol, params={"trigger": True})
        return [{"id": o["id"], "trigger": _f(o.get("triggerPrice") or o.get("stopPrice")
                                             or (o.get("info") or {}).get("triggerPrice"))}
                for o in orders if is_protective_stop(o) and (not close_position_only or str(
                    (o.get("info") or {}).get("closePosition", "")).lower() == "true")]

    async def position(self, symbol: str) -> dict | None:
        return next((p for p in await self._raw_positions() if p["symbol"] == symbol), None)

    async def positions(self) -> list[dict]:
        """Live positions + the stop actually on the exchange + the journal trade and its plan."""
        out = []
        for p in await self._raw_positions():
            sym = p["symbol"]
            stops = await self.stop_orders(sym)
            jt = self.j.active_trade(sym) or {}
            entry, mark = _f(p.get("entryPrice")), _f(p.get("markPrice"))
            stop = min((s["trigger"] for s in stops), default=None)
            out.append({
                "symbol": sym, "side": p.get("side"), "contracts": _f(p.get("contracts")),
                "entry": entry, "mark": mark, "notional": round(abs(_f(p.get("notional"))), 2),
                "unrealized_pnl": round(_f(p.get("unrealizedPnl")), 2),
                "pnl_pct": round((entry - mark) / entry * 100, 2) if entry else None,
                "leverage": p.get("leverage"), "margin_mode": p.get("marginMode"),
                "liquidation": p.get("liquidationPrice"), "exchange_stop": stop,
                "stop_distance_pct": round((stop - mark) / mark * 100, 2) if stop and mark else None,
                "UNPROTECTED": stop is None, "trade_id": jt.get("id"), "setup": jt.get("setup"),
                "thesis": jt.get("thesis"), "plan": jt.get("plan"), "opened_at": jt.get("opened_at"),
                "mae_pct": jt.get("mae_pct"), "mfe_pct": jt.get("mfe_pct"), "targets_hit": jt.get("targets_hit"),
                "journal_status": jt.get("status", "NOT IN JOURNAL"),
            })
        return out

    # ---------------------------------------------------------------- primitives
    async def _prepare(self, symbol: str, leverage: int) -> None:
        if not self._one_way_ok:
            mode = await self.t.fetch_position_mode(symbol)
            if mode.get("hedged"):
                await self.t.set_position_mode(False)  # fails loudly if hedge-mode positions exist
            self._one_way_ok = True
        try:
            await self.t.set_margin_mode("isolated", symbol)
        except ccxt.ExchangeError as e:
            # -4046 already isolated; -4067/-4047 "cannot be changed if there exists open orders": every order we
            # place went through this call first, so an open order means the symbol is already isolated
            if not any(c in str(e) for c in ("-4046", "-4047", "-4067", "No need to change margin type")):
                raise
        await self.t.set_leverage(leverage, symbol)

    async def _place_stop(self, symbol: str, stop_price: float, tries: int = 2) -> dict:
        px = float(self.t.price_to_precision(symbol, stop_price))
        for i in range(tries):
            try:
                return await self.t.create_order(symbol, "STOP_MARKET", "buy", None, None,
                                                 {**STOP_PARAMS, "triggerPrice": px})
            except (ccxt.NetworkError, ccxt.RateLimitExceeded, ccxt.InvalidNonce) as e:
                if i == tries - 1:
                    raise
                if is_clock_drift(e):
                    await self.ex.resync_time()
                await _sleep(1)
        raise RuntimeError("unreachable")

    async def _market_close(self, symbol: str, amount: float, tries: int = 3) -> dict:
        last: Exception | None = None
        for i in range(tries):
            try:
                return await self.t.create_order(symbol, "market", "buy", amount, None, {"reduceOnly": True})
            except Exception as e:  # noqa: BLE001 - retry anything; this is the last line of defence
                last = e
                if is_clock_drift(e):
                    await self.ex.resync_time()
                await _sleep(2 ** i)
        raise RuntimeError(f"market close failed after {tries} tries: {last}")

    async def _verified_position(self, symbol: str, tries: int = 4) -> dict | None:
        for _ in range(tries):
            p = await self.position(symbol)
            if p:
                return p
            await _sleep(0.5)
        return None
