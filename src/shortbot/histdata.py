"""Historical data layer: ~2 months of hourly history for EVERY USDT-margined crypto perpetual, delisted ones included
(no survivorship bias), in its own SQLite file (data/hist.db, never the guardian's shortbot.db).

Sources: data.binance.vision archive (klines, premiumIndexKlines, metrics, fundingRate, spot klines) via
histdata_vision; the current exchangeInfo for onboardDate/tags; the REST API only for the hours the archive has not
published yet (histdata_rest). Read-only. Resumable: every archive file is fetched once (table `files`).

    .venv/bin/python -m shortbot.histdata build --days 64      # log: data/logs/histdata.log
    from shortbot.histdata import load60; coins = load60()      # same shape as patterns_data.load + delist/LS keys
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import histdata_vision as V
from .config import Settings
from .exchange import NO_DELIVERY, Exchanges, to_raw, to_symbol
from .histdata_rest import Pacer, tail
from .histdata_vision import DAY, HOUR

log = logging.getLogger("shortbot.histdata")
DB_PATH = "data/hist.db"
INDEX_PERPS = {"BTCDOMUSDT", "DEFIUSDT", "FOOTBALLUSDT", "BLUEBIRDUSDT"}   # composite indices, not coins

SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (symbol TEXT PRIMARY KEY, meta TEXT, updated_ms INTEGER);
CREATE TABLE IF NOT EXISTS files (key TEXT PRIMARY KEY, symbol TEXT, fetched_ms INTEGER, n INTEGER);
CREATE TABLE IF NOT EXISTS bars (symbol TEXT, kind TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, qv REAL,
    n INTEGER, tbq REAL, PRIMARY KEY (symbol, kind, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS metrics_h (symbol TEXT, ts INTEGER, oi REAL, oi_val REAL, ls_top REAL, ls_top_acct REAL,
    ls_global REAL, taker REAL, PRIMARY KEY (symbol, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS funding (symbol TEXT, ts INTEGER, rate REAL, PRIMARY KEY (symbol, ts)) WITHOUT ROWID;
"""
MCOLS = ("oi", "oi_val", "ls_top", "ls_top_acct", "ls_global", "taker")


def connect(db_path: str | Path = DB_PATH) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path))
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


# ------------------------------------------------------------ storage
def put_bars(db, raw: str, kind: str, bars: list[dict], start_ms: int) -> int:
    rows = [(raw, kind, b["ts"], b["o"], b["h"], b["l"], b["c"], b["qv"], b["n"], b["tbq"])
            for b in bars if b["ts"] >= start_ms]
    db.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def put_metrics(db, raw: str, hourly: dict[int, dict], start_ms: int, merge: bool = False) -> int:
    """Hourly metrics rows; merge=True only overwrites the given (non-None) fields of an existing hour."""
    n = 0
    for ts, m in hourly.items():
        if ts < start_ms:
            continue
        if merge:
            db.execute("INSERT OR IGNORE INTO metrics_h (symbol, ts) VALUES (?,?)", (raw, ts))
            sets = [k for k in MCOLS if m.get(k) is not None]
            if sets:
                db.execute(f"UPDATE metrics_h SET {', '.join(f'{k}=?' for k in sets)} WHERE symbol=? AND ts=?",
                           [m[k] for k in sets] + [raw, ts])
        else:
            db.execute("INSERT OR REPLACE INTO metrics_h VALUES (?,?,?,?,?,?,?,?)",
                       (raw, ts, *(m.get(k) for k in MCOLS)))
        n += 1
    return n


def put_funding(db, raw: str, rows: list[list], start_ms: int) -> int:
    rows = [(raw, int(t), float(r)) for t, r in rows if t >= start_ms]
    db.executemany("INSERT OR REPLACE INTO funding VALUES (?,?,?)", rows)
    return len(rows)


def _last(db, raw: str) -> dict[str, int | None]:
    q = lambda sql, *a: db.execute(sql, (raw, *a)).fetchone()[0]  # noqa: E731
    out = {k: q("SELECT MAX(ts) FROM bars WHERE symbol=? AND kind=?", k) for k in ("perp", "prem", "spot")}
    out["oi"] = q("SELECT MAX(ts) FROM metrics_h WHERE symbol=? AND oi_val IS NOT NULL")
    out["funding"] = q("SELECT MAX(ts) FROM funding WHERE symbol=?")
    return out


# ------------------------------------------------------------ universe + meta
def _public_ex(db_path) -> Exchanges:
    """Keyless public client: never loads .env, never has a trade client."""
    return Exchanges(Settings(mode="demo", api_key="", api_secret="", db_path=Path(db_path),
                              telegram_token="", telegram_chat_id=""))


async def _exchange_info(ex: Exchanges) -> dict[str, dict]:
    try:
        info = await ex.public.fapiPublicGetExchangeInfo()
        return {s["symbol"]: s for s in info["symbols"]}
    except Exception as e:  # noqa: BLE001 - delisted-style inference still works without it
        log.warning("exchangeInfo failed (%s); meta inferred from the archive", e)
        return {}


def _is_coin_perp(raw: str, inf: dict | None) -> bool:
    if not re.fullmatch(r"[A-Z0-9]+USDT", raw) or raw in INDEX_PERPS:
        return False
    if inf is None:
        return True
    return (inf.get("contractType") == "PERPETUAL" and inf.get("underlyingType") == "COIN"
            and inf.get("quoteAsset") == "USDT")


async def universe(f: V.Fetcher, info: dict[str, dict]) -> list[str]:
    """Every USDT coin perp the archive has ever had (delisted included) + currently trading ones."""
    _, pre = await f.list(f"{V.UM}/daily/klines/", delimiter="/")
    raws = {p.rstrip("/").rsplit("/", 1)[-1] for p in pre}
    raws |= {s for s, i in info.items() if i.get("status") == "TRADING"}
    return sorted(r for r in raws if _is_coin_perp(r, info.get(r)))


def _delivery(inf: dict | None) -> int | None:
    d = int((inf or {}).get("deliveryDate") or NO_DELIVERY)
    return None if d == NO_DELIVERY else d


def purge(db, raw: str) -> None:
    for t in ("bars", "metrics_h", "funding", "coins"):
        db.execute(f"DELETE FROM {t} WHERE symbol=?", (raw,))


def trim_dead(db, raw: str, inf: dict | None, start_ms: int) -> int | None:
    """The archive keeps publishing flat zero-volume klines and stale metrics for settled perps. For a coin that no
    longer trades, cut the perp-side series at its delisting (deliveryDate, else one hour after the last bar with
    trades). Returns the cut ts (None for trading coins); purges the coin and returns 0 if it did not trade inside
    the window."""
    if (inf or {}).get("status") == "TRADING":
        return None
    last_live = db.execute("SELECT MAX(ts) FROM bars WHERE symbol=? AND kind='perp' AND n>0", (raw,)).fetchone()[0]
    cut = _delivery(inf) or (last_live + HOUR if last_live is not None else None)
    if last_live is None or cut is None or cut <= start_ms:
        purge(db, raw)
        return 0
    db.execute("DELETE FROM bars WHERE symbol=? AND kind IN ('perp','prem') AND ts>=?", (raw, cut))
    db.execute("DELETE FROM metrics_h WHERE symbol=? AND ts>?", (raw, cut))
    db.execute("DELETE FROM funding WHERE symbol=? AND ts>?", (raw, cut))
    return cut


def _meta(db, raw: str, inf: dict | None, fd: int | None, cut: int | None, start_ms: int) -> dict[str, Any]:
    """Onboard from exchangeInfo, else the first archived day (`fd`) / first stored bar; delist = deliveryDate
    (also set for trading coins with a scheduled delisting), else the trim cut of a coin that stopped trading."""
    first = db.execute("SELECT MIN(ts) FROM bars WHERE symbol=? AND kind='perp'", (raw,)).fetchone()[0]
    onboard, src = int((inf or {}).get("onboardDate") or 0), "exchangeInfo"
    if not onboard:
        onboard, src = (fd, "archive_first_day") if fd and fd < start_ms else (first, "archive_first_bar")
    return {"symbol": to_symbol(raw), "raw": raw, "status": (inf or {}).get("status", "DELISTED"),
            "onboard_ms": onboard or None, "onboard_source": src, "delist_ms": _delivery(inf) or cut or None,
            "tags": (inf or {}).get("underlyingSubType") or []}


# ------------------------------------------------------------ archive per coin
async def archive_coin(f: V.Fetcher, db, raw: str, inf: dict | None, spot_set: set[str], start_ms: int,
                       today_ms: int, now_ms: int, trim: bool = True) -> dict[str, Any]:
    """Download (once) every archive file of one coin inside the window, then commit. `trim` needs exchangeInfo
    (a coin missing from it is treated as delisted)."""
    if trim and (inf or {}).get("status") != "TRADING" and (_delivery(inf) or now_ms) <= start_ms:
        purge(db, raw)
        db.commit()
        return {"no_data": True}     # settled before the window (the archive still has flat bars for it)
    plans = {"perp": V.plan_keys(*(await V.list_series(f, "perp", raw, start_ms)), start_ms, today_ms)}
    if not plans["perp"] and (inf or {}).get("status") != "TRADING":
        return {"no_data": True}     # delisted before the window: skip the other listings
    kinds = ("prem", "metrics", "funding")
    lists = await asyncio.gather(*(V.list_series(f, k, raw, start_ms) for k in kinds))
    plans.update({k: V.plan_keys(m, d, start_ms, today_ms) for k, (m, d) in zip(kinds, lists)})
    spot_raw, mult = None, 1.0
    for cand, m in V.spot_candidates(raw):
        if cand in spot_set:
            sp = V.plan_keys(*(await V.list_series(f, "spot", cand, start_ms)), start_ms, today_ms)
            if sp:
                spot_raw, mult, plans["spot"] = cand, m, sp
                break
    done = {k for (k,) in db.execute("SELECT key FROM files WHERE symbol=?", (raw,))}
    todo = [(k, key) for k, keys in plans.items() for key in keys if key not in done]
    texts = await asyncio.gather(*(f.csv(key) for _, key in todo))
    fd = None if (inf or {}).get("onboardDate") else await V.first_day_ms(f, raw)
    rows = 0   # no awaits from here to commit: coins share one connection
    for (kind, key), text in zip(todo, texts):
        if text is None:
            continue
        if kind == "metrics":
            n = put_metrics(db, raw, V.metrics_hourly(V.parse_metrics(text)), start_ms)
        elif kind == "funding":
            n = put_funding(db, raw, V.parse_funding(text), start_ms)
        else:
            n = put_bars(db, raw, kind, V.parse_klines(text), start_ms)
        db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?)", (key, raw, now_ms, n))
        rows += n
    cut = trim_dead(db, raw, inf, start_ms) if trim else None
    if cut == 0:
        db.commit()
        return {"no_data": True}
    meta = _meta(db, raw, inf, fd, cut, start_ms)
    meta.update(spot_raw=spot_raw, spot_mult=mult)
    db.execute("INSERT OR REPLACE INTO coins VALUES (?,?,?)", (raw, json.dumps(meta), now_ms))
    db.commit()
    return {"files": len(todo), "rows": rows, "meta": meta}


async def _tail_coin(ex, pacer, db, raw: str, meta: dict, start_ms: int, now_ms: int) -> dict:
    full = meta.get("status") == "TRADING"
    r = await tail(ex, pacer, raw, meta.get("spot_raw"), _last(db, raw), start_ms, now_ms, full=full)
    for k in ("perp", "prem", "spot"):
        put_bars(db, raw, k, r.get(k) or [], start_ms)
    put_metrics(db, raw, r.get("oi") or {}, start_ms, merge=True)
    put_funding(db, raw, r.get("funding") or [], start_ms)
    db.commit()
    return r["errors"]


async def build(days: int = 64, symbols: list[str] | None = None, db_path: str | Path = DB_PATH,
                concurrency: int = 8, rest_tail: bool = True) -> dict[str, Any]:
    """Download + cache `days` of hourly history for every coin perp (or `symbols`). Resumable; commits per coin."""
    t0 = time.time()
    now_ms = int(t0 * 1000)
    today_ms = now_ms // DAY * DAY
    start_ms = today_ms - days * DAY
    db, f, ex = connect(db_path), V.Fetcher(concurrency), _public_ex(db_path)
    errors: dict[str, str] = {}
    try:
        info = await _exchange_info(ex)
        raws = [to_raw(s) for s in symbols] if symbols else await universe(f, info)
        _, sp = await f.list(f"{V.SPOT}/daily/klines/", delimiter="/")
        spot_set = {p.rstrip("/").rsplit("/", 1)[-1] for p in sp}
        log.info("universe %d symbols, %d spot pairs, window %s .. now", len(raws), len(spot_set),
                 V.day_str(start_ms))
        sem, done = asyncio.Semaphore(concurrency), [0]

        async def one(raw: str) -> None:
            async with sem:
                try:
                    r = await archive_coin(f, db, raw, info.get(raw), spot_set, start_ms, today_ms, now_ms,
                                          trim=bool(info))
                except Exception as e:  # noqa: BLE001 - one coin failing must not stop the build
                    errors[raw] = f"{type(e).__name__}: {e}"[:160]
                    db.rollback()
                    r = {"error": errors[raw]}
            done[0] += 1
            log.info("[%d/%d] %s %s (%.0fs, %d http)", done[0], len(raws), raw,
                     "no data in window" if r.get("no_data") else r.get("error") or f"{r['files']} files",
                     time.time() - t0, f.requests)

        await asyncio.gather(*(one(r) for r in raws))
        if rest_tail:
            pacer = Pacer()
            metas = {s: json.loads(m) for s, m in db.execute("SELECT symbol, meta FROM coins") if s in set(raws)}
            recent = {s for s, m in metas.items() if m.get("status") == "TRADING" or
                      (m.get("delist_ms") or 0) > today_ms - 45 * DAY}
            log.info("REST tail for %d coins", len(recent))
            for i, raw in enumerate(sorted(recent)):
                errs = await _tail_coin(ex, pacer, db, raw, metas[raw], start_ms, int(time.time() * 1000))
                if errs:
                    errors[f"{raw}:tail"] = json.dumps(errs)[:160]
                if i % 50 == 0:
                    log.info("tail %d/%d (%d REST calls)", i, len(recent), pacer.calls)
    finally:
        await f.close()
        await ex.close()
    out = summary(db, start_ms, now_ms)
    out.update(seconds=round(time.time() - t0), http_requests=f.requests, errors=len(errors),
               error_sample=dict(list(errors.items())[:5]))
    db.close()
    log.info("done: %s", json.dumps(out))
    return out


# ------------------------------------------------------------ summary + load
def summary(db, start_ms: int, now_ms: int) -> dict[str, Any]:
    """Coin counts, per-field coverage and hourly gaps (missing perp hours inside each coin's live span)."""
    metas = [json.loads(m) for (m,) in db.execute("SELECT meta FROM coins")]
    n = len(metas)
    active = sum(m.get("status") == "TRADING" for m in metas)
    cov = {}
    for label, sql in (("perp", "SELECT DISTINCT symbol FROM bars WHERE kind='perp'"),
                       ("spot", "SELECT DISTINCT symbol FROM bars WHERE kind='spot'"),
                       ("premium", "SELECT DISTINCT symbol FROM bars WHERE kind='prem'"),
                       ("oi", "SELECT DISTINCT symbol FROM metrics_h WHERE oi_val IS NOT NULL"),
                       ("ls_top", "SELECT DISTINCT symbol FROM metrics_h WHERE ls_top IS NOT NULL"),
                       ("funding", "SELECT DISTINCT symbol FROM funding")):
        cov[label] = round(100 * len(db.execute(sql).fetchall()) / max(n, 1), 1)
    counts = dict(db.execute("SELECT symbol, COUNT(*) FROM bars WHERE kind='perp' GROUP BY symbol").fetchall())
    gaps = {}
    for m in metas:
        lo = max(start_ms, m.get("onboard_ms") or start_ms)
        hi = min(now_ms - HOUR, m.get("delist_ms") or now_ms)
        want = max(0, (hi - lo) // HOUR)
        miss = want - counts.get(m["raw"], 0)
        if miss > 2:
            gaps[m["raw"]] = miss
    return {"coins": n, "active": active, "delisted_or_settling": n - active, "coverage_pct": cov,
            "coins_with_gaps": len(gaps), "gap_sample": dict(sorted(gaps.items(), key=lambda x: -x[1])[:8])}


def load60(db_path: str | Path = DB_PATH, symbols: list[str] | None = None,
           days: float | None = None) -> list[dict[str, Any]]:
    """One dict per coin, patterns_data.load shape + delist_ms and hourly positioning (ls_top, ls_top_acct,
    ls_global, taker_ratio keyed by hour END). Spot o/h/l/c are multiplied by spot_mult (1000X perps)."""
    db = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    want = {to_raw(s) for s in symbols} if symbols else None
    since = int(time.time() * 1000 - days * DAY) if days else 0
    out = []
    for raw, meta_s in db.execute("SELECT symbol, meta FROM coins ORDER BY symbol").fetchall():
        meta = json.loads(meta_s)
        if (want is not None and raw not in want) or meta.get("no_data"):
            continue

        def bars(kind: str) -> list[tuple]:
            return db.execute("SELECT ts,o,h,l,c,qv,n,tbq FROM bars WHERE symbol=? AND kind=? AND ts>=? "
                              "ORDER BY ts", (raw, kind, since)).fetchall()
        mult = meta.get("spot_mult") or 1.0
        mrows = db.execute(f"SELECT ts,{','.join(MCOLS)} FROM metrics_h WHERE symbol=? AND ts>=? ORDER BY ts",
                           (raw, since)).fetchall()
        col = lambda i: {r[0]: r[i] for r in mrows if r[i] is not None}  # noqa: E731
        out.append({
            "symbol": meta["symbol"], "onboard_ms": meta.get("onboard_ms") or 0, "delist_ms": meta.get("delist_ms"),
            "tags": meta.get("tags") or [],
            "perp": [dict(zip(("ts", "o", "h", "l", "c", "qv", "n", "tbq"), r)) for r in bars("perp")],
            "oi": col(2), "premium": {r[0]: r[4] for r in bars("prem")},
            "funding": [list(r) for r in db.execute("SELECT ts, rate FROM funding WHERE symbol=? AND ts>=? "
                                                    "ORDER BY ts", (raw, since))],
            "spot": [{"ts": r[0], "o": r[1] * mult, "h": r[2] * mult, "l": r[3] * mult, "c": r[4] * mult,
                      "qv": r[5], "n": r[6], "tbq": r[7]} for r in bars("spot")],
            "spot_mult": mult,
            "ls_top": col(3), "ls_top_acct": col(4), "ls_global": col(5), "taker_ratio": col(6),
        })
    db.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Build/inspect the historical dataset (data/hist.db)")
    ap.add_argument("cmd", choices=["build", "summary"])
    ap.add_argument("--days", type=int, default=64)
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--no-tail", action="store_true")
    ap.add_argument("--log", default="data/logs/histdata.log")
    a = ap.parse_args()
    Path(a.log).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(a.log), logging.StreamHandler()])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if a.cmd == "build":
        print(json.dumps(asyncio.run(build(a.days, a.symbols, a.db, a.concurrency, not a.no_tail)), indent=1))
    else:
        now = int(time.time() * 1000)
        print(json.dumps(summary(connect(a.db), now // DAY * DAY - a.days * DAY, now), indent=1))


if __name__ == "__main__":
    main()
