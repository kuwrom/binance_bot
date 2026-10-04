"""Market map (docs/MARKET_MAP.md): read the whole tape at once. Every coin's move is split into BTC beta +
sector + its own residual, sectors are ranked by residual strength, and the few whole-market numbers (breadth,
dispersion, correlation) label the regime. The guardian builds it every 10 minutes and caches it; the `sector`
signal reads the cache to veto shorting into a sector-wide rally or a rotation, and the analyst reads the table.
Math is pure (unit-tested); `build_map` does the I/O. Thresholds are starting assumptions, logged for testing."""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import statistics
import time
from typing import Any

from .. import clock
from .sectors import base_of, sector_map

BETA_SHRINK, BETA_CLIP = 0.3, (0.0, 3.0)
MIN_SECTOR = 5
SECTOR_RALLY = (3.0, 70.0)          # median 4h % and breadth % -> veto shorts in that sector
SQUEEZE = (75.0, 1.5)               # market breadth % and BTC 1h % -> pause new shorts
BTC_IMPULSE_15M = 0.8               # BTC or ETH % in 15 min
KV_LAST = "market_map_last"
KV_PREV_RANKS = "market_map_ranks"


def log_returns(closes: list[float]) -> list[float]:
    return [math.log(b / a) for a, b in itertools.pairwise(closes) if a > 0 and b > 0]


def beta(coin: list[float], btc: list[float]) -> float:
    """OLS beta of 1h log returns on BTC, shrunk toward 1 and clipped."""
    n = min(len(coin), len(btc))
    if n < 24:
        return 1.0
    x, y = btc[-n:], coin[-n:]
    mx, my = statistics.fmean(x), statistics.fmean(y)
    vx = sum((a - mx) ** 2 for a in x)
    b = sum((a - mx) * (c - my) for a, c in zip(x, y, strict=True)) / vx if vx else 1.0
    b = (1 - BETA_SHRINK) * b + BETA_SHRINK * 1.0
    return max(BETA_CLIP[0], min(BETA_CLIP[1], b))


def pct(closes: list[float], bars: int) -> float | None:
    if len(closes) <= bars or not closes[-bars - 1]:
        return None
    return (closes[-1] / closes[-bars - 1] - 1) * 100


def ema(xs: list[float], n: int) -> float | None:
    if len(xs) < n:
        return None
    k, e = 2 / (n + 1), statistics.fmean(xs[:n])
    for x in xs[n:]:
        e = x * k + e * (1 - k)
    return e


def coin_row(sym: str, c1h: list[list[float]], btc_rets: list[float], btc_moves: dict[str, float | None]) -> dict:
    closes = [c[4] for c in c1h]
    qv = [c[5] * c[4] for c in c1h]                   # quote volume per hour
    rets = log_returns(closes)
    b = beta(rets, btc_rets)
    moves = {h: pct(closes, n) for h, n in (("1h", 1), ("4h", 4), ("24h", 24))}
    resid = {h: (moves[h] - b * btc_moves[h]) if moves[h] is not None and btc_moves.get(h) is not None else None
             for h in moves}
    res = [r - b * x for r, x in zip(rets[-len(btc_rets):], btc_rets[-len(rets):], strict=False)] if btc_rets else []
    resid_vol = statistics.pstdev(res) * 100 if len(rets) > 24 and len(res) > 1 else None
    base_vol = statistics.fmean(qv[-170:-2]) if len(qv) > 26 else None   # [-1] is the still-forming hour
    e20 = ema(closes, 20)
    return {"symbol": sym, "beta": round(b, 2), **{f"r{h}": _r(v) for h, v in moves.items()},
            **{f"resid{h}": _r(v) for h, v in resid.items()}, "resid_vol_1h": _r(resid_vol),
            "above_ema20": bool(e20 and closes[-1] > e20), "vol_surge": _r(qv[-2] / base_vol if base_vol else None),
            "rets": rets[-168:]}


def _r(v: float | None, n: int = 2) -> float | None:
    return None if v is None else round(v, n)


def _med(xs: list[float | None]) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def market_header(rows: list[dict], btc: dict, eth: dict) -> dict[str, Any]:
    n = len(rows)
    breadth = sum(r["above_ema20"] for r in rows) / n * 100 if n else None
    up = sum(1 for r in rows if (r["r1h"] or 0) > 0)
    r1 = [r["r1h"] for r in rows if r["r1h"] is not None]
    med = statistics.median(r1) if r1 else 0.0
    disp = statistics.median([abs(x - med) for x in r1]) if r1 else None
    series = [r["rets"] for r in rows if len(r["rets"]) >= 48]
    corr = None
    if len(series) >= 10:
        m = min(len(s) for s in series)
        idx = [statistics.fmean(s[-m + i] for s in series) for i in range(m)]
        mean_var = statistics.fmean(statistics.pvariance(s[-m:]) for s in series)
        corr = statistics.pvariance(idx) / mean_var if mean_var else None
    regime = ("RISK-ON" if breadth is not None and breadth > 65 and (btc.get("4h") or 0) > 0
              else "RISK-OFF" if breadth is not None and breadth < 35 else "MIXED")
    return {"coins": n, "btc": btc, "eth": eth, "breadth_pct": _r(breadth, 1),
            "adv_decl_1h": _r(up / max(n - up, 1)), "dispersion_1h": _r(disp), "corr_proxy": _r(corr),
            "regime": regime}


def sector_table(rows: list[dict], sectors: dict[str, str]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(sectors.get(base_of(r["symbol"]), "other"), []).append(r)
    table = []
    for name, rs in groups.items():
        if len(rs) < MIN_SECTOR or name == "other":
            continue
        m24 = _med([r["resid24h"] for r in rs]) or 0.0
        for r in rs:
            r["sector"] = name
            if r["resid24h"] is not None:
                r["resid_sector24h"] = round(r["resid24h"] - m24, 2)
                r["resid_sector_z"] = _r((r["resid24h"] - m24) / (r["resid_vol_1h"] * math.sqrt(24)))\
                    if r.get("resid_vol_1h") else None
        ranked = sorted((r for r in rs if r.get("resid_sector24h") is not None), key=lambda r: r["resid_sector24h"])
        table.append({"sector": name, "n": len(rs), "r1h": _r(_med([r["r1h"] for r in rs])),
                      "r4h": _r(_med([r["r4h"] for r in rs])), "r24h": _r(_med([r["r24h"] for r in rs])),
                      "resid4h": _r(_med([r["resid4h"] for r in rs])), "resid24h": _r(m24),
                      "breadth_pct": round(sum(r["above_ema20"] for r in rs) / len(rs) * 100),
                      "vol_surge": _r(_med([r["vol_surge"] for r in rs])),
                      "leader": ranked[-1]["symbol"].split("/")[0] if ranked else None,
                      "laggard": ranked[0]["symbol"].split("/")[0] if ranked else None})
    table.sort(key=lambda s: s["resid4h"] if s["resid4h"] is not None else -99, reverse=True)
    for i, s in enumerate(table):
        s["rank4h"] = i + 1
    return table


def rotation_flags(table: list[dict], prev_ranks: list[dict[str, int]]) -> None:
    """rotation_in: sector was in the bottom half two maps ago, is top 3 now AND was top 3 last map (held 20 min),
    with breadth > 60% and volume surge > 1.5x. rotation_out mirrors it (top 3 -> bottom half, held)."""
    half = len(table) / 2
    two_back, one_back = (prev_ranks[-2], prev_ranks[-1]) if len(prev_ranks) >= 2 else ({}, {})
    for s in table:
        a, b, now = two_back.get(s["sector"]), one_back.get(s["sector"]), s["rank4h"]
        s["rotation_in"] = bool(a is not None and b is not None and a > half and b <= 3 and now <= 3
                                and s["breadth_pct"] > 60 and (s["vol_surge"] or 0) > 1.5)
        s["rotation_out"] = bool(a is not None and b is not None and a <= 3 and b > half and now > half)


def market_vetoes(header: dict, table: list[dict], btc_15m: float | None, eth_15m: float | None) -> dict[str, Any]:
    """Market-wide pause + per-sector vetoes (the `sector` signal applies them per coin)."""
    pause = []
    if (header["breadth_pct"] or 0) > SQUEEZE[0] and (header["btc"].get("1h") or 0) > SQUEEZE[1]:
        pause.append(f"market squeeze: breadth {header['breadth_pct']}% and BTC 1h {header['btc']['1h']}%")
    for name, v in (("BTC", btc_15m), ("ETH", eth_15m)):
        if v is not None and v > BTC_IMPULSE_15M:
            pause.append(f"{name} +{v:.2f}% in 15 min: alts catch up, wait")
    sectors = {}
    for s in table:
        why = []
        if (s["r4h"] or 0) > SECTOR_RALLY[0] and s["breadth_pct"] > SECTOR_RALLY[1]:
            why.append(f"{s['sector']} sector rallying (median 4h {s['r4h']}%, breadth {s['breadth_pct']}%)")
        if s["rotation_in"]:
            why.append(f"money rotating INTO {s['sector']}")
        sectors[s["sector"]] = {"vetoes": why, "leader": s["leader"], "rank4h": s["rank4h"],
                                "resid24h": s["resid24h"], "rotation_out": s["rotation_out"]}
    return {"pause": pause, "sectors": sectors}


async def _klines(ex, sym: str, sem: asyncio.Semaphore, limit: int = 170) -> list[list[float]]:
    async with sem:
        try:
            return await ex.public.fetch_ohlcv(sym, "1h", limit=limit)
        except Exception:  # noqa: BLE001 - one coin missing never blocks the map
            return []


async def build_map(ex, journal, min_quote_vol: float = 10_000_000) -> dict[str, Any]:
    t0 = time.time()  # wall-clock: build timing
    perps = await ex.coin_perps()
    tickers, rates, sectors = await asyncio.gather(ex.public.fetch_tickers(), ex.public.fetch_funding_rates(),
                                                   sector_map(journal))
    universe = [s for s in perps if (tickers.get(s, {}).get("quoteVolume") or 0) >= min_quote_vol]
    sem = asyncio.Semaphore(12)
    majors = ("BTC/USDT:USDT", "ETH/USDT:USDT")
    kl = await asyncio.gather(*(_klines(ex, s, sem) for s in [*majors, *universe]))
    data = dict(zip([*majors, *universe], kl, strict=True))
    btc_c = [c[4] for c in data[majors[0]]]
    if len(btc_c) < 30:   # BTC klines failed (timeout): a map without its benchmark would be wrong - keep the old one
        raise RuntimeError(f"BTC klines unavailable ({len(btc_c)} bars): market map not rebuilt this cycle")
    btc_rets = log_returns(btc_c)
    mv = lambda cl: {h: _r(pct(cl, n)) for h, n in (("1h", 1), ("4h", 4), ("24h", 24))}  # noqa: E731
    btc, eth = mv(btc_c), mv([c[4] for c in data[majors[1]]])
    rows = [coin_row(s, data[s], btc_rets, btc) for s in universe if len(data[s]) >= 30 and s not in majors]
    for r in rows:
        r["funding"] = (rates.get(r["symbol"]) or {}).get("fundingRate")
    header = market_header(rows, btc, eth)
    table = sector_table(rows, sectors)
    fr_all = _med([r["funding"] for r in rows])
    for s in table:
        s["funding_tilt_bp"] = _r(((_med([r["funding"] for r in rows if r.get("sector") == s["sector"]]) or 0)
                                   - (fr_all or 0)) * 1e4)
    prev = json.loads(journal.get(KV_PREV_RANKS) or "[]")
    rotation_flags(table, prev)
    ranks = json.dumps((prev + [{s["sector"]: s["rank4h"] for s in table}])[-3:])
    journal.mark(KV_PREV_RANKS, ranks)   # the next build reads it back, also while it waits for the lock (#82)
    b15, e15 = await asyncio.gather(*(ex.public.fetch_ohlcv(m, "1m", limit=16) for m in majors),
                                    return_exceptions=True)
    m15 = [(c[-1][4] / c[0][1] - 1) * 100 if isinstance(c, list) and c else None for c in (b15, e15)]
    vetoes = market_vetoes(header, table, *m15)
    coins = {r["symbol"]: {k: r.get(k) for k in ("sector", "beta", "r1h", "r4h", "r24h", "resid4h", "resid24h",
                                                 "resid_sector24h", "resid_sector_z", "vol_surge")} for r in rows}
    out = {"ts": int(clock.now_s()), "built_s": round(time.time() - t0, 1), "header": header,  # wall-clock: built_s
           "btc_15m": _r(m15[0]), "eth_15m": _r(m15[1]), "sectors": table, "vetoes": vetoes, "coins": coins}
    cache = json.dumps(out)
    journal.mark(KV_LAST, cache)   # the protective lane's sweep reads its pause at once (entry_gates, #82)
    return out


def _fresh(m: dict[str, Any], max_age_s: float) -> bool:
    """Built at most max_age_s before now and not after it (a replay pins the clock; 60 s of skew allowed)."""
    age = clock.now_s() - m["ts"]
    return -60 <= age <= max_age_s


def cached(journal, max_age_s: float = 1800) -> dict[str, Any] | None:
    raw = journal.get(KV_LAST)
    if not raw:
        return None
    m = json.loads(raw)
    return m if _fresh(m, max_age_s) else None


class MapCache:
    """Read-only access to the guardian's cached map from the shared DB (for signals via the DataHub)."""

    def __init__(self, db_path):
        import sqlite3
        self.db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=15)

    def get(self, max_age_s: float = 1800) -> dict[str, Any] | None:
        try:
            row = self.db.execute("SELECT v FROM kv WHERE k=?", (KV_LAST,)).fetchone()
        except Exception:  # noqa: BLE001 - no kv table yet
            return None
        m = json.loads(row[0]) if row else None
        return m if m and _fresh(m, max_age_s) else None


def render(m: dict[str, Any]) -> str:
    """30-second read: header, sectors strongest->weakest vs BTC, then vetoes."""
    h = m["header"]
    lines = [f"MAP {time.strftime('%H:%M', time.gmtime(m['ts']))} UTC | {h['regime']} | breadth {h['breadth_pct']}% "
             f"A/D {h['adv_decl_1h']} | disp {h['dispersion_1h']} corr {h['corr_proxy']} | BTC 1h/4h/24h "
             f"{h['btc'].get('1h')}/{h['btc'].get('4h')}/{h['btc'].get('24h')} (15m {m.get('btc_15m')}) | "
             f"ETH 4h {h['eth'].get('4h')} | {h['coins']} coins",
             "sector      n   1h    4h    24h  res4h res24h brd  vol  fund  leader/laggard  flags"]
    for s in m["sectors"]:
        flags = ("ROT-IN " if s.get("rotation_in") else "") + ("ROT-OUT" if s.get("rotation_out") else "")
        lines.append(f"{s['sector']:<10}{s['n']:>3} {s['r1h']:>5} {s['r4h']:>5} {s['r24h']:>6} {s['resid4h']:>5} "
                     f"{s['resid24h']:>6} {s['breadth_pct']:>3}% {s['vol_surge']:>4} {s.get('funding_tilt_bp')!s:>5} "
                     f"{s['leader']}/{s['laggard']}  {flags}")
    v = m["vetoes"]
    lines += [f"PAUSE: {p}" for p in v["pause"]]
    lines += [f"VETO {k}: {'; '.join(x['vetoes'])}" for k, x in v["sectors"].items() if x["vetoes"]]
    return "\n".join(lines)


def log_snapshot(journal, m: dict[str, Any]) -> None:
    """Keep every map (header + sectors) so the thresholds can be tested against outcomes later. A unit (the guardian
    awaits journal.write(log_snapshot, m))."""
    journal.db.execute("CREATE TABLE IF NOT EXISTS market_maps (ts INTEGER PRIMARY KEY, header TEXT, sectors TEXT,"
                       " vetoes TEXT)")
    journal.db.execute("INSERT OR REPLACE INTO market_maps VALUES (?,?,?,?)",
                       (m["ts"], json.dumps(m["header"]), json.dumps(m["sectors"]), json.dumps(m["vetoes"])))
