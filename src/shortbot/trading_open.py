"""Opening a short (mixed into Trader): server-side vetoes, regime and budget checks, sizing, the plan, then a
market SELL that is only kept once its stop is on the exchange.

The SELL carries a client id journalled in the 'opening' row first (entry_features.client_id, #9). An ambiguous answer
is settled by that id and the position, never re-sent; one that stays unknown keeps the row 'opening' (a late fill is
never untracked) and the symbol refuses new entries until reconcile settles it. Once filled, the stop always goes on,
even if the position can't be read; then the plan's resting targets, still under the symbol lock (#71; a failure there
is alerted and never fails the entry: the guardian's pass retries). A size under one step is refused, nothing sent.

One trade per symbol (#12): no market short beside an order resting on the symbol (ours, of unknown outcome, or one
the desk didn't place): its fill would join this position as a second trade and every USDT would count twice. It is
refused, not cancelled first: a cancel can find the order already filled, and its group may hold other symbols' bets.

Portfolio caps (#74): the whole flow holds the account lock (symlock.py), so a session adding risk on another symbol
waits, then sees this one. The caps also count other symbols' resting entries and entries of unknown outcome
(exposure.py); the intended notional is journalled before the send, so a lost answer still counts at its full size.

Two overrides, each a non-empty research reason (#11): `veto_override` trades through the signal vetoes, the cold-desk
gate and a hostile/unknown regime; `budget_override` lets an explicit notional risk more than the dynamic budget
(heat, cluster caps, daily-cap room, governor halving). Neither lifts a blocked budget or a hard cap. Each one used is
recorded in entry_features with what it bypassed, and alerted.

trading.py imports this module, so `_f` and `_sleep` are imported inside each method at call time: a module-level
import would be circular, and would bind `_sleep` before the tests patch `shortbot.trading._sleep`.
"""

from __future__ import annotations

import asyncio
import sqlite3

from . import clientid, errors, exposure, fill_quality, killswitch, reconcile, stops, targets_sync, warmup
from .audit import refusals
from .entry_intents import ON_BOOK
from .exchange import to_symbol
from .plans import build_plan, validate_plan
from .risk import (RiskViolation, ShortRequest, check_open_short, liquidity_tier,
                   max_notional_for_risk)
from .setups import resolve
from .steps import floor_amount, too_small
from .symlock import busy_refusal


def _reason(x: str | None) -> str | None:
    return x.strip() if isinstance(x, str) and x.strip() else None


def _resting(j, symbol: str) -> list[dict]:
    """Entry rows on `symbol` that are or may be on the book, this journal's mode (filtered here, so a table not
    migrated yet still counts as demo)."""
    try:
        rows = j._rows(f"SELECT id, group_id, status, order_id, mode FROM entries WHERE symbol=? AND {ON_BOOK}",
                       (symbol,))
    except sqlite3.OperationalError:   # no entries table yet: nothing rests
        return []
    return [r for r in rows if (r.get("mode") or "demo") == j.mode]


def _beside(symbol: str, rows: list[dict]) -> dict:
    own, ext = [r for r in rows if r["status"] != "external"], [r for r in rows if r["status"] == "external"]
    why = ([f"resting entries {[r['id'] for r in own]} (group {', '.join(sorted({r['group_id'] for r in own}))}) can "
            "fill into it: cancel_entry_group first, or let them rest"] if own else []) + (
           [f"external orders {[r['order_id'] for r in ext]} (not placed by shortbot) rest on it: cancel them on "
            "Binance first"] if ext else [])
    return {"ok": False, "resting_entries": [{k: r[k] for k in ("id", "group_id", "status")} for r in rows],
            "error": f"no market short on {symbol} beside an order that can fill into the same position (a second "
                     "trade, its PnL counted twice): " + "; ".join(why)}


class OpenFlow:
    """Uses Trader's exchange, journal, account_lock (then symbol_lock), prechecks, budget and order primitives."""

    @refusals("open_short")
    @busy_refusal
    async def open_short(self, symbol: str, stop_price: float, setup: str, thesis: str,
                         plan: dict | None = None, notional: float | None = None, leverage: int = 2,
                         veto_override: str | None = None, conviction: str = "B",
                         budget_override: str | None = None) -> dict:
        """conviction: C 0.5x | B 1x | A 1.5x (only with a proven setup record) | M moonshot bucket.
        veto_override / budget_override: research reasons (module doc); blank = none."""
        from .trading import _f
        veto_why, budget_why = _reason(veto_override), _reason(budget_override)
        symbol = to_symbol(symbol)
        st = resolve(setup)
        async with self.account_lock(f"open short {symbol} ({setup})"):   # the caps through the send, across processes
            if blocked := await killswitch.gate(self, "open_short"):
                return blocked
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
            if blockers and not veto_why:
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
            sized, over_budget = notional is None, None
            if sized:  # size = dynamic risk budget / stop distance, still inside the hard caps
                notional = round(min(budget["risk_usd"] / dist if dist > 0 else 0,
                                     max_notional_for_risk(acct.equity, price, stop_price, lim)) * 0.98, 2)
            elif dist > 0 and notional * dist > budget["risk_usd"] * 1.001:
                if not budget_why:
                    return {"ok": False, "rejected_by_budget": [
                        f"risk at stop {notional * dist:.2f} USDT > current budget {budget['risk_usd']} USDT "
                        f"({budget['multipliers']}); size down or omit notional to auto-size"], "budget": budget,
                        "hint": "or pass budget_override='<research reason>' to risk more as a logged exception"}
                over_budget = {"risk_at_stop_usdt": round(notional * dist, 2), "budget_risk_usd": budget["risk_usd"],
                               "multipliers": budget.get("multipliers"), "reasons": budget.get("reasons")}
            req = ShortRequest(symbol, notional, price, stop_price, leverage, _f(prod_ticker.get("quoteVolume")),
                               maint=await self.ex.maint_margin(symbol, notional))   # the symbol's leverage bracket
            try:   # what rests elsewhere or may still fill counts (#74); this symbol's own: refused under its lock
                check_open_short(req, exposure.counted(self.j, acct, lim.max_position_notional, symbol=symbol,
                                                  own_resting=False), lim)
            except RiskViolation as e:
                self.j.log_decision("rejected", "; ".join(e.reasons), symbol, snapshot=req.__dict__)
                return {"ok": False, "rejected_by_risk": e.reasons, "request": req.__dict__}
            tp = build_plan(st.default_plan, plan)
            errs = validate_plan(tp, price, stop_price)
            if errs:
                return {"ok": False, "plan_errors": errs}
            amount = floor_amount(self.t, symbol, notional / price if price else 0)
            if small := too_small(self.t, symbol, amount, price):   # nothing sent (#71)
                return {"ok": False, "error": small}
            if blocked := await killswitch.gate(self, "open_short"):   # again: a trip during the checks above
                return blocked
            async with self.symbol_lock(symbol, f"open short ({setup})"):
                if blocked := killswitch.state_refusal(self.j, "open_short"):   # tripped while we waited for it
                    return blocked
                if await self.position(symbol):   # a limit fill or another session got there first
                    return {"ok": False, "error": f"{symbol} already has a position (opened since the checks); "
                                                  "manage it instead"}
                await targets_sync.clear_stale(self, symbol)   # the last trade's targets never meet this one (#65)
                if (jt := self.j.active_trade(symbol)) and jt["status"] == "opening":   # under the lock: not in flight
                    return {"ok": False, "trade_id": jt["id"], "error": f"trade #{jt['id']}'s entry on {symbol} is "
                            "unresolved (send status unknown): it may still fill. Never re-send it; the guardian "
                            "settles it within ~3 min"}
                if resting := _resting(self.j, symbol):   # under the lock: placement inserts its row under it too
                    return _beside(symbol, resting)
                await self._prepare(symbol, leverage)
                features = {**checks.get("features", {}), "regime": checks.get("regime"), "budget": budget,
                            "vetoes_overridden": blockers or None, "veto_override": veto_why if blockers else None,
                            "budget_overridden": over_budget, "budget_override": budget_why if over_budget else None,
                            "limits": lim.__dict__}
                tid = self.j.create_trade(symbol, setup, thesis, stop_price, leverage, tp.to_dict(), features,
                                          self.ex.experiment)
                cid = clientid.new("open", tid)   # journalled BEFORE the send
                self.j.update_trade(tid, conviction=budget.get("conviction"), regime_cell=budget.get("cell"),
                                    entry_features={**features, "client_id": cid,   # its size counts until it settles
                                                    "intended_notional": round(amount * price, 2)})
                res = await self._enter(tid, cid, symbol, amount, stop_price, sized, leverage, lim, acct.equity)
        await self._override_alerts(symbol, tid, features, res)
        return res

    async def _override_alerts(self, symbol: str, tid: int, f: dict, res: dict) -> None:
        """Each override used reaches the human (#11). After the order, never before it: no alert delays a fill."""
        done = "opened" if res.get("ok") else f"entry failed: {res.get('error')}"
        for kind, what in (("veto_override", f["vetoes_overridden"]), ("budget_override", f["budget_overridden"])):
            if what:
                try:
                    await self.alert(kind, f"trade #{tid}: {f[kind]!r} overrode {what} ({done})", symbol, False)
                except Exception:  # noqa: BLE001 - the trade is journalled with it; an alert outage changes nothing
                    pass

    async def _enter(self, tid: int, cid: str, symbol: str, amount: float, stop_price: float, sized: bool,
                     leverage: int, lim, equity: float) -> dict:
        from .trading import DEAD, _f, _sleep
        try:
            order = await self._market_sell(symbol, amount, cid)
        except clientid.Unknown as e:
            try:
                held = await self.position(symbol)   # flat before the send (checked under this lock): ours, if any
            except Exception:  # noqa: BLE001 - can't tell
                held = None
            if not held:
                return await self._entry_unknown(tid, cid, symbol, e)
            order = {}
        except Exception as e:  # noqa: BLE001 - a definite refusal (_market_sell raises no other): nothing ran
            why = errors.describe(e)   # InsufficientMargin, MarketClosed, Precision...: what to change
            self.j.abort_trade(tid, f"entry order refused: {why}")
            return {"ok": False, "error": f"entry order refused, no position: {why}"}
        if not _f(order.get("filled")) and order.get("status") in DEAD:
            self.j.abort_trade(tid, f"entry order {order['status']}, unfilled")
            return {"ok": False, "error": f"entry order {order['status']} unfilled; no position"}
        ref = fill_quality.snap(self.ex, symbol)   # mainnet at the fill, beside the stop (#35): never awaited here
        pos, unread = await self._fill_position(symbol)
        size = _f((pos or {}).get("contracts")) or _f(order.get("filled"))
        fill = _f((pos or {}).get("entryPrice")) or _f(order.get("average"))
        if not size and unread is None:   # the position reads flat and the order reports no fill
            self.j.abort_trade(tid, "entry sent but no position found")
            return {"ok": False, "error": "entry sent but no position found; check the exchange"}
        mark = _f((pos or {}).get("markPrice"), fill)
        try:
            if fill and stop_price <= max(fill, mark):
                raise ValueError(f"price {max(fill, mark)} already at/above stop {stop_price}")
            try:
                stop = await self._place_stop(symbol, stop_price)   # always, even when the position couldn't be read
            except Exception as e:  # noqa: BLE001 - a stale closePosition stop is swapped out (#14), else re-raised
                stop = await stops.replace_stale(self, symbol, stop_price, size or amount, e)
        except Exception as e:  # noqa: BLE001
            try:
                await self._market_close(symbol, size or amount)
                await _sleep(1)
                res = await reconcile.finalize(self, {**self.j.get_trade(tid), "status": "open"},
                                               f"aborted: stop placement failed ({e})")
            except Exception as e2:  # noqa: BLE001
                if size and fill:
                    self.j.activate_trade(tid, fill, size, round(size * fill, 2))
                await self.alert("NAKED_POSITION", f"stop failed ({e}) AND close failed ({e2}). Position is "
                                 "UNPROTECTED. Guardian will retry; check Binance now.", symbol, True)
                return {"ok": False, "error": "UNPROTECTED POSITION: stop and close both failed", "trade_id": tid}
            await self.alert("stop_failed", f"stop placement failed ({e}); position closed", symbol, True)
            return {"ok": False, "error": f"stop placement failed ({e}); position closed", "closed": res}
        warnings = [] if pos else [f"position unread after the fill ({unread or 'reads flat'}): " + (
            "journalled from the order's own fill" if size and fill else "trade stays 'opening'; the guardian "
            "journals it from the live position")]
        if not (size and fill):
            for w in warnings:
                await self.alert("entry_warning", w, symbol, True)
            return {"ok": True, "trade_id": tid, "symbol": symbol, "client_id": cid, "stop": stop_price,
                    "stop_order_id": stop.get("id"), "warnings": warnings}
        self.j.activate_trade(tid, fill, size, round(size * fill, 2))
        fill_quality.record(self, tid, "entry", "market", order, px=fill, qty=size, snap=ref)
        resting = await targets_sync.at_entry(self, symbol, "entry")   # stop on, trade open; still under its lock
        risk_usdt = size * (stop_price - fill)
        liq = _f((pos or {}).get("liquidationPrice")) or None
        if liq and stop_price >= liq * (1 - lim.liq_buffer_pct / 100):
            warnings.append(f"stop {stop_price} is within {lim.liq_buffer_pct}% of liquidation {liq}")
        if equity and risk_usdt / equity * 100 > lim.risk_per_trade_pct * 1.25:
            warnings.append(f"slippage: actual risk {risk_usdt / equity * 100:.2f}% of equity")
        for w in warnings:
            await self.alert("entry_warning", w, symbol, False)
        self.j.log_decision("open", self.j.get_trade(tid)["thesis"], symbol, snapshot={
            "trade_id": tid, "fill": fill, "size": size, "stop": stop_price, "warnings": warnings})
        return {"ok": True, "trade_id": tid, "symbol": symbol, "amount": size, "fill": fill, "client_id": cid,
                "notional": round(size * fill, 2), "auto_sized": sized, "leverage": leverage,
                "stop": stop_price, "stop_order_id": stop.get("id"), "liquidation": liq,
                "risk_at_stop_usdt": round(risk_usdt, 2), "warnings": warnings,
                **({"resting_targets": resting} if resting is not None else {})}

    async def _fill_position(self, symbol: str, tries: int = 4) -> tuple[dict | None, Exception | None]:
        """The position a fill made, read up to `tries` times: (position, None), (None, None) when it reads flat, or
        (None, the error) when the reads kept failing. Never raises: the stop goes on regardless."""
        from .trading import _sleep
        err: Exception | None = None
        for _ in range(tries):
            try:
                if p := await self.position(symbol):
                    return p, None
                err = None
            except Exception as e:  # noqa: BLE001 - returned to the caller, which alerts what it can't read
                err = e
            await _sleep(0.5)
        return None, err

    async def _entry_unknown(self, tid: int, cid: str, symbol: str, e: clientid.Unknown) -> dict:
        """The entry may still fill: its row stays 'opening' with its client id, never aborted (a late fill must not be
        untracked). open_short refuses the symbol until reconcile settles the row (activated from the position, or
        aborted after OPENING_GRACE_S); the guardian's stop pass protects a late fill at the row's stop."""
        msg = f"entry send status unknown and {cid} not found ({type(e.cause).__name__}): it may still fill; not re-sent"
        self.j.log_decision("entry_unknown", msg, symbol, snapshot={"trade_id": tid, "client_id": cid})
        await self.alert("entry_unknown", f"trade #{tid}: {msg}. The guardian protects and journals a late fill.",
                         symbol, True)
        return {"ok": False, "unknown": True, "trade_id": tid, "client_id": cid, "error": msg,
                "hint": "Don't re-send it. Re-read positions in ~3 min: the guardian adopts a late fill at the planned "
                        "stop, or aborts the row."}
