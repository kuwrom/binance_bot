"""Opening a short (mixed into Trader): server-side vetoes, regime and budget checks, sizing, the plan, then a
market SELL that is only kept once its stop is on the exchange.

trading.py imports this module, so `_f` and `_sleep` are imported inside each method at call time: a module-level
import would be circular, and would bind `_sleep` before the tests patch `shortbot.trading._sleep`.
"""

from __future__ import annotations

import asyncio

from . import reconcile, warmup
from .exchange import to_symbol
from .plans import build_plan, validate_plan
from .risk import (RiskViolation, ShortRequest, check_open_short, liquidity_tier,
                   max_notional_for_risk)
from .setups import resolve


class OpenFlow:
    """Uses Trader's exchange, journal, lock, prechecks, budget and order primitives."""

    async def open_short(self, symbol: str, stop_price: float, setup: str, thesis: str,
                         plan: dict | None = None, notional: float | None = None, leverage: int = 2,
                         veto_override: str | None = None, conviction: str = "B") -> dict:
        """conviction: C 0.5x | B 1x | A 1.5x (only with a proven setup record) | M moonshot bucket."""
        from .trading import _f
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
        from .trading import _f, _sleep
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
