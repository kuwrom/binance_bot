"""Adopting a hand-placed short (mixed into Trader): a position opened in the Binance app gets a journal trade with a
setup, thesis, stop and plan, so the guardian's plan actions, its R and its exit lesson work as for a desk trade.

- Demo first: refused on the live desk until a demo drill has run (demo_first; cancel-external shares it).
- Never guessed: the position is rebuilt from the symbol's trade history. Its entry orders' ids go into fill_refs
  (order_ids.py), so reconcile closes it by ids (episodes.match). History that doesn't explain the live size and entry
  price (a position older than the lookback, a symbol that wasn't flat where the read began) is refused: a trade
  matched by time is never found again once its entry is older than its opened_at (`no_entry`).
- Stop first, then the journal in one unit: a stop that can't be placed journals nothing. Tighten only: a stop wider
  than the closePosition stop resting (the owner's, or the guardian's fallback) is refused, an equal one keeps it.
- amount = everything sold since the position opened (its original size, as plan targets and R count it): a part
  bought back by hand before the adoption is in the trade's income, as a target the plan had taken would be.
- The kill switch: adoption passes no gate (it adds no risk and must work in REDUCING and HALTED) and loosens nothing.
  A position the guardian would trip for (not in the journal, nothing explaining it: entry_intents.explained_symbols)
  trips REDUCING here, cause "reconciliation", before anything is written, so adopting inside the guardian's grace
  never avoids the trip. Only the human clears it (shortbot-admin).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

from . import entry_intents, episodes, killswitch, order_ids, reconcile, stops, targets_sync
from .audit import refusals
from .exchange import to_raw, to_symbol
from .journal import now_iso
from .plans import TradePlan, build_plan, validate_plan
from .setups import resolve
from .trading_open import _beside, _resting

if TYPE_CHECKING:
    from .config import Settings
    from .exchange import Exchanges
    from .journal import Journal
    from .trading import Trader

MAX_LOOKBACK_DAYS = 90      # Binance answers trade history 3 months back (episodes.py)
ENTRY_TOL = 5e-4            # the rebuilt entry price vs positionRisk's entryPrice (its rounding), relative
NEVER = 1 << 62             # episodes.first_open: no position is "over" before it, so it finds the one still open


def _now_ms() -> int:
    return int(time.time() * 1000)  # indirection so tests can follow the fake exchange's clock


def demo_first(settings: Settings, what: str) -> str | None:
    """Why `what` is refused here: it runs on the demo desk only until a demo drill has proven it (a later merge lifts
    this for live)."""
    if settings.mode != "demo" or settings.is_live:
        return (f"{what} runs on the demo desk only for now (mode {settings.mode}): it is new, and live gets it after "
                "a demo drill. Until then: close or move-stop manage a hand-placed position; cancel a hand-placed "
                "order on Binance")
    return None


def _running_entry(trades: list[dict[str, Any]]) -> float:
    """Binance's entryPrice of the position these fills leave open: an add averages in, a reduction changes nothing."""
    qty = entry = 0.0
    for x in trades:
        amount = float(x["amount"])
        if x["side"] == "sell":
            entry = (entry * qty + float(x["price"]) * amount) / (qty + amount)
            qty += amount
        else:
            qty -= amount
    return entry


class AdoptFlow:
    """Uses Trader's exchange, journal, symbol_lock, position and stop queries and stop primitives."""
    ex: Exchanges
    j: Journal

    @refusals("adopt")
    async def adopt(self, symbol: str, stop_price: float, setup: str, thesis: str, plan: dict | None = None,
                    lookback_days: float = 7.0) -> dict:
        """Journal the symbol's hand-placed short as a trade of `setup` with this stop and plan (module doc)."""
        from .trading import _f
        tr = cast("Trader", self)
        symbol, thesis = to_symbol(symbol), (thesis or "").strip()
        if why := demo_first(self.ex.settings, "adopt"):
            return {"ok": False, "error": why}
        if not thesis:
            return {"ok": False, "error": "an adopted trade needs a thesis: why hold it, and what ends it"}
        if not 0 < lookback_days <= MAX_LOOKBACK_DAYS:
            return {"ok": False, "error": f"lookback_days must be in (0, {MAX_LOOKBACK_DAYS}]"}
        try:
            tp = build_plan(resolve(setup).default_plan, plan)
        except ValueError as e:   # a retired or unknown setup, an unknown plan field
            return {"ok": False, "plan_errors": [str(e)]}
        async with tr.symbol_lock(symbol, f"adopt ({setup})"):
            await self.ex.trade_markets(symbol)
            pos = await tr.position(symbol)
            if refused := self._not_adoptable(symbol, pos):
                return refused
            assert pos is not None
            entry, size = _f(pos.get("entryPrice")), _f(pos["contracts"])
            stop = float(tr.t.price_to_precision(symbol, stop_price))
            old = await tr.stop_orders(symbol, close_position_only=True)
            cur = min((s["trigger"] for s in old), default=None)
            if refused := self._stop_refusal(symbol, pos, tp, stop, cur):
                return refused
            ep = await self._open_episode(symbol, size, entry, lookback_days)
            if isinstance(ep, str):
                return {"ok": False, "error": ep}
            from .entries import EntryDesk  # lazy: entries imports the trading modules
            if symbol not in entry_intents.explained_symbols(EntryDesk(tr)):   # the guardian would trip for it
                await killswitch.trip(self.j, self.ex.settings, killswitch.REDUCING, "adopt", f"reconciliation: "
                                      f"{symbol} position ({size:g}) was not in the journal (opened outside shortbot); "
                                      f"being adopted as a {setup} trade", {"symbol": symbol, "contracts": size},
                                      cause="reconciliation")
            if cur is None or stop < cur:   # stop first: nothing is journalled without it
                res = await stops.set_stop(tr, symbol, stop, size, old)
                if res.get("closed"):
                    return {"ok": False, "closed": True, "error": f"price reached the stop {stop:g} while it was "
                            "placed: the position was closed, nothing adopted"}
                if not res.get("ok"):
                    return {"ok": False, "error": f"stop not placed, nothing journalled: {res.get('error')}"}
            lev = await self._leverage(symbol, pos)
            features = {"entry_type": "adopted", "limits": self.ex.limits.__dict__,
                        "adopted": {"at": now_iso(), "orders": ep["orders"], "contracts": size, "stop_before": cur,
                                    "margin_mode": pos.get("marginMode"), "r_basis": "adoption stop"}}

            def row(j: Journal) -> int:
                tid = j.create_trade(symbol, setup, thesis, stop, lev, tp.to_dict(), features, self.ex.experiment)
                j.update_trade(tid, opened_at=reconcile.ms_to_iso(ep["opened_ms"]))   # its first entry fill
                order_ids.put(j, tid, "entry", dict.fromkeys(ep["orders"]))
                j.activate_trade(tid, entry, ep["qty"], round(ep["qty"] * entry, 2))
                return tid
            tid = await self.j.write(row)
            resting = await targets_sync.at_entry(tr, symbol, "adopt")   # stop on, trade open; still under its lock
            risk = round(size * (stop - entry), 2)
            self.j.log_decision("adopt", thesis, symbol, snapshot={
                "trade_id": tid, "entry": entry, "contracts": size, "sold": ep["qty"], "stop": stop,
                "stop_before": cur, "orders": ep["orders"]})
            await tr.alert("position_adopted", f"hand-placed short ({size:g} @ {entry:g}) adopted as trade #{tid} "
                           f"({setup}), stop {stop:g}, risk {risk:g} USDT: its plan, R and exit lesson now run. "
                           f"Kill switch: {killswitch.read(self.j)['state']} (adoption never clears it)", symbol, False)
            return {"ok": True, "trade_id": tid, "symbol": symbol, "entry": entry, "contracts": size,
                    "amount": ep["qty"], "stop": stop, "stop_before": cur, "risk_at_stop_usdt": risk,
                    "entry_orders": ep["orders"], "kill_switch": killswitch.read(self.j)["state"],
                    **({"resting_targets": resting} if resting is not None else {})}

    def _not_adoptable(self, symbol: str, pos: dict[str, Any] | None) -> dict[str, Any] | None:
        """The refusal when this position can't become a journal trade as it stands; None when it can."""
        if not pos:
            return {"ok": False, "error": f"no open position in {symbol}"}
        if pos.get("side") != "short":
            return {"ok": False, "error": f"{symbol} holds a {pos.get('side')} position: this desk is short-only, "
                                          "close it by hand"}
        if jt := self.j.active_trade(symbol):
            return {"ok": False, "trade_id": jt["id"], "error": f"{symbol} is already journalled as trade "
                    f"#{jt['id']} ({jt['status']}): manage it with update-plan / move-stop / close. If that trade "
                    "has closed since (stopped out), the guardian journals it within a minute: adopt then"}
        if resting := _resting(self.j, symbol):   # their fills would join this position as a second trade (#12)
            return {**_beside(symbol, resting), "hint": "cancel them first (cancel-group for the desk's, "
                    "cancel-external for an order the desk didn't place), then adopt"}
        if pos.get("marginMode") != "isolated":
            return {"ok": False, "error": f"{symbol} is on {pos.get('marginMode') or 'unreadable'} margin: the desk "
                    "assumes isolated (a cross position's loss can take the whole account and its liquidation "
                    "price isn't this trade's alone). Manage it with move-stop / close, or close and re-enter "
                    "isolated"}
        return None

    def _stop_refusal(self, symbol: str, pos: dict[str, Any], tp: TradePlan, stop: float,
                      cur: float | None) -> dict[str, Any] | None:
        from .trading import _f
        tr = cast("Trader", self)
        entry, mark = _f(pos.get("entryPrice")), _f(pos.get("markPrice"))
        errs = validate_plan(tp, entry, stop) + [f"uses_learnings: no learning #{i}"
                                                for i in self.j.unknown_learnings(tp.uses_learnings)]
        if errs:
            return {"ok": False, "plan_errors": errs}
        if stop <= mark:
            return {"ok": False, "error": f"stop {stop:g} must be above mark {mark:g} (else close instead)"}
        cap = tr._liq_cap(symbol, _f(pos.get("liquidationPrice")))
        if cap is not None and stop > cap:
            return {"ok": False, "error": f"stop {stop:g} is past liquidation minus "
                    f"{self.ex.limits.liq_buffer_pct:g}% ({cap:g}): it would never fire"}
        if cur is not None and stop > cur:
            return {"ok": False, "error": f"stops only tighten: {stop:g} is above the closePosition stop resting at "
                    f"{cur:g} (yours, or the guardian's fallback). Adopt at {cur:g} or lower"}
        dist = (stop - entry) / entry * 100
        if dist > self.ex.limits.max_stop_distance_pct:
            return {"ok": False, "error": f"stop {stop:g} is {dist:.1f}% from the entry {entry:g}: over the max stop "
                    f"distance {self.ex.limits.max_stop_distance_pct:g}%"}
        return None

    async def _open_episode(self, symbol: str, size: float, entry: float, days: float) -> dict[str, Any] | str:
        """The open position from trade history (episodes.span: its sells, their order ids, the first fill's time), or
        why the history doesn't explain what positionRisk shows. No fallback: see the module doc."""
        from .trading import _f
        tr = cast("Trader", self)
        trades = await episodes.history(tr.t, symbol, _now_ms() - int(days * 86_400_000))
        start = episodes.first_open(trades, NEVER)
        ep = episodes.span(trades, start) if start is not None else None
        more = (f"Nothing adopted. If it was opened more than {days:g} days ago, pass a longer lookback (up to "
                f"{MAX_LOOKBACK_DAYS} days); else manage it with move-stop / close")
        if ep is None or ep["closed_ms"] is not None:
            return f"the last {days:g} days of {symbol} trade history show no short still open. {more}"
        market = tr.t.market(symbol)["precision"]
        rebuilt = _running_entry(trades[start:])
        if abs(ep["open_qty"] - size) > max(_f(market["amount"]) / 2, 1e-9 * size):
            return (f"trade history explains {ep['open_qty']:g} {symbol} open, the position is {size:g}: the read "
                    f"began inside it. {more}")
        if abs(rebuilt - entry) > max(_f(market["price"]), entry * ENTRY_TOL):
            return (f"trade history rebuilds an entry price of {rebuilt:g}, the position shows {entry:g}: the read "
                    f"began inside it. {more}")
        return ep

    async def _leverage(self, symbol: str, pos: dict[str, Any]) -> int:
        """The symbol's leverage for the journal row (positionRisk v3 carries none): symbolConfig, else the position's
        own field, else 1. Research data only: nothing is sized from it."""
        tr, raw = cast("Trader", self), to_raw(symbol)
        try:
            rows = await tr.t.fapiPrivateGetSymbolConfig({"symbol": raw})
            row = next(r for r in (rows if isinstance(rows, list) else [rows]) if r.get("symbol") == raw)
            return int(float(row["leverage"]))
        except Exception:  # noqa: BLE001 - unreadable: the fallbacks below
            try:
                return int(float(pos.get("leverage") or 1))
            except (TypeError, ValueError):
                return 1
