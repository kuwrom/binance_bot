"""Keep the journal truthful: the exchange is the source of truth for fills, PnL, fees and funding.

- finalize(): close a journal trade using Binance income history (REALIZED_PNL + COMMISSION + FUNDING_FEE,
  INSURANCE_CLEAR = liquidation) and the actual exit fills. Given the trade's position rebuilt from trade history
  (`episode`), its income is bounded by that position and closed_at is its exit fill.
- reconcile(): journal trades are matched to the exchange by their own trade history (episodes.py), not by symbol
  (#15). A trade is closed once that history shows its position flat, at its exit fill, even if the symbol has been
  shorted again since; without its exit fill it is never closed. Rows stuck in 'opening' are recovered or aborted.
  An empty positions answer while trades are open proves nothing (a glitch looks the same): no 'opening' row is
  aborted on it, only a trade's own exit fill closes it, and a trade whose history shows it still open alerts.
Day / week P&L: Trader.account() (equity_marks.py).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from . import episodes, fill_quality
from .exchange import to_raw

log = logging.getLogger("shortbot.reconcile")
PNL_TYPES = {"REALIZED_PNL": "gross", "COMMISSION": "fees", "FUNDING_FEE": "funding", "INSURANCE_CLEAR": "liq_fee"}
OPENING_GRACE_S = 120
CLOSED_ON_EXCHANGE = "closed on exchange (stop / liquidation / manual)"
EMPTY_KEY = "reconcile:empty_since"   # set while positions answers come back empty with trades open (one alert)


def iso_to_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


async def income_since(t, start_ms: int, symbol: str | None = None) -> list[dict[str, Any]]:
    """Raw /fapi/v1/income rows since start_ms (paged by time)."""
    out: list[dict[str, Any]] = []
    cursor = start_ms
    for _ in range(10):
        req: dict[str, Any] = {"startTime": cursor, "limit": 1000}
        if symbol:
            req["symbol"] = to_raw(symbol)
        rows = await t.fapiPrivateGetIncome(req)
        out.extend(rows)
        if len(rows) < 1000:
            break
        cursor = int(rows[-1]["time"]) + 1
    return out


def summarize_income(rows: list[dict[str, Any]]) -> dict[str, float]:
    s = {"gross": 0.0, "fees": 0.0, "funding": 0.0, "liq_fee": 0.0}
    for r in rows:
        key = PNL_TYPES.get(r.get("incomeType"))
        if key:
            s[key] += float(r.get("income") or 0)
    s["net"] = s["gross"] + s["fees"] + s["funding"] + s["liq_fee"]
    s["liquidated"] = any(r.get("incomeType") == "INSURANCE_CLEAR" for r in rows)
    return {k: round(v, 6) if isinstance(v, float) else v for k, v in s.items()}


async def exit_fills(t, symbol: str, since_ms: int, until_ms: int | None = None) -> tuple[float | None, float]:
    """Volume-weighted buy (cover) price and quantity since the trade opened (up to until_ms, if given). All of it,
    however long the trade was held (episodes.history reads past ccxt's 7-day window)."""
    trades = await episodes.history(t, symbol, since_ms, until_ms)
    buys = [x for x in trades if x.get("side") == "buy" and (until_ms is None or int(x["timestamp"]) <= until_ms)]
    qty = sum(float(x["amount"]) for x in buys)
    if not qty:
        return None, 0.0
    return sum(float(x["price"]) * float(x["amount"]) for x in buys) / qty, qty


async def finalize(trader, trade: dict[str, Any], reason: str, lesson: str = "",
                   until_ms: int | None = None, *, episode: dict[str, Any] | None = None) -> dict[str, Any]:
    """Close a journal trade with exchange-truth PnL. Safe to call when the position is already flat.
    until_ms: the trade's end when it is known, so income and fills of later activity on the symbol are not counted.
    episode: the trade's position rebuilt from trade history (episodes.match / walk): income from its first entry fill
    to its until_ms, its exit fills' price, and closed_at = its exit fill (not the time of this call)."""
    t, j = trader.t, trader.j
    since = episode["opened_ms"] if episode else iso_to_ms(trade["opened_at"])
    until = episode.get("until_ms") if episode else until_ms
    rows = await income_since(t, since, trade["symbol"])
    inc = summarize_income([r for r in rows if until is None or int(r["time"]) <= until])
    exit_px = episode["exit"] if episode else (await exit_fills(t, trade["symbol"], since, until))[0]
    if inc["liquidated"] and "liquidat" not in reason:
        reason = f"LIQUIDATED ({reason})"
    j.close_trade(trade["id"], exit_px, inc["net"], reason, lesson,
                  gross_pnl=inc["gross"], fees=inc["fees"], funding=inc["funding"] + inc["liq_fee"])
    if episode:
        j.update_trade(trade["id"], closed_at=ms_to_iso(episode["closed_ms"]))
        fill_quality.record_episode(trader, trade, episode)   # mainnet refs for its fills not yet referenced (#35)
    j.log_decision("trade_closed", reason, trade["symbol"], snapshot={"trade_id": trade["id"], **inc,
                                                                      "exit": exit_px})
    return {"trade_id": trade["id"], "symbol": trade["symbol"], "exit": exit_px, **inc, "reason": reason}


async def reconcile(trader, live_positions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Finalize journal trades whose own position is flat; recover/abort stale 'opening' rows (module doc). One trade's
    failure never stops the others (it alerts)."""
    j = trader.j
    rows = j.open_trades(include_opening=True)
    live = live_positions if live_positions is not None else await trader._raw_positions()
    empty = not live and any(r["status"] == "open" for r in rows)   # proves nothing on its own (module doc)
    if not empty and j.get(EMPTY_KEY):
        j.set(EMPTY_KEY, "")
    pos = {p["symbol"]: p for p in live}
    newest = {r["symbol"]: r["id"] for r in rows}   # rows come by id: the last row per symbol wins
    done: list[dict[str, Any]] = []
    unproven: list[dict[str, Any]] = []
    for tr in rows:
        try:
            if tr["status"] == "opening":
                if not empty:
                    _opening(trader, tr, pos.get(tr["symbol"]))
                continue
            p, older = pos.get(tr["symbol"]), newest[tr["symbol"]] != tr["id"]
            if p and not older and _same(tr, p):
                continue   # the live position is the one this trade last saw: nothing to read
            m = await episodes.match(trader, tr)
            if m["state"] == "closed":   # its own exit fill proves it, whatever the positions answer said
                done.append(await finalize(trader, tr, CLOSED_ON_EXCHANGE, episode=m))
            elif empty:
                unproven.append(tr)
            elif not p or older:
                await trader.alert("reconcile_waiting", _waiting(tr, m, newest[tr["symbol"]] if older else None),
                                   tr["symbol"], False)
        except Exception as e:  # noqa: BLE001 - isolated: the other trades are still reconciled
            log.warning("reconcile trade #%s failed: %s: %s", tr["id"], type(e).__name__, e)
            await trader.alert("reconcile_error", f"trade #{tr['id']} not reconciled ({type(e).__name__}: {e}); "
                               "retried every pass", tr["symbol"], False)
    if unproven:
        await _empty_answer(trader, unproven)
    return done


def _same(tr: dict[str, Any], p: dict[str, Any]) -> bool:
    """The live position is the one the journal last saw: same average entry (an add or a re-entry changes it)."""
    e = float(tr.get("entry_price") or 0)
    return e > 0 and abs(float(p.get("entryPrice") or 0) - e) <= e * 1e-9


def _waiting(tr: dict[str, Any], m: dict[str, Any], newer: int | None) -> str:
    head = f"trade #{tr['id']} not closed: "
    if m["state"] == "open" and newer:
        return head + (f"its trade history shows its position still open ({m['open_qty']:g}), yet newer trade "
                       f"#{newer} is on the symbol: two journal rows on one position? Check and journal by hand")
    if m["state"] == "open":
        return head + ("no position on Binance, but its trade history doesn't show the exit fill yet "
                       f"({m['open_qty']:g} still open per history); checked again every pass")
    if m["state"] == "no_entry":
        return head + f"no entry fill of it in trade history since {tr['opened_at']}; check and journal by hand"
    return head + "its trade history buys back more than it sold (it didn't begin flat); check and journal by hand"


def _opening(trader, tr: dict[str, Any], p: dict[str, Any] | None) -> None:
    if (time.time() * 1000 - iso_to_ms(tr["opened_at"])) / 1000 < OPENING_GRACE_S:
        return   # an entry may be in flight right now
    if p:
        trader.j.activate_trade(tr["id"], float(p["entryPrice"]), float(p["contracts"]),
                                abs(float(p.get("notional") or 0)))
        trader.j.log_alert("trade_recovered", f"trade #{tr['id']} was stuck in 'opening'; activated from the live "
                           "position", tr["symbol"])
    else:
        trader.j.abort_trade(tr["id"], "entry never filled (recovered by reconcile)")


async def _empty_answer(trader, rows: list[dict[str, Any]]) -> None:
    """No position at all, yet these trades' own history shows them still open: a glitch looks just like that, so
    they stay open (and 'opening' rows stay as they are). Alerted once per streak; a non-empty answer ends it."""
    if not trader.j.get(EMPTY_KEY):
        trader.j.set(EMPTY_KEY, str(time.time()))
        await trader.alert("positions_empty", "Binance answered NO positions, but trade history shows "
                           + ", ".join(f"#{r['id']} {r['symbol']}" for r in rows) + " still open: nothing closed or "
                           "aborted on that answer, and the guardian sees no position to check. Check Binance now.",
                           None, True)
