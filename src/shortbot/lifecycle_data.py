"""Data collection for the listing-lifecycle study (docs/LIFECYCLE_STUDY.md). Read-only, public endpoints only:

- perp universe + listing/delisting dates + product tags: fapi /exchangeInfo (onboardDate, deliveryDate,
  status SETTLING = delisted, underlyingSubType = Alpha/Meme/AI/Layer-1...). Symbols already purged from
  exchangeInfo are recovered from the data.binance.vision S3 listing (monthly 1d zips).
- daily klines since listing: fapi /klines (1 call per symbol, sequential; the guardian shares the IP)
- spot listing date: data-api.binance.vision (market-data-only host) first 1d candle of BASEUSDT
- listing type: Binance CMS announcements (catalog 48 new listings, 161 delistings), the same public endpoint
  news.py uses
- supply/float: the guardian's CoinGecko cache (kv 'coin_supply' in data/shortbot.db, opened read-only)

Everything lands in data/lifecycle.db (never the desk DB).

    .venv/bin/python -m shortbot.lifecycle_data            # collect / top up
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .news import CMS_LIST, UA, normalize_base

FAPI = "https://fapi.binance.com/fapi/v1"
SPOT_DATA = "https://data-api.binance.vision/api/v3/klines"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
VISION = "https://data.binance.vision/"
LIFE_DB, DESK_DB = Path("data/lifecycle.db"), Path("data/shortbot.db")
SINCE = datetime(2025, 3, 1, tzinfo=UTC)          # ~18 months of listings
DAY_MS = 86_400_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (symbol TEXT PRIMARY KEY, base TEXT, onboard_ms INTEGER, status TEXT,
    delivery_ms INTEGER, utype TEXT, subtypes TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS klines (symbol TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, qv REAL,
    PRIMARY KEY (symbol, ts));
CREATE TABLE IF NOT EXISTS spot (base TEXT PRIMARY KEY, first_ms INTEGER);
CREATE TABLE IF NOT EXISTS announcements (catalog INTEGER, ts INTEGER, title TEXT, PRIMARY KEY (catalog, ts, title));
CREATE TABLE IF NOT EXISTS supply (base TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path: Path = LIFE_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=15)
    db.executescript(SCHEMA)
    return db


def _get(c: httpx.Client, url: str, **params):
    for attempt in range(5):
        try:
            r = c.get(url, params=params or None)
        except httpx.TransportError:
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code in (418, 429):
            time.sleep(30 * (attempt + 1))
            continue
        return r
    raise RuntimeError(f"giving up on {url}")


# ------------------------------------------------------------------ universe
def universe(c: httpx.Client, db: sqlite3.Connection) -> list[str]:
    since = SINCE.timestamp() * 1000
    info = _get(c, f"{FAPI}/exchangeInfo").json()["symbols"]
    rows = [(s["symbol"], normalize_base(s["baseAsset"]), s["onboardDate"], s["status"], s["deliveryDate"],
             s["underlyingType"], json.dumps(s.get("underlyingSubType") or []), "exchangeInfo")
            for s in info if s["contractType"] == "PERPETUAL" and s["quoteAsset"] == "USDT"
            and s["underlyingType"] == "COIN" and s["onboardDate"] >= since]
    db.executemany("INSERT OR REPLACE INTO coins VALUES (?,?,?,?,?,?,?,?)", rows)
    db.commit()
    known = {s["symbol"] for s in info}
    for sym in _s3_symbols(c):
        if sym.endswith("USDT") and sym not in known:
            _s3_recover(c, db, sym, since)
    db.commit()
    return [r[0] for r in db.execute("SELECT symbol FROM coins")]


def _s3_list(c: httpx.Client, prefix: str) -> tuple[list[str], list[str]]:
    dirs, keys, marker = [], [], ""
    while True:
        t = _get(c, S3, delimiter="/", prefix=prefix, marker=marker).text
        dirs += re.findall(r"<Prefix>([^<]+/)</Prefix>", t)[1:] if "<CommonPrefixes>" in t else []
        keys += re.findall(r"<Key>([^<]+\.zip)</Key>", t)
        if "<IsTruncated>true" not in t:
            return dirs, keys
        marker = re.search(r"<NextMarker>(.*?)</NextMarker>", t).group(1) if "<NextMarker>" in t else keys[-1]


def _s3_symbols(c: httpx.Client) -> list[str]:
    dirs, _ = _s3_list(c, "data/futures/um/monthly/klines/")
    return [d.rstrip("/").rsplit("/", 1)[-1] for d in dirs]


def _s3_recover(c: httpx.Client, db: sqlite3.Connection, sym: str, since: float) -> None:
    """A delisted perp that exchangeInfo no longer lists: listing month and klines from the monthly zips."""
    _, keys = _s3_list(c, f"data/futures/um/monthly/klines/{sym}/1d/")
    keys = sorted(keys)
    if not keys:
        return
    first = datetime.strptime(keys[0][-11:-4], "%Y-%m").replace(tzinfo=UTC).timestamp() * 1000
    if first < since - 31 * DAY_MS:
        return
    bars = []
    for k in keys:
        z = zipfile.ZipFile(io.BytesIO(_get(c, VISION + k).content))
        for line in z.read(z.namelist()[0]).decode().splitlines():
            f = line.split(",")
            if f[0].isdigit():
                bars.append((sym, int(f[0]), *map(float, f[1:6]), float(f[7])))
    if not bars or bars[0][1] < since:
        return
    db.executemany("INSERT OR REPLACE INTO klines VALUES (?,?,?,?,?,?,?,?)", bars)
    db.execute("INSERT OR REPLACE INTO coins VALUES (?,?,?,?,?,?,?,?)",
               (sym, normalize_base(sym[:-4]), bars[0][1], "PURGED", bars[-1][1] + DAY_MS, "COIN", "[]", "s3"))


# ------------------------------------------------------------------ prices
def klines(c: httpx.Client, db: sqlite3.Connection, symbols: list[str]) -> None:
    for sym in symbols + ["BTCUSDT"]:
        last = db.execute("SELECT MAX(ts) FROM klines WHERE symbol=?", (sym,)).fetchone()[0]
        start = last if last else (int(SINCE.timestamp() * 1000) - 60 * DAY_MS if sym == "BTCUSDT" else 0)
        r = _get(c, f"{FAPI}/klines", symbol=sym, interval="1d", startTime=start, limit=1000)
        if r.status_code != 200:
            continue
        rows = [(sym, k[0], *map(float, k[1:6]), float(k[7])) for k in r.json()
                if k[6] < time.time() * 1000]                       # closed candles only
        db.executemany("INSERT OR REPLACE INTO klines VALUES (?,?,?,?,?,?,?,?)", rows)
        db.commit()
        time.sleep(0.35)


def spot_dates(c: httpx.Client, db: sqlite3.Connection) -> None:
    done = {r[0] for r in db.execute("SELECT base FROM spot")}
    for (base,) in db.execute("SELECT DISTINCT base FROM coins").fetchall():
        if base in done:
            continue
        r = _get(c, SPOT_DATA, symbol=f"{base}USDT", interval="1d", startTime=0, limit=1)
        first = r.json()[0][0] if r.status_code == 200 and r.json() else None
        db.execute("INSERT OR REPLACE INTO spot VALUES (?,?)", (base, first))
        db.commit()
        time.sleep(0.2)


# ------------------------------------------------------------------ context
def announcements(c: httpx.Client, db: sqlite3.Connection) -> None:
    stop = SINCE.timestamp() * 1000 - 120 * DAY_MS
    for cid in (48, 161):
        for page in range(1, 60):
            r = _get(c, CMS_LIST, type=1, catalogId=cid, pageNo=page, pageSize=50)
            arts = r.json()["data"]["catalogs"][0]["articles"]
            db.executemany("INSERT OR IGNORE INTO announcements VALUES (?,?,?)",
                           [(cid, a["releaseDate"], a["title"]) for a in arts])
            if not arts or arts[-1]["releaseDate"] < stop:
                break
            time.sleep(0.5)
    db.commit()


def supply(db: sqlite3.Connection, desk_db: Path = DESK_DB) -> int:
    ro = sqlite3.connect(f"file:{desk_db}?mode=ro", uri=True, timeout=15)
    row = ro.execute("SELECT v FROM kv WHERE k='coin_supply'").fetchone()
    ro.close()
    coins = json.loads(row[0])["coins"] if row else {}
    db.executemany("INSERT OR REPLACE INTO supply VALUES (?,?)", [(b, json.dumps(s)) for b, s in coins.items()])
    db.execute("INSERT OR REPLACE INTO meta VALUES ('supply_asof', ?)", (json.loads(row[0])["ts"] if row else 0,))
    db.commit()
    return len(coins)


def collect(path: Path = LIFE_DB) -> dict:
    db = connect(path)
    with httpx.Client(headers=UA, timeout=30) as c:
        syms = universe(c, db)
        klines(c, db, syms)
        spot_dates(c, db)
        announcements(c, db)
    n_sup = supply(db)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('collected', ?)", (datetime.now(UTC).isoformat(),))
    db.commit()
    return {"coins": len(syms), "supply": n_sup,
            "bars": db.execute("SELECT COUNT(*) FROM klines").fetchone()[0]}


if __name__ == "__main__":
    print(collect())
