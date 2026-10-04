"""Fill a replay cache for a long window (task #73, stage 2: the hist.db window at a 30-minute scan cadence).

Differences from replay_fetch (stage 1, a few days):
- Monthly archive files for complete months (github.com/binance/binance-public-data: "new monthly data at the first
  monday of the month"; we wait 7 days after the month ends), daily files otherwise.
- 1m perp bars for every coin perp (the ranking and 1m signals), 5m mark / premium / spot bars for the movers only
  (5m is what every non-1m request needs when scans sit on the 30-minute grid; 1m mark is then unavailable, which
  only the sweep's robust check uses, as a missing input).
- Open interest, long/short ratios and settled funding come from data/hist.db (read-only), which already holds them
  for every coin of the window; no REST calls.
- Movers are picked from hourly 24h changes (top depth + margin each side at every scan time)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .. import histdata_vision as V
from . import replay_bars as RB
from .replay_fetch import DEMO_FAPI, FAPI, build_meta, exchange_info, funding_from_hist, hist_coins
from .replay_store import Store, daily_from_inputs, parse_klines, ro_connect

MIN, HOUR, DAY = RB.MIN, RB.HOUR, RB.DAY
STEPS = {"perp": MIN, "mark": 5 * MIN, "prem": 5 * MIN, "spot": 5 * MIN}
PATH = {"perp": ("um", "klines"), "mark": ("um", "markPriceKlines"), "prem": ("um", "premiumIndexKlines"),
        "spot": ("spot", "klines")}
PERP_LOOKBACK, FINE_LOOKBACK, LABEL_AHEAD = 8 * DAY, 2 * DAY, 3 * DAY + HOUR
MONTHLY_AFTER = 7 * DAY        # a month's file is used once the month ended this long ago


def periods(a_ms: int, b_ms: int, today_ms: int) -> list[str]:
    """Archive periods covering the days of [a, b) before today: 'YYYY-MM' for a month that ended MONTHLY_AFTER
    ago (one file, maybe more days than needed), else 'YYYY-MM-DD'."""
    out: list[str] = []
    d, end = a_ms // DAY * DAY, min(b_ms, today_ms // DAY * DAY)
    while d < end:
        day = datetime.fromtimestamp(d / 1000, UTC)
        nxt = day.replace(day=1, month=day.month % 12 + 1, year=day.year + (day.month == 12), hour=0, minute=0)
        month_end = int(nxt.timestamp() * 1000)
        if month_end + MONTHLY_AFTER <= today_ms:
            out.append(day.strftime("%Y-%m"))
            d = month_end
        else:
            out.append(day.strftime("%Y-%m-%d"))
            d += DAY
    return out


def key(kind: str, raw: str, period: str) -> str:
    market, folder = PATH[kind]
    iv = "1m" if STEPS[kind] == MIN else "5m"
    base = V.UM if market == "um" else V.SPOT
    freq = "monthly" if len(period) == 7 else "daily"
    return f"{base}/{freq}/{folder}/{raw}/{iv}/{raw}-{iv}-{period}.zip"


async def fetch(f: V.Fetcher, store: Store, kind: str, raws: list[str], a: int, b: int) -> int:
    today = int(time.time() * 1000)  # wall-clock: what the archive has published
    plan = periods(a, b, today)
    n = 0

    async def one(raw: str, period: str) -> None:
        nonlocal n
        k = key(kind, raw, period)
        if store.have(k):
            return
        text = await f.csv(k)
        rows = parse_klines(text) if text else []
        store.put_bars(kind, raw, rows)
        store.done(k, len(rows), int(time.time() * 1000))  # wall-clock: fetch log
        n += len(rows)
    for i in range(0, len(raws), 50):                        # commit in slices: a crash loses little
        await asyncio.gather(*(one(r, p) for r in raws[i:i + 50] for p in plan))
    return n


def metrics_from_hist(store: Store, hist_db: Path | str, raws: list[str], a: int, b: int) -> int:
    """hist.db's hourly metrics (keyed by the hour's end: the last 5m snapshot at or before it)."""
    db = ro_connect(hist_db)
    n = 0
    cols = ("oi", "oi_val", "ls_top", "ls_top_acct", "ls_global", "taker")
    for raw in raws:
        rows = db.execute(f"SELECT ts, {', '.join(cols)} FROM metrics_h WHERE symbol=? AND ts>=? AND ts<?",
                          (raw, a, b)).fetchall()
        store.put_metrics(raw, [dict(zip(("ts", *cols), r)) for r in rows])
        n += len(rows)
    db.close()
    store.db.commit()
    return n


async def fill_gaps(store: Store, f: Any, client: Any, raws: list[str], a: int, b: int,
                    pace_s: float = 0.4) -> dict[str, list[str]]:
    """Coins hist.db lacks (the non-ASCII perps before #73's fix): 5m metrics from the daily archive, settled
    funding from /fapi/v1/fundingRate pages. (The archive has no mark / premium klines for them at all.)"""
    no_m = [r for r in raws if not store.metrics(r, a, b)]
    no_f = [r for r in raws if not store.funding(r, a, b)]
    days = [V.day_str(d) for d in range(a // DAY * DAY, b, DAY)]

    async def one(raw: str, day: str) -> None:
        k = f"{V.UM}/daily/metrics/{raw}/{raw}-metrics-{day}.zip"
        if store.have(k):
            return
        text = await f.csv(k)
        rows = V.parse_metrics(text) if text else []
        store.put_metrics(raw, rows)
        store.done(k, len(rows), int(time.time() * 1000))  # wall-clock: fetch log
    await asyncio.gather(*(one(r, d) for r in no_m for d in days))
    for raw in no_f:
        start = a
        while start < b:
            r = await client.get(f"{FAPI}/fapi/v1/fundingRate", timeout=30,
                                 params={"symbol": raw, "startTime": start, "endTime": b, "limit": 1000})
            rows = [(int(x["fundingTime"]) // 1000 * 1000, float(x["fundingRate"])) for x in r.json()] \
                if r.status_code == 200 else []
            store.put_funding(raw, rows)
            await asyncio.sleep(pace_s)
            if len(rows) < 1000:
                break
            start = rows[-1][0] + 1
    store.db.commit()
    return {"metrics": no_m, "funding": no_f}


def movers(store: Store, raws: list[str], times: list[int], depth: int, margin: int) -> list[str]:
    """Coins within the top depth + margin 24h gainers or losers (hourly closes) at any of the scan times."""
    k = depth + margin
    series = {}
    for raw in raws:
        h = store.hourly("perp", raw, min(times) - 2 * DAY, max(times) + HOUR)
        series[raw] = {b[RB.TS]: b for b in h}
    keep = {"BTCUSDT", "ETHUSDT"}
    for t in times:
        last, first = t // HOUR * HOUR - HOUR, t // HOUR * HOUR - DAY
        ch = []
        for raw, s in series.items():
            a, b = s.get(first), s.get(last)
            if a and b and a[RB.O]:
                ch.append((b[RB.C] / a[RB.O] - 1, raw))
        ch.sort()
        keep |= {r for _, r in ch[:k]} | {r for _, r in ch[-k:]}
    return sorted(r for r in keep if r in series)


async def build_window(cache: Path | str, t0: int, t1: int, times: list[int], hist_db: Path | str,
                       daily_db: Path | str | None = None, depth: int = 25, margin: int = 15,
                       concurrency: int = 16, log=print) -> dict[str, Any]:
    store, f = Store(cache), V.Fetcher(concurrency)
    out: dict[str, Any] = {"t0": t0, "t1": t1, "scans": len(times)}
    async with httpx.AsyncClient() as client:
        prod, demo = await asyncio.gather(exchange_info(client, FAPI), exchange_info(client, DEMO_FAPI))
    meta = build_meta(prod, demo, hist_coins(hist_db), t0, t1)
    store.put_meta(meta)
    store.set("steps", {k: v for k, v in STEPS.items() if k != "perp"})
    raws = sorted(meta)
    log(f"{len(raws)} coin perps; perp 1m {V.day_str(t0 - PERP_LOOKBACK)} .. {V.day_str(t1 + LABEL_AHEAD)}")
    out["perp_rows"] = await fetch(f, store, "perp", raws, t0 - PERP_LOOKBACK, t1 + LABEL_AHEAD)
    store.derive_hourly("perp", raws)
    store.derive_five("perp", raws)
    out["daily_rows"] = daily_from_inputs(store, raws, hist_db, daily_db, (t0 - PERP_LOOKBACK) // DAY * DAY + DAY)
    out["funding_rows"] = funding_from_hist(store, hist_db, raws, t0 - 30 * DAY)
    out["metrics_rows"] = metrics_from_hist(store, hist_db, raws, t0 - 3 * DAY, t1 + LABEL_AHEAD)
    async with httpx.AsyncClient() as client:
        out["gaps"] = await fill_gaps(store, f, client, raws, t0 - 3 * DAY, t1 + DAY)
    mv = movers(store, raws, times, depth, margin)
    out["movers"] = len(mv)
    log(f"{len(mv)} movers; 5m mark / premium / spot")
    for kind in ("mark", "prem"):
        out[f"{kind}_rows"] = await fetch(f, store, kind, mv, t0 - FINE_LOOKBACK, t1 + DAY)
    spots = {r: meta[r]["spot"] or [V.spot_candidates(r)[-1][0], V.spot_candidates(r)[-1][1]] for r in mv}
    out["spot_rows"] = await fetch(f, store, "spot", sorted({s[0] for s in spots.values()}), t0 - FINE_LOOKBACK,
                                   t1 + DAY)
    have = {r[0] for r in store.db.execute("SELECT symbol FROM bars WHERE kind='spot' GROUP BY symbol")}
    for r, s in spots.items():
        meta[r]["spot"] = s if s[0] in have else None
    store.put_meta(meta)
    await f.close()
    out["requests"] = f.requests
    store.set("build", out)
    return out
