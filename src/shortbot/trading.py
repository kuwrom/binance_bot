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
  guardian, the MCP servers and CLI calls on this host. Every risk-adding flow first holds the account lock, from its
  portfolio checks through the send, so two sessions can't both spend the last slot (#74)

This module: account, positions and the stop primitives. The flows are mixed in from trading_open.py (open_short)
and trading_manage.py (close, move_stop, ensure_stop); the order primitives from trading_orders.py.

Public interface (#36 gateway step 0): other modules call only Trader's public methods, and send every exchange write
through its primitives (prepare, place_close_stop here; the rest in trading_orders.py), never through `t` directly
(test_trader_public.py). Reads may use `t`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import ccxt.async_support as ccxt

from . import clientid, equity_marks, errors, intents, reconcile, targets
from .exchange import Exchanges, to_raw, to_symbol
from .journal import Journal
from .risk import AccountState
from .stops import is_protective_stop
from .symlock import SymbolLocks, lock_dir_for
from .trading_manage import ManageFlow
from .trading_open import OpenFlow
from .trading_orders import OrderFlow

log = logging.getLogger("shortbot.trading")
STOP_PARAMS = {"closePosition": True, "workingType": "MARK_PRICE"}
DEAD = ("canceled", "expired", "rejected")   # final: an order found unfilled in one of these never fills
Alert = Callable[[str, str, str | None, bool], Awaitable[Any]]
Prechecks = Callable[[Exchanges, str, dict], Awaitable[dict[str, Any]]]
ISOLATED_OK_KEY = "isolated_ok:"   # kv (per mode): when this desk last saw the symbol isolated (prepare)
ISOLATED_TRUST_S = 86400            # how long that confirmation stands in for an unreadable symbolConfig


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


class Trader(OpenFlow, ManageFlow, OrderFlow):
    def __init__(self, ex: Exchanges, journal: Journal, alert: Alert | None = None,
                 prechecks: Prechecks = default_prechecks, budget=default_budget):
        self.ex, self.j = ex, journal
        self._alert = alert
        self.prechecks = prechecks
        self.budget = budget  # dynamic risk budget (risk_budget.py)
        self.symlocks = SymbolLocks(lock_dir_for(journal.db))
        self._one_way_ok = False

    def account_lock(self, action: str = "", wait_s: float | None = None):
        """`async with tr.account_lock("open short"):` from a risk-adding flow's cap checks through its send (#74)."""
        return self.symlocks.account(action, wait_s)

    def symbol_lock(self, symbol: str, action: str = "", wait_s: float | None = None):
        """`async with tr.symbol_lock(sym, "close"):` around an order-changing sequence (symlock.py). Reentrant in
        one task; raises SymbolBusy (nothing sent) after `wait_s` (default LOCK_WAIT_S)."""
        return self.symlocks.hold(symbol, action, wait_s)

    @property
    def t(self) -> ccxt.binanceusdm:
        return self.ex.require_trade()

    def set_alert(self, alert: Alert | None) -> None:
        """Where alert() goes from now on: the guardian's Notifier (as_trader_alert); None: the journal only."""
        self._alert = alert

    async def alert(self, kind: str, msg: str, symbol: str | None = None, urgent: bool = False) -> None:
        """Tell a human (the alert sink set at construction or by set_alert), else journal it."""
        if self._alert:
            await self._alert(kind, msg, symbol, urgent)
        else:
            self.j.log_alert(kind, msg, symbol)

    # ---------------------------------------------------------------- account / positions
    async def raw_positions(self) -> list[dict]:
        """The open positions as ccxt rows (contracts > 0), unjoined with the journal (positions() joins them). A perp
        listed after the markets loaded is reloaded first, so its row is keyed by its unified symbol."""
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
        pos = positions if positions is not None else await self.raw_positions()
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
        peak = equity_marks.adjusted_peak(self.j, equity, rows, now_ms)   # its writes are posted (#79)
        return {"realized_today": round(real["day"], 4), "unrealized": round(unreal, 4),
                "day_pnl": per["day_pnl"], "week_pnl": per["week_pnl"], "old_day_pnl": old_day,
                "old_week_pnl": old_week, "late": per["late"], "pending": per.get("pending", {}),
                "degraded": per["degraded"], "equity_peak": round(peak, 2)}

    async def account_state(self) -> AccountState:
        pos = await self.raw_positions()
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
        return next((p for p in await self.raw_positions() if p["symbol"] == symbol), None)

    async def positions(self) -> list[dict]:
        """Live positions + the stop actually on the exchange + the journal trade and its plan. A symbol whose stops
        can't be read is listed with UNPROTECTED "unknown" and the error: it never costs the other rows. `leverage`:
        positionRisk v3 has none, so the journal's."""
        out = []
        for p in await self.raw_positions():
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
                "targets_resting": targets.view(self.j, jt.get("id")),   # reduce-only limits on the book (#65)
                "journal_status": jt.get("status", "NOT IN JOURNAL"), **({"error": err} if err else {}),
            })
        return out

    # ---------------------------------------------------------------- primitives
    async def prepare(self, symbol: str, leverage: int) -> None:
        """Before an entry: one-way mode (once per process), isolated margin confirmed, then `leverage`. Raises when
        the symbol can't be confirmed isolated (#64): nothing may be sent then."""
        if not self._one_way_ok:
            mode = await self.t.fetch_position_mode(symbol)
            if mode.get("hedged"):
                await self.t.set_position_mode(False)  # fails loudly if hedge-mode positions exist
            self._one_way_ok = True
        try:
            await self.t.set_margin_mode("isolated", symbol)
            self._isolated(symbol)
        except ccxt.ExchangeError as e:   # -4046 (ccxt: MarginModeAlreadySet) = isolated already
            if any(c in str(e) for c in ("-4046", "No need to change margin type")):
                self._isolated(symbol)
            elif any(c in str(e) for c in ("-4047", "-4067")):
                await self._confirm_isolated(symbol, e)
            else:
                raise   # incl. -4168: no isolated margin under Multi-Assets mode
        await self.t.set_leverage(leverage, symbol)

    def _isolated(self, symbol: str) -> None:
        self.j.post(lambda j: j.set(j.mode_key(ISOLATED_OK_KEY + symbol), str(time.time())))   # a cache: posted

    async def _confirm_isolated(self, symbol: str, e: Exception) -> None:
        """-4047/-4067: open orders block a margin-type change. Demo answers -4067 whenever an order rests, even when
        nothing would change (MOVR split entry, 2026-10-01), but an order resting on a CROSSED symbol (not ours: ours
        all went through prepare) looks the same. GET /fapi/v1/symbolConfig (weight 5) decides: ISOLATED proceeds,
        anything else refuses before an order is sent. Unreadable: proceeds only when this desk saw the symbol isolated
        within ISOLATED_TRUST_S (a split entry's earlier leg), else refuses (#64)."""
        raw = to_raw(symbol)
        try:
            rows = await self.t.fapiPrivateGetSymbolConfig({"symbol": raw})
            row = next((r for r in (rows if isinstance(rows, list) else [rows])
                        if isinstance(r, dict) and r.get("symbol") == raw and r.get("marginType")), None)
            why = None if row else f"no marginType for {raw} in {str(rows)[:200]}"
        except Exception as x:  # noqa: BLE001 - unreadable: the confirmation below decides
            row, why = None, f"{type(x).__name__}: {x}"
        if row and str(row["marginType"]).upper() == "ISOLATED":
            return self._isolated(symbol)
        if row:
            raise RuntimeError(f"{symbol} is {row['marginType']} margin and open orders block the switch to isolated "
                               f"({e}): cancel them (not placed by this desk?) before an entry")
        seen = _f(self.j.get(self.j.mode_key(ISOLATED_OK_KEY + symbol)))
        if time.time() - seen < ISOLATED_TRUST_S:
            log.warning("margin type of %s unreadable (%s) behind open orders: isolated by this desk %.0f min ago, "
                        "proceeding", symbol, why, (time.time() - seen) / 60)
            return
        raise RuntimeError(f"margin type of {symbol} can't be confirmed isolated: open orders block the switch ({e}) "
                           f"and symbolConfig is unreadable ({why}). No entry")

    async def place_close_stop(self, symbol: str, stop_price: float, tries: int = 2) -> dict:
        """ONE closePosition stop at stop_price, under one client id (clientAlgoId) for all its sends. Returns the stop as
        acked, or as found by that id after an ambiguous answer (errors.py `unknown`: timeout, 5xx, 429, -1007,
        -1021). A definite refusal raises at once (StopExists -4130: a closePosition stop already rests; WouldTrigger
        -2021; Refused -4120; Precision...). Re-sending after looking is safe: the id is unique among open orders and a
        side holds one closePosition stop, so a re-send of a stop that landed meanwhile is refused, and that refusal is
        resolved by the id again. Raises the last error when the id was never found (it may still land)."""
        px = float(self.t.price_to_precision(symbol, stop_price))
        cid = clientid.new("stop", clientid.tag(symbol))
        intents.link(self.j, cid, symbol)
        doubt = False
        for i in range(tries):
            try:
                return await self.t.create_order(symbol, "STOP_MARKET", "buy", None, None,
                                                 {**STOP_PARAMS, "triggerPrice": px, "clientOrderId": cid})
            except Exception as e:  # noqa: BLE001 - classified: a definite refusal raises, else found by its id
                kind = errors.classify(e)
                unknown = kind.unknown
                if kind is errors.ClockDrift:
                    await self.ex.resync_time()
                if unknown:
                    await _sleep(1)
                if (unknown or doubt) and (found := await self.find_stop(symbol, cid)):
                    return found
                if not unknown or i == tries - 1:
                    raise
                doubt = True
        raise RuntimeError("unreachable")

    async def find_stop(self, symbol: str, cid: str) -> dict | None:
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

    async def verified_position(self, symbol: str, tries: int = 4) -> dict | None:
        """The symbol's position, read up to `tries` times 0.5 s apart (a fill can lag the position endpoint); None
        when it still reads flat."""
        for _ in range(tries):
            p = await self.position(symbol)
            if p:
                return p
            await _sleep(0.5)
        return None
