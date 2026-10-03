"""Keep the journal truthful: the exchange is the source of truth for fills, PnL, fees and funding.

- finalize(): close a journal trade using Binance income history (REALIZED_PNL + COMMISSION + FUNDING_FEE,
  INSURANCE_CLEAR = liquidation) and the actual exit fills. Given the trade's position rebuilt from trade history
  (`episode`), its income is that position's (trade_income) and closed_at is its exit fill. The desk's own close
  passes none: the position is rebuilt here when the trade's own order ids find it (episodes.match, #76).
- income rows are stamped to the whole second, 100-944 ms BEFORE their trade (demo, probe_result 395, #75): a read
  from a fill's own ms misses that fill's rows. A trade's rows are matched by tradeId, funding to the position held
  when it settled (episodes.holds), the rest by time from its opening second.
- reconcile(): journal trades are matched to the exchange by their own trade history (episodes.py), not by symbol
  (#15). A trade is closed once that history shows its position flat, at its exit fill, even if the symbol has been
  shorted again since; without its exit fill it is never closed. Rows stuck in 'opening' are recovered or aborted.
  An empty positions answer while trades are open proves nothing (a glitch looks the same): no 'opening' row is
  aborted on it, only a trade's own exit fill closes it, and a trade whose history shows it still open alerts.
  A close Binance made itself (ADL, liquidation) is journalled as such (exit_reason, #64).
Day / week P&L: Trader.account() (equity_marks.py).
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from . import episodes, fill_quality, targets
from .exchange import to_raw

log = logging.getLogger("shortbot.reconcile")
PNL_TYPES = {"REALIZED_PNL": "gross", "COMMISSION": "fees", "FUNDING_FEE": "funding", "INSURANCE_CLEAR": "liq_fee"}
OPENING_GRACE_S = 120
CLOSED_ON_EXCHANGE = "closed on exchange (stop / liquidation / manual)"
EXIT_ADL = "adl: auto-deleveraged by Binance (closed at the bankrupt counterparty's price)"
EXIT_LIQUIDATION = "liquidation: force-closed by Binance"
FORCED_SLACK_MS = 60_000             # a force order's time vs the trade's exit fill
FORCED_WINDOW_MS = 7 * 86_400_000    # forceOrders reads 7 days back (without startTime: 7 days before endTime)
EMPTY_KEY = "reconcile:empty_since"   # set while positions answers come back empty with trades open (one alert)


def iso_to_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def floor_s(ms: int) -> int:
    """The whole second holding ms: what an income row of a trade at ms is stamped (probe_result 395)."""
    return int(ms) // 1000 * 1000


def _row_key(r: dict[str, Any]) -> tuple:
    return tuple(sorted((k, str(v)) for k, v in r.items()))


async def income_since(t, start_ms: int, symbol: str | None = None) -> list[dict[str, Any]]:
    """Raw /fapi/v1/income rows since start_ms, paged by time. Many rows share a second (module doc), so a full page
    can end inside one: the next page starts AT that second and skips the rows of it already read."""
    out: list[dict[str, Any]] = []
    cursor, seen = start_ms, Counter()
    for _ in range(10):
        req: dict[str, Any] = {"startTime": cursor, "limit": 1000}
        if symbol:
            req["symbol"] = to_raw(symbol)
        rows = await t.fapiPrivateGetIncome(req)
        fresh = []
        for r in rows:
            k = _row_key(r)
            if seen[k]:
                seen[k] -= 1   # its second's rows the last page answered already
            else:
                fresh.append(r)
        out.extend(fresh)
        if len(rows) < 1000:
            break
        if not fresh:
            log.warning("income from %s: 1000+ rows in one second; the rest of it is not read", cursor)
            break
        cursor = int(rows[-1]["time"])
        seen = Counter(_row_key(r) for r in out if int(r["time"]) == cursor)
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


async def trade_income(t, trade: dict[str, Any], episode: dict[str, Any] | None = None,
                       until_ms: int | None = None) -> dict[str, Any]:
    """The trade's income (summarize_income), read from the second it opened (module doc). With its episode, a row
    carrying a tradeId counts when that trade is one of the position's fills (the fill before it or a re-entry, in the
    same second or before until_ms, is another position's); a FUNDING_FEE row when the position held it
    (episodes.holds: read from FUNDING_LAG_MS before that second); other rows without a tradeId (insurance) from that
    second up to the episode's until_ms. Without an episode, every row from that second to until_ms (open-ended
    without)."""
    since = floor_s(episode["opened_ms"] if episode else iso_to_ms(trade["opened_at"]))
    until = episode.get("until_ms") if episode else until_ms
    ids = {str(x.get("id")) for x in [*episode["sells"], *(episode.get("buys") or [])]} if episode else None

    def ours(r: dict[str, Any]) -> bool:
        tid, ms = str(r.get("tradeId") or ""), int(r["time"])
        if ids is not None and tid:
            return tid in ids
        if episode and r.get("incomeType") == "FUNDING_FEE":
            return episodes.holds(episode, ms)
        return since <= ms and (until is None or ms <= until)
    rows = await income_since(t, since - (episodes.FUNDING_LAG_MS if episode else 0), trade["symbol"])
    return summarize_income([r for r in rows if ours(r)])


async def finalize(trader, trade: dict[str, Any], reason: str, lesson: str = "",
                   until_ms: int | None = None, *, episode: dict[str, Any] | None = None) -> dict[str, Any]:
    """Close a journal trade with exchange-truth PnL. Safe to call when the position is already flat.
    until_ms: the trade's end when it is known, so income and fills of later activity on the symbol are not counted.
    episode: the trade's position rebuilt from trade history (episodes.match / walk): its own income (trade_income),
    its exit fills' price, and closed_at = its exit fill (not the time of this call). None given (the desk's own close):
    rebuilt here by the trade's order ids (own_episode)."""
    t, j = trader.t, trader.j
    given = episode is not None
    if not given and until_ms is None:
        episode = await own_episode(trader, trade)
    inc = await trade_income(t, trade, episode, until_ms)
    exit_px = episode["exit"] if episode else (await exit_fills(t, trade["symbol"], iso_to_ms(trade["opened_at"]),
                                                                until_ms))[0]
    if inc["liquidated"] and "liquidat" not in reason:
        reason = f"LIQUIDATED ({reason})"
    j.close_trade(trade["id"], exit_px, inc["net"], reason, lesson,
                  gross_pnl=inc["gross"], fees=inc["fees"], funding=inc["funding"] + inc["liq_fee"])
    if episode:
        j.update_trade(trade["id"], closed_at=ms_to_iso(episode["closed_ms"]))
    if given:   # rebuilt here: the desk's close, whose fills it benchmarks live (#35)
        fill_quality.record_episode(trader, trade, episode,   # mainnet refs for its fills not yet referenced (#35)
                                    targets.order_kinds(j, trade["id"]))   # its resting targets' fills say so
    j.log_decision("trade_closed", reason, trade["symbol"], snapshot={"trade_id": trade["id"], **inc,
                                                                      "exit": exit_px})
    return {"trade_id": trade["id"], "symbol": trade["symbol"], "exit": exit_px, **inc, "reason": reason}


async def own_episode(trader, trade: dict[str, Any]) -> dict[str, Any] | None:
    """The trade's closed position, found in trade history by its own order ids (episodes.match `by` "ids", #76): a
    close and a re-entry in one second each keep their own fills and income. None for a trade journalled without ids
    (matched by time, it could be the previous position) or while history doesn't show it flat yet: finalize then
    counts income from its opening second, as before #76."""
    m = await episodes.match(trader, trade)
    return m if m["state"] == "closed" and m.get("by") == "ids" else None


async def reconcile(trader, live_positions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Finalize journal trades whose own position is flat; recover/abort stale 'opening' rows (module doc). One trade's
    failure never stops the others (it alerts)."""
    j = trader.j
    rows = j.open_trades(include_opening=True)
    live = live_positions if live_positions is not None else await trader.raw_positions()
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
                why = await exit_reason(trader.t, tr["symbol"], m)
                if why == CLOSED_ON_EXCHANGE:   # not Binance's own close: its last exit may be a resting target (#65)
                    why = targets.exit_label(trader.j, tr["id"], m) or why
                done.append(await finalize(trader, tr, why, episode=m))
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


async def exit_reason(t, symbol: str, ep: dict[str, Any]) -> str:
    """Why an exchange-side close happened, when Binance made it itself: one of the trade's exit orders is listed by GET
    /fapi/v1/forceOrders (weight 20 with a symbol) under autoCloseType ADL or LIQUIDATION (their client ids:
    "adl_autoclose" and "autoclose-..."). Unreadable: the generic reason (a label, never a reason not to close)."""
    end = int(ep["closed_ms"]) + FORCED_SLACK_MS
    req = {"symbol": to_raw(symbol), "startTime": max(int(ep["opened_ms"]), end - FORCED_WINDOW_MS), "endTime": end}
    exits = {str(o) for o in ep.get("exit_orders") or []}
    for kind, reason in (("ADL", EXIT_ADL), ("LIQUIDATION", EXIT_LIQUIDATION)):
        try:
            if forced_by(await t.fapiPrivateGetForceOrders({**req, "autoCloseType": kind}), exits):
                return reason
        except Exception as e:  # noqa: BLE001
            log.warning("forceOrders %s %s unreadable: %s: %s", symbol, kind, type(e).__name__, e)
            return CLOSED_ON_EXCHANGE
    return CLOSED_ON_EXCHANGE


def forced_by(rows: Any, exits: set[str]) -> bool:
    """Does a forceOrders answer list one of these exit orders?"""
    return isinstance(rows, list) and any(isinstance(r, dict) and str(r.get("orderId")) in exits for r in rows)


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
