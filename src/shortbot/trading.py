"""Guarded order placement. Short-only: the only opening order is a market SELL, always followed by an
exchange-side stop-market (closePosition, mark price). Safety properties:
- a journal row ('opening') exists BEFORE the entry order, so crashes mid-entry are recoverable
- fills are verified from the live position; the stop is placed with retries; if it can't be placed
  the position is closed (with retries) and an urgent alert fires if even that fails
- stop swaps roll back to the old stop on failure; stops only ever tighten
- scanner vetoes + market regime are re-checked server-side; overriding them needs a logged reason
- one asyncio lock serialises all order-changing calls in this process
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import ccxt.async_support as ccxt

from . import reconcile, warmup
from .exchange import Exchanges, to_symbol
from .journal import Journal
from .plans import build_plan, validate_plan
from .risk import (AccountState, RiskViolation, ShortRequest, check_open_short, liquidity_tier,
                   max_notional_for_risk)
from .setups import resolve
from .stops import STOP_TYPES, is_clock_drift, is_protective_stop, swap_stop  # noqa: F401 (re-export)

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
    return compute_budget(trader.ex.limits, await gather_inputs(trader, regime, setup=setup), symbol, conviction)


async def default_prechecks(ex: Exchanges, symbol: str, ticker: dict) -> dict[str, Any]:
    from .scanner import entry_checks  # lazy: scanner imports are heavy
    return await entry_checks(ex, symbol, ticker)


class Trader:
    def __init__(self, ex: Exchanges, journal: Journal, alert: Alert | None = None,
                 prechecks: Prechecks = default_prechecks, budget=default_budget):
        self.ex, self.j = ex, journal
        self._alert = alert
        self.prechecks = prechecks
        self.budget = budget  # dynamic risk budget (risk_budget.py)
        self.lock = asyncio.Lock()
        self._one_way_ok = False

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
            if "-4046" not in str(e) and "No need to change margin type" not in str(e):
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

    # ---------------------------------------------------------------- open
    async def open_short(self, symbol: str, stop_price: float, setup: str, thesis: str,
                         plan: dict | None = None, notional: float | None = None, leverage: int = 2,
                         veto_override: str | None = None, conviction: str = "B") -> dict:
        """conviction: C 0.5x | B 1x | A 1.5x (only with a proven setup record) | M moonshot bucket."""
        symbol = to_symbol(symbol)
        st = resolve(setup)
        async with self.lock:
            await self.ex.trade_markets()
            if symbol not in self.t.markets:
                return {"ok": False, "error": f"{symbol} is not listed on the connected "
                                              f"{self.ex.settings.mode} account"}
            lim = self.ex.limits
            prod_ticker, ticker, acct = await asyncio.gather(
                self.ex.public.fetch_ticker(symbol), self.t.fetch_ticker(symbol), self.account_state())
            price = _f(ticker["last"])
            checks = await self.prechecks(self.ex, symbol, prod_ticker)
            wait = checks.get("features", {}).get("execution_guard")
            if wait:  # market orders only; not overridable, it clears within a minute or two
                return {"ok": False, "wait": wait}
            blockers = list(checks.get("vetoes", [])) + [g for g in [warmup.gate(self.j)] if g]
            if checks.get("regime") in ("hostile", "unknown"):
                blockers.append(f"market regime is {checks['regime']}")
            if blockers and not (veto_override and veto_override.strip()):
                self.j.log_decision("rejected", "vetoes: " + "; ".join(blockers), symbol, snapshot=checks)
                return {"ok": False, "vetoed": blockers,
                        "hint": "pass veto_override='<research reason>' to trade it anyway as a logged exception"}
            budget = await self.budget(self, symbol, checks.get("regime", "unknown"), conviction, setup)
            if budget["blocked"]:
                return {"ok": False, "rejected_by_budget": budget["reasons"] or ["no risk budget left"],
                        "budget": budget}
            f = checks.get("features") or {}
            tier, lmult = liquidity_tier(f.get("quote_volume"), f.get("oi_value"))
            fmult = float(f.get("funding_size_mult") or 1.0)   # shorts paying funding: half size (#22)
            m = min(lmult if tier else 0.5, fmult)   # unknown / too-thin (override): never full size
            if m < 1:   # tier B and/or shorts-pay funding: at most half the risk budget (learnings #13/#18/#22)
                budget = {**budget, "risk_usd": round(budget["risk_usd"] * m, 2),
                          "multipliers": {**(budget.get("multipliers") or {}), "liquidity_funding": m}}
            dist = (stop_price - price) / price if price else 0
            sized = notional is None
            if sized:  # size = dynamic risk budget / stop distance, still inside the hard caps
                notional = round(min(budget["risk_usd"] / dist if dist > 0 else 0,
                                     max_notional_for_risk(acct.equity, price, stop_price, lim)) * 0.98, 2)
            elif dist > 0 and notional * dist > budget["risk_usd"] * 1.001 and not veto_override:
                return {"ok": False, "rejected_by_budget": [
                    f"risk at stop {notional * dist:.2f} USDT > current budget {budget['risk_usd']} USDT "
                    f"({budget['multipliers']}); size down or omit notional to auto-size"], "budget": budget}
            req = ShortRequest(symbol, notional, price, stop_price, leverage, _f(prod_ticker.get("quoteVolume")))
            try:
                check_open_short(req, acct, lim)
            except RiskViolation as e:
                self.j.log_decision("rejected", "; ".join(e.reasons), symbol, snapshot=req.__dict__)
                return {"ok": False, "rejected_by_risk": e.reasons, "request": req.__dict__}
            tp = build_plan(st.default_plan, plan)
            errs = validate_plan(tp, price, stop_price)
            if errs:
                return {"ok": False, "plan_errors": errs}
            amount = float(self.t.amount_to_precision(symbol, notional / price))
            min_cost = self.t.market(symbol)["limits"]["cost"]["min"] or 0
            if amount * price < min_cost:
                return {"ok": False, "error": f"size {amount * price:.2f} USDT below exchange minimum {min_cost}"}

            await self._prepare(symbol, leverage)
            features = {**checks.get("features", {}), "regime": checks.get("regime"), "budget": budget,
                        "vetoes_overridden": blockers if blockers else None, "veto_override": veto_override,
                        "limits": lim.__dict__}
            tid = self.j.create_trade(symbol, setup, thesis, stop_price, leverage, tp.to_dict(), features,
                                      self.ex.experiment)
            self.j.update_trade(tid, conviction=budget.get("conviction"), regime_cell=budget.get("cell"))
            return await self._enter(tid, symbol, amount, stop_price, sized, leverage, lim, acct.equity)

    async def _enter(self, tid: int, symbol: str, amount: float, stop_price: float, sized: bool,
                     leverage: int, lim, equity: float) -> dict:
        try:
            await self.t.create_order(symbol, "market", "sell", amount)
        except Exception as e:  # noqa: BLE001 - the order may still have filled (timeout)
            if not await self._verified_position(symbol, tries=2):
                self.j.abort_trade(tid, f"entry order failed: {e}")
                return {"ok": False, "error": f"entry order failed, no position: {e}"}
        pos = await self._verified_position(symbol)
        if not pos:
            self.j.abort_trade(tid, "entry sent but no position found")
            return {"ok": False, "error": "entry sent but no position found; check the exchange"}
        fill, size = _f(pos.get("entryPrice")), _f(pos.get("contracts"))
        mark = _f(pos.get("markPrice"), fill)
        try:
            if stop_price <= max(fill, mark):
                raise ValueError(f"price {max(fill, mark)} already at/above stop {stop_price}")
            stop = await self._place_stop(symbol, stop_price)
        except Exception as e:  # noqa: BLE001
            try:
                await self._market_close(symbol, size)
                await _sleep(1)
                res = await reconcile.finalize(self, {**self.j.get_trade(tid), "status": "open"},
                                               f"aborted: stop placement failed ({e})")
            except Exception as e2:  # noqa: BLE001
                self.j.activate_trade(tid, fill, size, round(size * fill, 2))
                await self.alert("NAKED_POSITION", f"stop failed ({e}) AND close failed ({e2}). Position is "
                                 "UNPROTECTED. Guardian will retry; check Binance now.", symbol, True)
                return {"ok": False, "error": "UNPROTECTED POSITION: stop and close both failed", "trade_id": tid}
            await self.alert("stop_failed", f"stop placement failed ({e}); position closed", symbol, True)
            return {"ok": False, "error": f"stop placement failed ({e}); position closed", "closed": res}

        self.j.activate_trade(tid, fill, size, round(size * fill, 2))
        risk_usdt = size * (stop_price - fill)
        liq = _f(pos.get("liquidationPrice")) or None
        warnings = []
        if liq and stop_price >= liq * (1 - lim.liq_buffer_pct / 100):
            warnings.append(f"stop {stop_price} is within {lim.liq_buffer_pct}% of liquidation {liq}")
        if equity and risk_usdt / equity * 100 > lim.risk_per_trade_pct * 1.25:
            warnings.append(f"slippage: actual risk {risk_usdt / equity * 100:.2f}% of equity")
        for w in warnings:
            await self.alert("entry_warning", w, symbol, False)
        self.j.log_decision("open", self.j.get_trade(tid)["thesis"], symbol, snapshot={
            "trade_id": tid, "fill": fill, "size": size, "stop": stop_price, "warnings": warnings})
        return {"ok": True, "trade_id": tid, "symbol": symbol, "amount": size, "fill": fill,
                "notional": round(size * fill, 2), "auto_sized": sized, "leverage": leverage,
                "stop": stop_price, "stop_order_id": stop.get("id"), "liquidation": liq,
                "risk_at_stop_usdt": round(risk_usdt, 2), "warnings": warnings}

    # ---------------------------------------------------------------- manage
    async def close(self, symbol: str, reason: str, lesson: str = "", fraction: float = 1.0) -> dict:
        symbol = to_symbol(symbol)
        if not 0 < fraction <= 1:
            return {"ok": False, "error": "fraction must be in (0, 1]"}
        async with self.lock:
            await self.ex.trade_markets()
            pos = await self.position(symbol)
            if not pos:
                return {"ok": False, "error": f"no open position in {symbol}"}
            size = _f(pos["contracts"])
            amount = size if fraction == 1 else float(self.t.amount_to_precision(symbol, size * fraction))
            if amount <= 0:
                return {"ok": False, "error": "fraction too small for the exchange step size"}
            order = await self._market_close(symbol, amount)
            exit_px = _f(order.get("average"), _f(pos.get("markPrice")))
            trade = self.j.active_trade(symbol)
            if fraction < 1 or amount < size:
                self.j.log_decision("partial_close", reason, symbol, snapshot={
                    "trade_id": trade and trade["id"], "exit": exit_px, "amount": amount, "lesson": lesson})
                return {"ok": True, "symbol": symbol, "closed_amount": amount, "exit": exit_px,
                        "remaining": round(size - amount, 8)}
            from .tranches import cancel_legs
            await cancel_legs(self, symbol, "trade closed")
            try:
                await self.t.cancel_all_orders(symbol, params={"trigger": True})
            except Exception as e:  # noqa: BLE001 - position is flat; an orphan stop is harmless but noted
                await self.alert("orphan_stop", f"closed but could not cancel stops: {e}", symbol, False)
            await _sleep(1)  # let income records land
            res = await reconcile.finalize(self, trade, reason, lesson) if trade else {"note": "not in journal"}
            return {"ok": True, "symbol": symbol, "closed_amount": amount, "exit": exit_px, "result": res}

    async def move_stop(self, symbol: str, new_stop: float, reason: str) -> dict:
        """Tighten-only. Swap = cancel old, place new; on failure the old stop is restored."""
        symbol = to_symbol(symbol)
        async with self.lock:
            await self.ex.trade_markets()
            pos = await self.position(symbol)
            if not pos:
                return {"ok": False, "error": f"no open position in {symbol}"}
            stops = await self.stop_orders(symbol)
            mark = _f(pos.get("markPrice"))
            current = min((s["trigger"] for s in stops), default=None)
            new_stop = float(self.t.price_to_precision(symbol, new_stop))   # compare what the exchange would get
            if current is not None and new_stop >= current:   # also a same-tick "move": never swap for nothing
                return {"ok": False, "noop": new_stop == current,
                        "error": f"stops only tighten: new {new_stop} >= current {current}"}
            if new_stop <= mark:
                return {"ok": False, "error": f"new stop {new_stop} must be above mark {mark} (else close instead)"}
            if current is None:
                dist = (new_stop - _f(pos["entryPrice"])) / _f(pos["entryPrice"]) * 100
                if dist > self.ex.limits.max_stop_distance_pct:
                    return {"ok": False, "error": f"no stop yet and {dist:.1f}% from entry exceeds max stop distance"}
            res = await swap_stop(self, symbol, stops, current, new_stop, _f(pos["contracts"]))
            if not res["ok"] or res.get("closed"):
                return res
            placed = res["placed"]
            trade = self.j.active_trade(symbol)
            if trade:
                self.j.update_trade(trade["id"], stop_price=new_stop)
            self.j.log_decision("move_stop", reason, symbol, snapshot={"from": current, "to": new_stop})
            return {"ok": True, "symbol": symbol, "old_stop": current, "new_stop": new_stop,
                    "order_id": placed.get("id")}

    async def ensure_stop(self, symbol: str, fallback_pct: float) -> dict | None:
        """Guardian safety net: restore a missing stop (journal stop, else entry+fallback%), or close the
        position if price is already beyond the intended stop."""
        async with self.lock:
            if await self.stop_orders(symbol, close_position_only=True):
                return None
            pos = await self.position(symbol)
            if not pos:
                return None
            jt = self.j.active_trade(symbol) or {}
            if jt.get("status") == "opening":
                return {"symbol": symbol, "action": "skipped: entry in progress"}
            mark = _f(pos.get("markPrice"))
            stop = _f(jt.get("stop_price")) or _f(pos.get("entryPrice")) * (1 + fallback_pct / 100)
            if stop <= mark:
                await self._market_close(symbol, _f(pos["contracts"]))
                if jt:
                    await _sleep(1)
                    await reconcile.finalize(self, jt, f"guardian: unprotected and past stop {stop}")
                return {"symbol": symbol, "action": "closed", "mark": mark, "stop": stop}
            await self._place_stop(symbol, stop, tries=3)
            self.j.log_alert("stop_restored", f"placed missing stop at {stop}", symbol)
            return {"symbol": symbol, "action": "stop_placed", "stop": stop}
