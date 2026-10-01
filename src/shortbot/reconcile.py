"""Keep the journal truthful: the exchange is the source of truth for fills, PnL, fees and funding.

- finalize(): close a journal trade using Binance income history (REALIZED_PNL + COMMISSION + FUNDING_FEE,
  INSURANCE_CLEAR = liquidation) and the actual exit fills.
- reconcile(): journal rows whose position no longer exists (stop hit, liquidation, ADL, closed in the app)
  get finalized; rows stuck in 'opening' are recovered or aborted.
- day_pnl(): realized income since 00:00 UTC + current unrealized PnL (deposits/transfers don't count).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .exchange import to_raw

PNL_TYPES = {"REALIZED_PNL": "gross", "COMMISSION": "fees", "FUNDING_FEE": "funding", "INSURANCE_CLEAR": "liq_fee"}
OPENING_GRACE_S = 120


def iso_to_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def utc_midnight_ms() -> int:
    now = datetime.now(timezone.utc)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


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
    """Volume-weighted buy (cover) price and quantity since the trade opened (up to until_ms, if given)."""
    trades = await t.fetch_my_trades(symbol, since=since_ms, limit=500)
    buys = [x for x in trades if x.get("side") == "buy" and (until_ms is None or int(x["timestamp"]) <= until_ms)]
    qty = sum(float(x["amount"]) for x in buys)
    if not qty:
        return None, 0.0
    return sum(float(x["price"]) * float(x["amount"]) for x in buys) / qty, qty


async def finalize(trader, trade: dict[str, Any], reason: str, lesson: str = "",
                   until_ms: int | None = None) -> dict[str, Any]:
    """Close a journal trade with exchange-truth PnL. Safe to call when the position is already flat.
    until_ms: the trade's end when it is known (a round trip rebuilt from trade history), so income and fills of
    later activity on the symbol are not counted in it."""
    t, j = trader.t, trader.j
    since = iso_to_ms(trade["opened_at"])
    rows = await income_since(t, since, trade["symbol"])
    inc = summarize_income([r for r in rows if until_ms is None or int(r["time"]) <= until_ms])
    exit_px, _ = await exit_fills(t, trade["symbol"], since, until_ms)
    if inc["liquidated"] and "liquidat" not in reason:
        reason = f"LIQUIDATED ({reason})"
    j.close_trade(trade["id"], exit_px, inc["net"], reason, lesson,
                  gross_pnl=inc["gross"], fees=inc["fees"], funding=inc["funding"] + inc["liq_fee"])
    j.log_decision("trade_closed", reason, trade["symbol"], snapshot={"trade_id": trade["id"], **inc,
                                                                      "exit": exit_px})
    return {"trade_id": trade["id"], "symbol": trade["symbol"], "exit": exit_px, **inc, "reason": reason}


async def reconcile(trader, live_positions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Finalize journal trades that are flat on the exchange; recover/abort stale 'opening' rows."""
    live = live_positions if live_positions is not None else await trader._raw_positions()
    live_syms = {p["symbol"] for p in live}
    done: list[dict[str, Any]] = []
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    for tr in trader.j.open_trades(include_opening=True):
        age_s = (now_ms - iso_to_ms(tr["opened_at"])) / 1000
        if tr["status"] == "opening":
            if age_s < OPENING_GRACE_S:
                continue  # an entry may be in flight right now
            if tr["symbol"] in live_syms:
                p = next(p for p in live if p["symbol"] == tr["symbol"])
                trader.j.activate_trade(tr["id"], float(p["entryPrice"]), float(p["contracts"]),
                                        abs(float(p.get("notional") or 0)))
                trader.j.log_alert("trade_recovered", f"trade #{tr['id']} was stuck in 'opening'; "
                                   "activated from the live position", tr["symbol"])
            else:
                trader.j.abort_trade(tr["id"], "entry never filled (recovered by reconcile)")
            continue
        if tr["symbol"] not in live_syms:
            done.append(await finalize(trader, tr, "closed on exchange (stop / liquidation / manual)"))
    return done


async def day_pnl(t, unrealized_total: float) -> dict[str, float]:
    inc = summarize_income(await income_since(t, utc_midnight_ms()))
    return {"realized_today": round(inc["net"], 4), "unrealized": round(unrealized_total, 4),
            "day_pnl": round(inc["net"] + unrealized_total, 4)}
