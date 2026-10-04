"""data.binance.vision archive access for histdata.py: S3 listings, zip downloads and CSV parsers.

Verified file formats (2026-09):
- futures/um klines + premiumIndexKlines (monthly `SYM-1h-YYYY-MM.zip`, daily `SYM-1h-YYYY-MM-DD.zip`): header row
  `open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore`,
  times in ms. Premium klines use the same columns (close = premium fraction, volumes 0).
- spot klines: NO header, and times in MICROseconds since 2025-01-01 (ms before).
- futures/um daily `metrics` (`SYM-metrics-YYYY-MM-DD.zip`): header `create_time,symbol,sum_open_interest,
  sum_open_interest_value,count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,count_long_short_ratio,
  sum_taker_long_short_vol_ratio`; create_time is a UTC string, rows are NOT sorted. Checked against the REST
  /futures/data endpoints: a row stamped T holds the OI / long-short snapshots REST reports at T+5m, and the taker
  buy/sell ratio of the 5 minutes [T, T+5m) (REST takerlongshortRatio stamped T).
- futures/um monthly `fundingRate` (`SYM-fundingRate-YYYY-MM.zip`, monthly only): header
  `calc_time,funding_interval_hours,last_funding_rate`, calc_time in ms (a few ms of jitter).
"""

from __future__ import annotations

import asyncio
import io
import re
import zipfile
from datetime import UTC, datetime
from typing import Any

import httpx

BASE = "https://data.binance.vision/"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
HOUR = 3_600_000
DAY = 86_400_000
FIVE_MIN = 300_000
UM = "data/futures/um"
SPOT = "data/spot"

_KEY = re.compile(r"<Key>([^<]+)</Key>")
_PREFIX = re.compile(r"<Prefix>([^<]+)</Prefix>")
_TRUNC = re.compile(r"<IsTruncated>(\w+)</IsTruncated>")
_NEXT = re.compile(r"<NextMarker>([^<]+)</NextMarker>")


class Fetcher:
    """Shared httpx client + request semaphore (data.binance.vision is a CDN, not the Binance API weight)."""

    def __init__(self, concurrency: int = 8):
        self.sem = asyncio.Semaphore(concurrency)
        self.client = httpx.AsyncClient(timeout=60, limits=httpx.Limits(max_connections=concurrency * 2))
        self.requests = 0

    async def close(self) -> None:
        await self.client.aclose()

    async def get(self, url: str, params: dict | None = None) -> httpx.Response | None:
        """GET with retries; None on 404."""
        status = None
        for attempt in range(5):
            try:
                async with self.sem:
                    self.requests += 1
                    r = await self.client.get(url, params=params)
                status = r.status_code
                if status == 404:
                    return None
                if status == 200:
                    return r
            except httpx.HTTPError:
                if attempt == 4:
                    raise
            await asyncio.sleep(1.5 * 2 ** attempt)
        raise RuntimeError(f"GET {url} failed: HTTP {status}")

    async def list(self, prefix: str, marker: str = "", delimiter: str | None = None,
                   max_keys: int | None = None) -> tuple[list[str], list[str]]:
        """S3 listing (paginated): (keys, common prefixes). Keys sort lexicographically = chronologically here."""
        keys: list[str] = []
        prefixes: list[str] = []
        while True:
            params = {"prefix": prefix, "marker": marker}
            if delimiter:
                params["delimiter"] = delimiter
            if max_keys:
                params["max-keys"] = str(max_keys)
            r = await self.get(S3, params)
            text = r.text if r is not None else ""
            k, p = _KEY.findall(text), [x for x in _PREFIX.findall(text) if x != prefix]
            keys += k
            prefixes += p
            if max_keys or _TRUNC.findall(text) != ["true"] or not (k or p):
                return keys, prefixes
            nxt = _NEXT.findall(text)
            marker = nxt[0] if nxt else max(k + p)

    async def csv(self, key: str) -> str | None:
        """Download a zip and return its first member as text (None if the file does not exist)."""
        r = await self.get(BASE + key)
        if r is None:
            return None
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            return z.read(z.namelist()[0]).decode()


# ------------------------------------------------------------ parsers
def _f(x: str) -> float | None:
    try:
        return float(x)
    except ValueError:
        return None


def parse_klines(text: str) -> list[dict[str, Any]]:
    """Kline CSV (with or without header; ms or µs times) -> [{"ts","o","h","l","c","qv","n","tbq"}] sorted."""
    out = []
    for line in text.splitlines():
        if not line or not line[0].isdigit():
            continue
        r = line.split(",")
        ts = int(r[0])
        if ts > 10 ** 14:          # spot files since 2025 use microseconds
            ts //= 1000
        out.append({"ts": ts, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                    "qv": float(r[7]), "n": int(float(r[8])), "tbq": float(r[10])})
    return sorted(out, key=lambda b: b["ts"])


METRIC_COLS = {"sum_open_interest": "oi", "sum_open_interest_value": "oi_val",
               "count_toptrader_long_short_ratio": "ls_top_acct", "sum_toptrader_long_short_ratio": "ls_top",
               "count_long_short_ratio": "ls_global", "sum_taker_long_short_vol_ratio": "taker"}
METRIC_HEADER = ["create_time", "symbol", *METRIC_COLS]
SNAPSHOT_FIELDS = ("oi", "oi_val", "ls_top_acct", "ls_top", "ls_global")


def _ms(stamp: str) -> int:
    return int(datetime.fromisoformat(stamp).replace(tzinfo=UTC).timestamp() * 1000)


def parse_metrics(text: str) -> list[dict[str, Any]]:
    """Daily metrics CSV -> 5-minute rows {"ts": create_time ms, oi, oi_val, ls_top_acct, ls_top, ls_global,
    taker}, sorted (the files are not)."""
    lines = [x for x in text.splitlines() if x.strip()]
    if not lines:
        return []
    header = METRIC_HEADER
    if not lines[0][0].isdigit():
        header, lines = lines[0].strip().split(","), lines[1:]
    idx = {METRIC_COLS[h]: i for i, h in enumerate(header) if h in METRIC_COLS}
    ti = header.index("create_time")
    out = []
    for line in lines:
        r = line.split(",")
        row: dict[str, Any] = {"ts": _ms(r[ti])}
        row.update({k: (_f(r[i]) if i < len(r) else None) for k, i in idx.items()})
        out.append(row)
    return sorted(out, key=lambda x: x["ts"])


def metrics_hourly(rows: list[dict[str, Any]]) -> dict[int, dict[str, float | None]]:
    """5-minute metrics -> {hour_end_ms: {...}}. Snapshots (OI, long/short ratios) take the last value observed at
    or before the hour end (observation time = create_time + 5m); `taker` is the mean of the hour's twelve 5-minute
    buy/sell ratios (a ratio has no meaningful 'last value')."""
    out: dict[int, dict[str, Any]] = {}
    seen: dict[int, int] = {}
    taker: dict[int, list[float]] = {}
    for r in rows:
        eff = r["ts"] + FIVE_MIN
        h = -(-eff // HOUR) * HOUR
        b = out.setdefault(h, {k: None for k in (*SNAPSHOT_FIELDS, "taker")})
        if eff >= seen.get(h, 0):
            seen[h] = eff
            for k in SNAPSHOT_FIELDS:
                if r.get(k) is not None:
                    b[k] = r[k]
        if r.get("taker") is not None:
            taker.setdefault(r["ts"] // HOUR * HOUR + HOUR, []).append(r["taker"])
    for h, v in taker.items():
        out.setdefault(h, {k: None for k in (*SNAPSHOT_FIELDS, "taker")})["taker"] = sum(v) / len(v)
    return out


def parse_funding(text: str) -> list[list]:
    """fundingRate CSV -> [[funding_time_ms (floored to the second), rate], ...] sorted."""
    out = []
    for line in text.splitlines():
        if not line or not line[0].isdigit():
            continue
        r = line.split(",")
        ts = int(r[0])
        out.append([ts - ts % 1000, float(r[-1])])
    return sorted(out)


# ------------------------------------------------------------ keys and plans
def spot_candidates(raw_perp: str) -> list[tuple[str, float]]:
    """Spot pairs that may track a perp, best first, with the price multiplier (1000PEPE perp = 1000 x PEPE).
    The unprefixed-exact pair wins when it exists (e.g. 1000SATSUSDT trades on spot as is)."""
    out = [(raw_perp, 1.0)]
    base = raw_perp[:-4]
    for prefix, m in (("1000000", 1e6), ("1000", 1e3), ("1M", 1e6)):
        if base.startswith(prefix) and len(base) > len(prefix):
            out.append((f"{base[len(prefix):]}USDT", m))
            break
    return out


def day_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def series_prefixes(kind: str, raw: str) -> tuple[str | None, str | None, str]:
    """(monthly prefix, daily prefix, file stem) for a series kind: perp | prem | metrics | funding | spot."""
    if kind == "perp":
        return f"{UM}/monthly/klines/{raw}/1h/", f"{UM}/daily/klines/{raw}/1h/", f"{raw}-1h-"
    if kind == "prem":
        return (f"{UM}/monthly/premiumIndexKlines/{raw}/1h/", f"{UM}/daily/premiumIndexKlines/{raw}/1h/",
                f"{raw}-1h-")
    if kind == "metrics":
        return None, f"{UM}/daily/metrics/{raw}/", f"{raw}-metrics-"
    if kind == "funding":
        return f"{UM}/monthly/fundingRate/{raw}/", None, f"{raw}-fundingRate-"
    if kind == "spot":
        return f"{SPOT}/monthly/klines/{raw}/1h/", f"{SPOT}/daily/klines/{raw}/1h/", f"{raw}-1h-"
    raise ValueError(kind)


def _period(key: str) -> str:
    """'.../CRVUSDT-1h-2026-08.zip' -> '2026-08'; daily -> '2026-08-01'."""
    stem = key.rsplit("/", 1)[-1][:-4]
    return stem[-10:] if re.search(r"\d{4}-\d{2}-\d{2}$", stem) else stem[-7:]


def plan_keys(monthly: list[str], daily: list[str], start_ms: int, today_ms: int) -> list[str]:
    """Files to download for [start, today): a complete month's monthly file when published, else its daily files."""
    monthly = [k for k in monthly if k.endswith(".zip")]
    daily = [k for k in daily if k.endswith(".zip")]
    start_d, today_d = day_str(start_ms), day_str(today_ms)
    cur_month = today_d[:7]
    months = {_period(k): k for k in monthly if start_d[:7] <= _period(k) < cur_month}
    out = list(months.values())
    out += [k for k in daily if start_d <= _period(k) < today_d and _period(k)[:7] not in months]
    return sorted(out)


async def list_series(f: Fetcher, kind: str, raw: str, start_ms: int) -> tuple[list[str], list[str]]:
    """(monthly keys, daily keys) at or after the window start."""
    mp, dp, stem = series_prefixes(kind, raw)
    d0 = day_str(start_ms)
    m = (await f.list(mp, marker=f"{mp}{stem}{d0[:7]}"))[0] if mp else []
    d = (await f.list(dp, marker=f"{dp}{stem}{d0}"))[0] if dp else []
    return m, d


async def first_day_ms(f: Fetcher, raw: str) -> int | None:
    """Midnight of the first daily perp kline file ever archived (listing proxy for the onboard date)."""
    _, dp, _ = series_prefixes("perp", raw)
    if dp is None:   # never: perp klines have daily files (series_prefixes)
        raise ValueError(f"{raw}: no daily perp prefix")
    keys, _ = await f.list(dp, max_keys=1)
    if not keys:
        return None
    return _ms(_period(keys[0]) + " 00:00:00")
