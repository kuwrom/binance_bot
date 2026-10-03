"""One coin-memory pass (coin_memory.py, docs/COIN_MEMORY.md): for each focus coin, public market data through DataHub
(RESEARCH weight in the guardian's `coins` lane), its state (coin_stats.measure), its events and, on an open short,
#81's break score; every state saved in one journal unit, and expired reads scored by the price at their expiry. The
guardian runs it every 15 min and alerts on it (guardian_coins.py); `coin_memory refresh=True` runs the same `refresh`
on demand. No LLM and no orders: it measures, and a session reads and rewrites the read."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import clock
from .coin_memory import current_read, due_outcomes, focus_symbols, save, save_outcomes, states
from .coin_stats import H1, break_score, events, measure, pct
from .exchange import to_symbol
from .market import DataHub
from .market.liquidations import recent_many
from .market.streams import raw_id
from .profile import level_behaviour
from .scan_triggers import liq_bursts

log = logging.getLogger("shortbot.coin_memory")
BTC = "BTC/USDT:USDT"
BARS_5M, BARS_1H, OI_5M, FLOW_5M, FUNDING_N = 864, 480, 13, 13, 9   # 3 days of 5m, 20 days of 1h, 1h of OI/flow
MACRO_SOON_MIN = 120
OUTCOMES_PER_PASS = 10
FAKEOUT_TTL_S = 86_400   # the sweep study adds a day of events daily
_FAKEOUT: dict[str, tuple[float, dict[str, float]]] = {}


def fakeout_table(path: str | None) -> dict[str, float]:
    """{symbol: fake-out p90 at levels (%), "*": the all-coin value}: profile.level_behaviour's shrunk value, the
    larger of its 24h-high and round-number kinds. It reads every level_sweeps row on its own read-only connection:
    run it off the event loop, at most daily."""
    if not path or not Path(path).exists():
        return {}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        ev = [{**json.loads(d), "symbol": s} for s, d in db.execute("SELECT symbol, data FROM level_sweeps")]
    except sqlite3.Error:   # no sweep study yet
        return {}
    finally:
        db.close()

    def p90(sym: str) -> float | None:
        xs = [k["fakeout_p90_mark_pct"]["value"] for k in level_behaviour(ev, sym).values()]
        xs = [x for x in xs if x is not None]
        return round(max(xs), 2) if xs else None
    out = {s: v for s in {e["symbol"] for e in ev} if (v := p90(s)) is not None}
    if (u := p90("*")) is not None:
        out["*"] = u
    return out


async def fakeouts(path: str | None) -> dict[str, float]:
    """fakeout_table, recomputed in a worker thread at most once a day per journal."""
    hit = _FAKEOUT.get(path or "")
    if hit and time.time() - hit[0] < FAKEOUT_TTL_S:
        return hit[1]
    table = await asyncio.to_thread(fakeout_table, path)
    _FAKEOUT[path or ""] = (time.time(), table)
    return table


async def refresh(ex: Any, j: Any, entries: Any, symbols: list[str] | None = None,
                  now_ms: int | None = None) -> dict[str, Any]:
    """One pass over the focus set (or `symbols`). {"focus", "coins": {symbol: state}, "errors", "scored"}. A coin
    whose data failed is left out (its error listed); a pass in which every coin failed raises."""
    now_ms = now_ms or clock.now_ms()
    full = focus_symbols(j, entries, now_ms)
    focus = full if symbols is None else {to_symbol(s): full.get(to_symbol(s), ["asked"]) for s in symbols}
    syms, path = list(focus), j.w.path
    hub = DataHub(ex)
    try:   # one shared read for every coin's funding
        await hub.funding_tables()
    except Exception as e:  # noqa: BLE001 - each coin's funding is then listed under its state's `missing`
        log.warning("coin memory: funding tables failed: %s", e)
    prev = {s: r["state"] for s, r in states(j.db, syms).items()}
    trades = {t["symbol"]: t for t in j.open_trades()}
    limits: dict[str, list[float]] = {}
    for row in entries.pending() if entries is not None else ():
        if row.get("limit_price"):
            limits.setdefault(row["symbol"], []).append(float(row["limit_price"]))
    btc_bars = await hub.context(BTC).candles("5m", 4)
    btc = round(pct(btc_bars[0][4], btc_bars[-1][4]) or 0.0, 2) if len(btc_bars) >= 4 else None
    liq = await asyncio.to_thread(recent_many, path, syms, now_ms - H1, now_ms) if path and syms else {}
    bursts: dict[str, list[str]] = {}
    for t in liq_bursts(liq, now_ms):
        bursts.setdefault(t.symbol, []).append(t.rule)
    fake = await fakeouts(path)
    got = await asyncio.gather(*(_coin(hub, s, prev.get(s), now_ms, btc=btc, liq=bursts.get(raw_id(s), []),
                                       fakeout=fake.get(s, fake.get("*")), entries=limits.get(s, []),
                                       trade=trades.get(s), read=current_read(j.db, s, now_ms) if s in trades else None)
                                 for s in syms), return_exceptions=True)
    coins, errors = {}, {}
    for s, r in zip(syms, got):
        if isinstance(r, Exception):
            errors[s] = f"{type(r).__name__}: {r}"
        elif isinstance(r, BaseException):
            raise r
        else:
            coins[s] = r
    scored = await _outcomes(ex, j.db, now_ms)
    await j.write(_store, coins, focus, scored, now_ms)
    if syms and not coins:
        raise RuntimeError("coin memory: every coin failed: " + "; ".join(f"{s}: {w}" for s, w in errors.items()))
    for s, why in errors.items():
        log.warning("coin memory %s failed: %s", s, why)
    return {"focus": focus, "coins": coins, "errors": errors, "scored": len(scored)}


def _store(j: Any, coins: dict[str, dict[str, Any]], focus: dict[str, list[str]], scored: list[tuple[int, int, float]],
           now_ms: int) -> None:
    save(j, coins, focus, now_ms)
    save_outcomes(j, scored)


async def _coin(hub: DataHub, sym: str, prev: dict[str, Any] | None, now_ms: int, *, btc: float | None,
                liq: list[str], fakeout: float | None, entries: list[float], trade: dict[str, Any] | None,
                read: dict[str, Any] | None) -> dict[str, Any]:
    ctx = hub.context(sym)
    got: list[Any] = list(await asyncio.gather(   # each getter records a failure in ctx.missing, never raises
        ctx.candles("5m", BARS_5M), ctx.candles("1h", BARS_1H), ctx.oi_history("5m", OI_5M), ctx.long_short(),
        ctx.flow_candles("5m", FLOW_5M), ctx.funding(), ctx.funding_history(FUNDING_N), ctx.news(), ctx.macro()))
    b5, b1h, oi, ls, flow, fund, fhist, news, macro = got
    items = None if not news or news.get("errors") else \
        len(news.get("binance_announcements") or []) + len(news.get("headlines") or [])   # a count, never the text
    soon = [m["minutes_to"] for m in macro or () if 0 <= m.get("minutes_to", -1) <= MACRO_SOON_MIN]
    st = measure(b5, b1h, oi, ls, flow, prev, now_ms,
                 funding_hist=[float(f["fundingRate"]) for f in fhist or () if f.get("fundingRate") is not None],
                 btc_15m_pct=btc, fakeout_p90=fakeout, funding=fund.get("rate"), news=items,
                 macro={"within_2h": len(soon), "next_min": min(soon) if soon else None},
                 missing=sorted(ctx.missing))
    st["events"] = events(st, prev, {"btc_15m_pct": btc, "liq": liq, "entries": entries})
    if trade:
        at = datetime.fromisoformat(trade["opened_at"]) if trade.get("opened_at") else None
        opened = (at if at is None or at.tzinfo else at.replace(tzinfo=timezone.utc))
        st["break"] = {**break_score({"opened_ms": opened.timestamp() * 1000 if opened else 0}, st,
                                     (read or {}).get("invalidation")),
                       "trade_id": trade["id"]}
    return st


async def _outcomes(ex: Any, db: sqlite3.Connection, now_ms: int) -> list[tuple[int, int, float]]:
    """(read_id, expiry, price at expiry) for expired, unscored reads: the open of the first 5m bar at or after the
    expiry (REST, any coin, in focus or not). A failed fetch is retried on a later pass."""
    out = []
    for r in due_outcomes(db, now_ms, OUTCOMES_PER_PASS):
        exp = r["expires_ms"]
        try:
            bars = await ex.public.fetch_ohlcv(r["symbol"], "5m", since=exp, limit=1)
        except Exception as e:  # noqa: BLE001 - scored on a later pass
            log.warning("coin memory: the price at read #%s's expiry failed: %s", r["id"], e)
            continue
        if bars and 0 <= bars[0][0] - exp < 600_000 and bars[0][1]:
            out.append((r["id"], exp, float(bars[0][1])))
    return out
