"""Fill the replay cache (replay_store) for a window (task #29). Public data only, written under a scratch dir.

Sources (checked 2026-10-02):
- data.binance.vision daily files (github.com/binance/binance-public-data: every kline interval incl. 1m; "daily data
  becoming available the next day"): futures/um klines, markPriceKlines, premiumIndexKlines, metrics (5m OI and
  long/short ratios), spot klines (µs timestamps since 2025). The S3 listing keeps settled perps (STGUSDT 1m files
  from 2022-08-25 through its 2026-09-24 delivery and beyond, as flat zero-volume bars).
- /fapi/v1/exchangeInfo (prod and demo-fapi): onboardDate, deliveryDate (131 SETTLING symbols still listed), tags,
  status. Coins settled long before the window are not listed there any more: this cache serves recent windows.
- /fapi/v1/fundingRate for settled funding after data/hist.db ends (the monthly archive file appears on the first
  Monday of the next month); hist.db / daily.db are read read-only for older funding and the 200-day daily lookback.

Two passes: 1m perp bars for every coin perp (the ranking needs the whole universe), then mark / premium / spot /
metrics / funding only for the coins that could make a scan's top `depth` (+ margin) at the replayed moments."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from .. import clock
from .. import histdata_vision as V
from ..exchange import NO_DELIVERY
from ..histdata import _is_coin_perp
from . import replay_bars as RB
from .replay_store import Store, daily_from_inputs, parse_klines, ro_connect

log = logging.getLogger("shortbot.replay")
FAPI = "https://fapi.binance.com"
DEMO_FAPI = "https://demo-fapi.binance.com"
DAY, HOUR = RB.DAY, RB.HOUR
PERP_LOOKBACK = 8 * DAY          # market map: 170 hourly bars
FINE_LOOKBACK = 2 * DAY          # 5m x 288, the forming day, premium since the last funding
KEYS = {"perp": "{um}/daily/klines/{raw}/1m/{raw}-1m-{day}.zip",
        "mark": "{um}/daily/markPriceKlines/{raw}/1m/{raw}-1m-{day}.zip",
        "prem": "{um}/daily/premiumIndexKlines/{raw}/1m/{raw}-1m-{day}.zip",
        "spot": "{spot}/daily/klines/{raw}/1m/{raw}-1m-{day}.zip",
        "metrics": "{um}/daily/metrics/{raw}/{raw}-metrics-{day}.zip"}


def days(a_ms: int, b_ms: int) -> list[str]:
    today = int(time.time() // 86400 * 86400 * 1000)  # wall-clock: archive days are published after they end
    return [V.day_str(d) for d in range(a_ms // DAY * DAY, min(b_ms, today), DAY)]


async def exchange_info(client: httpx.AsyncClient, base: str) -> list[dict[str, Any]]:
    r = await client.get(f"{base}/fapi/v1/exchangeInfo", timeout=30)
    r.raise_for_status()
    return r.json()["symbols"]


def build_meta(prod: list[dict], demo: list[dict], hist_coins: dict[str, dict], t0: int, t1: int) -> dict[str, dict]:
    """Coin perps alive at some point of [t0 - lookback, t1] with point-in-time listing facts."""
    demo_set = {s["symbol"] for s in demo}
    out = {}
    for s in prod:
        raw = s["symbol"]
        if s.get("contractType") != "PERPETUAL" or not _is_coin_perp(raw, s):
            continue
        onboard = int(s.get("onboardDate") or 0)
        d = int(s.get("deliveryDate") or NO_DELIVERY)
        delivery = None if d == NO_DELIVERY else d
        if onboard > t1 or (delivery and delivery < t0 - PERP_LOOKBACK):
            continue
        h = hist_coins.get(raw) or {}
        notional = next((float(f.get("notional") or 5) for f in s.get("filters", []) if f.get("filterType") == "MIN_NOTIONAL"), 5.0)
        out[raw] = {"symbol": f"{raw[:-4]}/USDT:USDT", "onboard_ms": onboard or h.get("onboard_ms"),
                    "delivery_ms": delivery, "status_now": s.get("status"), "tags": s.get("underlyingSubType") or [],
                    "demo": raw in demo_set, "min_notional": notional,
                    "spot": [h["spot_raw"], h.get("spot_mult") or 1.0] if h.get("spot_raw") else None}
    return out


def hist_coins(hist_db: Path | str | None) -> dict[str, dict]:
    if not hist_db or not Path(hist_db).exists():
        return {}
    db = ro_connect(hist_db)
    out = {s: json.loads(m) for s, m in db.execute("SELECT symbol, meta FROM coins")}
    db.close()
    return out


async def fetch_kind(f: V.Fetcher, store: Store, kind: str, raws: list[str], day_list: list[str]) -> int:
    """Download the daily files of one kind (skips files already cached; a missing file is recorded empty)."""
    n = 0

    async def one(raw: str, day: str) -> None:
        nonlocal n
        key = KEYS[kind].format(um=V.UM, spot=V.SPOT, raw=raw, day=day)
        if store.have(key):
            return
        text = await f.csv(key)
        if kind == "metrics":
            rows = V.parse_metrics(text) if text else []
            store.put_metrics(raw, rows)
        else:
            rows = parse_klines(text) if text else []
            store.put_bars(kind, raw, rows)
        store.done(key, len(rows), int(time.time() * 1000))  # wall-clock: fetch log
        n += len(rows)
    await asyncio.gather(*(one(r, d) for r in raws for d in day_list))
    return n


async def fetch_funding(client: httpx.AsyncClient, store: Store, raws: list[str], a: int, b: int,
                        pace_s: float = 0.4) -> int:
    """Settled funding from REST (weight shared with fundingInfo: 500 / 5 min / IP, so paced)."""
    n = 0
    for raw in raws:
        r = await client.get(f"{FAPI}/fapi/v1/fundingRate", params={"symbol": raw, "startTime": a, "endTime": b,
                                                                     "limit": 1000}, timeout=30)
        if r.status_code == 200:
            rows = [(int(x["fundingTime"]) // 1000 * 1000, float(x["fundingRate"])) for x in r.json()]
            store.put_funding(raw, rows)
            n += len(rows)
        await asyncio.sleep(pace_s)
    store.db.commit()
    return n


def funding_from_hist(store: Store, hist_db: Path | str | None, raws: list[str], a: int) -> int:
    if not hist_db or not Path(hist_db).exists():
        return 0
    db = ro_connect(hist_db)
    n = 0
    for raw in raws:
        rows = db.execute("SELECT ts, rate FROM funding WHERE symbol=? AND ts>=?", (raw, a)).fetchall()
        store.put_funding(raw, [(int(t), float(r)) for t, r in rows])
        n += len(rows)
    db.close()
    store.db.commit()
    return n


def movers(store: Store, times: list[int], depth: int, margin: int, universe: str) -> list[str]:
    """Coins inside the top depth + margin gainers or losers at any replayed moment (the scan evaluates the top)."""
    from .replay_exchange import ReplayExchange
    rex = ReplayExchange(store, universe=universe)
    keep: set[str] = {"BTCUSDT", "ETHUSDT"}
    for t in times:
        with clock.at(t):
            tk = sorted(rex.tickers().items(), key=lambda kv: kv[1]["percentage"] or 0)
        k = depth + margin
        keep |= {r for r, _ in tk[:k]} | {r for r, _ in tk[-k:]}
    return sorted(keep)


async def build(cache: Path | str, t0: int, t1: int, times: list[int], hist_db: Path | str | None = None,
                daily_db: Path | str | None = None, universe: str = "demo", depth: int = 25, margin: int = 15,
                concurrency: int = 16) -> dict[str, Any]:
    """Fill `cache` for replays in [t0, t1]; returns counts (also stored under kv 'build')."""
    store = Store(cache)
    f = V.Fetcher(concurrency)
    out: dict[str, Any] = {"t0": t0, "t1": t1}
    async with httpx.AsyncClient() as client:
        prod, demo = await asyncio.gather(exchange_info(client, FAPI), exchange_info(client, DEMO_FAPI))
        meta = build_meta(prod, demo, hist_coins(hist_db), t0, t1)
        store.put_meta(meta)
        store.set("exchange_info", {"fetched_ms": int(time.time() * 1000), "prod": len(prod), "demo": len(demo),  # wall-clock: fetch log
                                    "coin_perps": len(meta)})
        raws = sorted(meta)
        out["perp_rows"] = await fetch_kind(f, store, "perp", raws, days(t0 - PERP_LOOKBACK, t1 + DAY))
        store.derive_hourly("perp", raws)
        out["daily_rows"] = daily_from_inputs(store, raws, hist_db, daily_db, (t0 - PERP_LOOKBACK) // DAY * DAY)
        out["funding_hist"] = funding_from_hist(store, hist_db, raws, t0 - 30 * DAY)
        mv = [r for r in movers(store, times, depth, margin, universe) if r in meta]
        out["movers"] = len(mv)
        fine = days(t0 - FINE_LOOKBACK, t1 + DAY)
        for kind in ("mark", "prem", "metrics"):
            out[f"{kind}_rows"] = await fetch_kind(f, store, kind, mv, fine)
        spots = {r: (meta[r]["spot"] or [V.spot_candidates(r)[-1][0], V.spot_candidates(r)[-1][1]]) for r in mv}
        out["spot_rows"] = await fetch_kind(f, store, "spot", sorted({s[0] for s in spots.values()}), fine)
        have = {r[0] for r in store.db.execute("SELECT DISTINCT symbol FROM bars WHERE kind='spot'")}
        for r, s in spots.items():
            meta[r]["spot"] = s if s[0] in have else None
        store.put_meta(meta)
        hist_end = store.db.execute("SELECT MAX(ts) FROM funding").fetchone()[0] or t0 - 30 * DAY
        out["funding_rest"] = await fetch_funding(client, store, mv, min(hist_end, t0) - 3 * DAY, t1 + DAY)
    await f.close()
    out["requests"] = f.requests
    store.set("build", out)
    return out
