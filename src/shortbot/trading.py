"""Guarded order placement. Short-only: the only opening order is a market SELL, always followed by an
exchange-side stop-market (closePosition, mark price). Safety properties:
- a journal row ('opening') exists BEFORE the entry order, so crashes mid-entry are recoverable
- fills are verified from the live position; the stop is placed with retries; if it can't be placed
  the position is closed (with retries) and an urgent alert fires if even that fails
- every order carries a client id (clientid.py, #9): an ambiguous answer (timeout, 5xx, -1007) is resolved by that id
  and the position, never by a blind resend
- stop changes are make before break (stops.py): a bridge stop covers the swap; move_stop only tightens
- scanner vetoes + market regime are re-checked server-side; overriding them needs a logged reason
- every order-changing sequence on a symbol holds that symbol's lock (symlock.py): one at a time across the
  guardian, the MCP servers and CLI calls on this host. Opening also holds `lock`, this process's one asyncio lock
  (its portfolio checks), and always takes it first

This module: account, positions and the order primitives. The flows are mixed in from trading_open.py (open_short)
and trading_manage.py (close, move_stop, ensure_stop).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import ccxt.async_support as ccxt

from . import clientid, equity_marks, reconcile
from .exchange import Exchanges, to_symbol
from .journal import Journal
from .risk import AccountState
from .stops import STOP_TYPES, is_clock_drift, is_protective_stop  # noqa: F401 (re-export)
from .symlock import SymbolLocks, lock_dir_for
from .trading_manage import ManageFlow
from .trading_open import OpenFlow

log = logging.getLogger("shortbot.trading")
STOP_PARAMS = {"closePosition": True, "workingType": "MARK_PRICE"}
DEAD = ("canceled", "expired", "rejected")   # final: an order found unfilled in one of these never fills
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
        rows = await self._fetch_positions()
        unknown = [p["symbol"] for p in rows if p.get("symbol") not in (self.t.markets or {})]
        if unknown:   # a perp listed after our markets loaded: ccxt keys its position by the raw id until a reload
            await self.ex.trade_markets(unknown[0])
            if any(s in (self.t.markets or {}) for s in map(to_symbol, unknown)):
                rows = await self._fetch_positions()
        return rows

    async def _fetch_positions(self) -> list[dict]:
        try:
            rows = await self.t.fetch_positions()
        except ccxt.InvalidNonce:  # -1021 clock drift: resync once and retry
            await self.ex.resync_time()
            rows = await self.t.fetch_positions()
        return [p for p in rows if _f(p.get("contracts")) > 0]

    async def account(self, positions: list[dict] | None = None) -> dict:
        """Balance, day/week P&L and the transfer-adjusted equity peak. day_pnl / week_pnl are THE definition every
        consumer reads (risk.py's day check, the governor and its give-back peak, the guardian's daily-loss trip)."""
        await self.ex.trade_markets()
        bal = await self.t.fetch_balance()
        info = bal.get("info", {})
        pos = positions if positions is not None else await self._raw_positions()
        unreal = sum(_f(p.get("unrealizedPnl")) for p in pos)
        equity = round(_f(info.get("totalMarginBalance"), _f(bal.get("total", {}).get("USDT"))), 2)
        return {"mode": self.ex.settings.mode, "equity": equity, "wallet": round(_f(info.get("totalWalletBalance")), 2),
                "available": round(_f(info.get("availableBalance")), 2), **await self._period_pnl(equity, unreal),
                "limits": self.ex.limits.__dict__, "experiment": self.ex.experiment}

    async def _period_pnl(self, equity: float, unreal: float) -> dict:
        """ONE income read (since the week start, or the peak's transfer cursor if older), then (equity_marks.py):
        day/week P&L = equity now - the period's mark - transfers since it, so a loss carried over midnight in an open
        position isn't today's and transfers move nothing; an equity move income doesn't explain yet is `pending`
        (income view used); a late or unreadable mark keeps the old definition (old_*_pnl: realized since the period
        start + all unrealized), whichever is worse. Raises only if income can't be read: no figure is better than a
        guess (the guardian's pass fails after its stop checks)."""
        now = datetime.now(timezone.utc)
        now_ms, starts = int(now.timestamp() * 1000), equity_marks.period_starts(now)
        cursor = int(self.j.get(self.j.mode_key(equity_marks.KV_CURSOR)) or now_ms)
        rows = await reconcile.income_since(self.t, min(starts["week"][1], cursor))
        real = {k: reconcile.summarize_income([r for r in rows if int(r.get("time") or 0) >= s])["net"]
                for k, (_, s) in starts.items()}
        old_day, old_week = round(real["day"] + unreal, 4), round(real["week"] + unreal, 4)
        per = equity_marks.period_pnl(self.j, equity, rows, old_day, old_week, now, unreal)   # never raises
        try:
            peak = equity_marks.adjusted_peak(self.j, equity, rows, now_ms)
        except Exception:  # noqa: BLE001 - kv locked: the stored peak; the cursor stays, so no transfer is lost
            log.warning("equity peak not updated (kv)", exc_info=True)
            peak = max(_f(self.j.get(self.j.mode_key(equity_marks.KV_PEAK))), equity)
        return {"realized_today": round(real["day"], 4), "unrealized": round(unreal, 4),
                "day_pnl": per["day_pnl"], "week_pnl": per["week_pnl"], "old_day_pnl": old_day,
                "old_week_pnl": old_week, "late": per["late"], "pending": per.get("pending", {}),
                "degraded": per["degraded"], "equity_peak": round(peak, 2)}

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
        """Live positions + the stop actually on the exchange + the journal trade and its plan. A symbol whose stops
        can't be read is listed with UNPROTECTED "unknown" and the error: it never costs the other rows. `leverage`:
        positionRisk v3 has none, so the journal's."""
        out = []
        for p in await self._raw_positions():
            sym, err = p["symbol"], None
            try:
                stops = await self.stop_orders(sym)
            except Exception as e:  # noqa: BLE001 - one symbol's failed read never fails the whole list
                stops, err = [], f"stop read failed: {type(e).__name__}: {e}"
            jt = self.j.active_trade(sym) or {}
            entry, mark = _f(p.get("entryPrice")), _f(p.get("markPrice"))
            stop = min((s["trigger"] for s in stops), default=None)
            out.append({
                "symbol": sym, "side": p.get("side"), "contracts": _f(p.get("contracts")),
                "entry": entry, "mark": mark, "notional": round(abs(_f(p.get("notional"))), 2),
                "unrealized_pnl": round(_f(p.get("unrealizedPnl")), 2),
                "pnl_pct": round((entry - mark) / entry * 100, 2) if entry else None,
                "leverage": p.get("leverage") or jt.get("leverage"), "margin_mode": p.get("marginMode"),
                "liquidation": p.get("liquidationPrice"), "exchange_stop": stop,
                "stop_distance_pct": round((stop - mark) / mark * 100, 2) if stop and mark else None,
                "UNPROTECTED": "unknown" if err else stop is None, "trade_id": jt.get("id"), "setup": jt.get("setup"),
                "thesis": jt.get("thesis"), "plan": jt.get("plan"), "opened_at": jt.get("opened_at"),
                "mae_pct": jt.get("mae_pct"), "mfe_pct": jt.get("mfe_pct"), "targets_hit": jt.get("targets_hit"),
                "journal_status": jt.get("status", "NOT IN JOURNAL"), **({"error": err} if err else {}),
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
        """ONE closePosition stop at stop_price, under one client id (clientAlgoId) for all its sends. Returns the stop as
        acked, or as found by that id after an ambiguous answer (timeout, 5xx, -1007, -1021). A definite refusal raises
        at once (any ExchangeError subclass, and clientid.STOP_REFUSALS: -4130 a closePosition stop already rests,
        -2021 it would trigger now, -4120). Re-sending after looking is safe: the id is unique among open orders and a
        side holds one closePosition stop, so a re-send of a stop that landed meanwhile is refused, and that refusal is
        resolved by the id again. Raises the last error when the id was never found (it may still land)."""
        px = float(self.t.price_to_precision(symbol, stop_price))
        cid = clientid.new("stop", clientid.tag(symbol))
        doubt = False
        for i in range(tries):
            try:
                return await self.t.create_order(symbol, "STOP_MARKET", "buy", None, None,
                                                 {**STOP_PARAMS, "triggerPrice": px, "clientOrderId": cid})
            except Exception as e:  # noqa: BLE001 - classified below
                unknown = clientid.send_unknown(e, clientid.STOP_REFUSALS)
                if is_clock_drift(e):
                    await self.ex.resync_time()
                if unknown:
                    await _sleep(1)
                if (unknown or doubt) and (found := await self._find_stop(symbol, cid)):
                    return found
                if not unknown or i == tries - 1:
                    raise
                doubt = True
        raise RuntimeError("unreachable")

    async def _find_stop(self, symbol: str, cid: str) -> dict | None:
        """The conditional order sent under `cid`: by its client id (Query Algo Order), else among the open conditional
        orders (a verified shape; the lookup's not-found answer is not, #55). None when neither shows it."""
        try:
            o = await clientid.lookup(self.t, symbol, cid, trigger=True)
            if o and o.get("clientOrderId") == cid:
                return o
        except Exception:  # noqa: BLE001 - the listing may still show it
            pass
        try:
            return next((o for o in await self.t.fetch_open_orders(symbol, params={"trigger": True})
                         if o.get("clientOrderId") == cid), None)
        except Exception:  # noqa: BLE001 - not seen: the caller decides
            return None

    async def _market_close(self, symbol: str, amount: float, tries: int = 3, whole: bool = True) -> dict:
        """Reduce-only market BUY of `amount`, each send under its own client id. A definite refusal is retried (nothing
        ran). An ambiguous answer is settled, never blindly re-sent: the order found by its id is the answer; a flat
        position leaves nothing to buy back ({"flat": True, "filled": None}); otherwise only a `whole` close (amount =
        the position the caller saw) is sent again, as reduce-only can't buy back more than the position. A partial
        close (whole=False) is never re-sent while in doubt: clientid.Unknown once its tries are spent."""
        doubt: list[str] = []
        last: Exception | None = None
        for i in range(tries):
            if whole or not doubt:
                cid = clientid.new("close", clientid.tag(symbol))
                try:
                    return await self.t.create_order(symbol, "market", "buy", amount, None,
                                                     {"reduceOnly": True, "clientOrderId": cid})
                except Exception as e:  # noqa: BLE001 - this is the last line of defence: classified, then retried
                    last = e
                    if is_clock_drift(e):
                        await self.ex.resync_time()
                    if clientid.send_unknown(e):
                        doubt.append(cid)
            await _sleep(2 ** i)
            if doubt and (got := await self._close_outcome(symbol, doubt)) is not None:
                return got
        if doubt and not whole:
            raise clientid.Unknown(doubt[-1], last or RuntimeError("no answer"))
        unsure = f" (outcome unknown: {', '.join(doubt)})" if doubt else ""
        raise RuntimeError(f"market close failed after {tries} tries{unsure}: {last}")

    async def _close_outcome(self, symbol: str, doubt: list[str]) -> dict | None:
        """Settle close sends of unknown outcome: the newest one found with a fill; one found dead and unfilled leaves
        `doubt`; {"flat": True} when the position reads flat; None while still in doubt."""
        for cid in reversed(list(doubt)):
            try:
                o = await clientid.lookup(self.t, symbol, cid)
            except Exception:  # noqa: BLE001 - still unknown
                continue
            if o and _f(o.get("filled")) > 0:
                return o
            if o and o.get("status") in DEAD:
                doubt.remove(cid)
        try:
            pos = await self.position(symbol)
        except Exception:  # noqa: BLE001 - still unknown
            return None
        if pos is None:
            return {"id": None, "clientOrderId": doubt[-1] if doubt else None, "flat": True, "status": None,
                    "filled": None, "average": None}
        return None

    async def _market_sell(self, symbol: str, amount: float, cid: str, lookups: int = 3) -> dict:
        """A market SELL that opens or adds to the short, sent ONCE under client id `cid` (journalled by the caller).
        Returns the order as acked, or as found settled by `cid` after an ambiguous answer. Raises a definite refusal as
        is, or clientid.Unknown when `cid` stays unsettled: it may still fill, so it is never re-sent."""
        try:
            return await self.t.create_order(symbol, "market", "sell", amount, None, {"clientOrderId": cid})
        except Exception as e:  # noqa: BLE001 - classified below
            if not clientid.send_unknown(e):
                raise
            if is_clock_drift(e):
                await self.ex.resync_time()
            for i in range(lookups):
                await _sleep(1 + i)
                try:
                    o = await clientid.lookup(self.t, symbol, cid)
                except Exception:  # noqa: BLE001 - look again
                    continue
                if o is not None and (_f(o.get("filled")) > 0 or o.get("status") in DEAD):
                    return o
            raise clientid.Unknown(cid, e) from e

    async def _verified_position(self, symbol: str, tries: int = 4) -> dict | None:
        for _ in range(tries):
            p = await self.position(symbol)
            if p:
                return p
            await _sleep(0.5)
        return None
