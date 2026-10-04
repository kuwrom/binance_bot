"""Per-entry features for the backtest, computed ONLY from data known at the close of bar i (no look-ahead; the
test truncates every coin at that close and checks the features do not change).

Market = every coin's aligned rows + cross-coin context (BTC, sectors). Coin = one perp with precomputed ATR%,
EMA20 and lookups. `features(m, coin, i)` returns the dict the report splits on."""

from __future__ import annotations

import itertools
import math
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..patterns import align
from ..sessions import session_bucket
from ..tape_math import next_round_above
from .sim import H, _funding_index

# one primary sector per coin from Binance underlyingSubType tags (narrative first, like market/sectors.py)
SECTOR_PRIORITY = ("Meme", "AI", "RWA", "Gaming", "NFT", "Metaverse", "DeFi", "Payment", "Storage", "PoW",
                   "Layer-2", "Layer-1", "Infrastructure")
EXTRA_KEYS = ("ls_top", "ls_top_acct", "ls_global", "taker_ratio")


def primary_sector(tags: list[str]) -> str:
    return next((t for t in SECTOR_PRIORITY if t in (tags or [])), "other")


def _ema(xs: list[float], n: int) -> list[float]:
    k, out, e = 2 / (n + 1), [], None
    for x in xs:
        e = x if e is None else e + k * (x - e)
        out.append(e)
    return out


def _atr_pct(rows: list[dict], n: int = 14) -> list[float]:
    """ATR% of bar i = mean true range of bars i-n+1..i over close(i) (bar i is complete at its close)."""
    tr = []
    for k, r in enumerate(rows):
        pc = rows[k - 1]["c"] if k else r["o"]
        tr.append(max(r["h"], pc) - min(r["l"], pc))
    out, s = [], 0.0
    for k in range(len(rows)):
        s += tr[k] - (tr[k - n] if k >= n else 0)
        out.append(s / min(k + 1, n) / rows[k]["c"] * 100 if rows[k]["c"] else 0.0)
    return out


@dataclass
class Coin:
    symbol: str
    rows: list[dict]
    onboard_ms: int
    tags: list[str]
    sector: str
    funding: list
    extra: dict[str, dict[int, float]]
    delist_ms: int | None = None
    atr: list[float] = field(default_factory=list)
    ema20: list[float] = field(default_factory=list)
    pos: dict[int, int] = field(default_factory=dict)
    fidx: dict[int, float] = field(default_factory=dict)

    @classmethod
    def build(cls, raw: dict[str, Any]) -> Coin:
        rows = [r for r in align(raw) if r["c"] and r["o"]]
        extra = {k: {int(t): float(v) for t, v in (raw.get(k) or {}).items() if v is not None} for k in EXTRA_KEYS}
        c = cls(raw["symbol"], rows, int(raw.get("onboard_ms") or 0), list(raw.get("tags") or []),
                primary_sector(raw.get("tags") or []), sorted(raw.get("funding") or []), extra,
                int(raw["delist_ms"]) if raw.get("delist_ms") else None)
        c.atr, c.ema20 = _atr_pct(rows), _ema([r["c"] for r in rows], 20)
        c.pos = {r["ts"]: k for k, r in enumerate(rows)}
        c.fidx = _funding_index(rows, c.funding)
        return c

    def close_at(self, ts: int) -> float | None:
        k = self.pos.get(ts)
        return self.rows[k]["c"] if k is not None else None

    def ret(self, i: int, back: int) -> float | None:
        return (self.rows[i]["c"] / self.rows[i - back]["c"] - 1) * 100 if i >= back else None

    def high(self, a: int, b: int) -> float:
        """max high of bars a..b-1."""
        return max(r["h"] for r in self.rows[max(0, a):b])


class Market:
    def __init__(self, raw_coins: list[dict[str, Any]]):
        self.coins = {c.symbol: c for c in (Coin.build(r) for r in raw_coins) if len(c.rows) >= 96}
        self.btc = next((c for s, c in self.coins.items() if s.startswith("BTC/")), None)
        self.sectors: dict[str, list[Coin]] = {}
        for c in self.coins.values():
            self.sectors.setdefault(c.sector, []).append(c)
        self._sec_cache: dict[tuple[str, int, int], float | None] = {}
        self.cache: dict[Any, Any] = {}   # per-strategy precomputations (pump bars, ...)
        ts = [r["ts"] for c in self.coins.values() for r in (c.rows[0], c.rows[-1])]
        self.t0, self.t1 = (min(ts), max(ts)) if ts else (0, 0)

    def sector_ret(self, sector: str, ts: int, back: int, exclude: str | None = None) -> float | None:
        """Median `back`-hour % return of the sector's members at bar `ts` (the coin itself excluded)."""
        key = (sector, ts, back)
        if key not in self._sec_cache or exclude:
            xs = []
            for c in self.sectors.get(sector, []):
                if c.symbol == exclude:
                    continue
                a, b = c.close_at(ts - back * H), c.close_at(ts)
                if a and b:
                    xs.append((b / a - 1) * 100)
            v = statistics.median(xs) if len(xs) >= 3 else None
            if exclude:
                return v
            self._sec_cache[key] = v
        return self._sec_cache[key]

    def beta_corr(self, coin: Coin, i: int, n: int = 72) -> tuple[float | None, float | None]:
        """OLS beta and correlation of the coin's 1h log returns on BTC's over the n hours ending at bar i."""
        if not self.btc or coin is self.btc or i < n:
            return (1.0, 1.0) if coin is self.btc else (None, None)
        xs, ys = [], []
        for k in range(i - n + 1, i + 1):
            ts, pts = coin.rows[k]["ts"], coin.rows[k - 1]["ts"]
            a, b = self.btc.close_at(pts), self.btc.close_at(ts)
            if a and b:
                xs.append(math.log(b / a))
                ys.append(math.log(coin.rows[k]["c"] / coin.rows[k - 1]["c"]))
        if len(xs) < n // 2:
            return None, None
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        syy = sum((y - my) ** 2 for y in ys)
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
        if not sxx or not syy:
            return None, None
        return sxy / sxx, sxy / math.sqrt(sxx * syy)


def _extra_at(coin: Coin, key: str, t_close: int) -> float | None:
    """Latest value of a {hour_end_ts: v} series known at t_close (end-of-hour stamps)."""
    d = coin.extra.get(key) or {}
    for back in range(0, 3):
        v = d.get(t_close - back * H)
        if v is not None:
            return v
    return None


def efficiency_ratio(closes: list[float]) -> float | None:
    """|net move| / path length: 1 = a one-direction run, ~0 = chop."""
    path = sum(abs(b - a) for a, b in itertools.pairwise(closes))
    return abs(closes[-1] - closes[0]) / path if path else None


def false_break(rows: list[dict], i: int, look: int = 24) -> bool:
    """Did any bar of the last `look` hours wick above its prior-24h high and still close below it?"""
    for k in range(max(24, i - look + 1), i + 1):
        lvl = max(r["h"] for r in rows[k - 24:k])
        if rows[k]["h"] > lvl * 1.001 and rows[k]["c"] < lvl:
            return True
    return False


def features(m: Market, coin: Coin, i: int) -> dict[str, Any]:
    rows, r = coin.rows, coin.rows[i]
    t_close = r["ts"] + H
    dt = datetime.fromtimestamp(t_close / 1000, UTC)
    v24 = sum(x["qv"] for x in rows[max(0, i - 23):i + 1])
    oi = r.get("oi")
    beta, corr = m.beta_corr(coin, i)
    r4 = coin.ret(i, 4)
    btc4 = btc24 = None
    if m.btc:
        a, b, a24 = m.btc.close_at(r["ts"] - 4 * H), m.btc.close_at(r["ts"]), m.btc.close_at(r["ts"] - 24 * H)
        btc4 = (b / a - 1) * 100 if a and b else None
        btc24 = (b / a24 - 1) * 100 if a24 and b else None
    sec4 = m.sector_ret(coin.sector, r["ts"], 4, exclude=coin.symbol)
    sync = None
    if r4 is not None:
        if abs(r4) < 1.0:
            sync = "quiet"
        else:
            by_btc = btc4 is not None and beta is not None and beta * btc4 * r4 > 0 and abs(beta * btc4) >= 0.5 * abs(r4)
            by_sec = sec4 is not None and sec4 * r4 > 0 and abs(sec4) >= 0.5 * abs(r4)
            sync = "sync" if by_btc or by_sec else "idio"
    oi4 = rows[i - 4].get("oi") if i >= 4 else None
    oi24 = rows[i - 24].get("oi") if i >= 24 else None
    fund = next((rate for ts, rate in reversed(coin.funding) if ts <= t_close), None)
    tq = sum(x["qv"] for x in rows[max(0, i - 3):i + 1])
    tb = sum(x["tbq"] for x in rows[max(0, i - 3):i + 1])
    ls_top, ls_glob = _extra_at(coin, "ls_top", t_close), _extra_at(coin, "ls_global", t_close)
    wr = ls_top / ls_glob if ls_top and ls_glob else None
    hi24 = coin.high(i - 23, i + 1)
    closes = [x["c"] for x in rows[max(0, i - 24):i + 1]]
    age = (t_close - coin.onboard_ms) / 864e5 if coin.onboard_ms else None
    return {
        "hour": dt.hour, "session": session_bucket(dt), "weekday": dt.strftime("%a"), "weekend": dt.weekday() >= 5,
        "vol24_m": v24 / 1e6, "oi_m": oi / 1e6 if oi else None, "oi_vol": oi / v24 if oi and v24 else None,
        "atr_pct": coin.atr[i], "age_d": age, "has_spot": r.get("spot_c") is not None,
        "alpha": "Alpha" in coin.tags, "sector": coin.sector,
        "beta": beta, "corr": corr, "ret_1h": coin.ret(i, 1), "ret_4h": r4, "ret_24h": coin.ret(i, 24),
        "btc_ret_4h": btc4, "btc_ret_24h": btc24, "sector_ret_4h": sec4,
        "resid_4h": r4 - beta * btc4 if r4 is not None and beta is not None and btc4 is not None else None,
        "sync": sync,
        "oi_chg_4h": (oi / oi4 - 1) * 100 if oi and oi4 else None,
        "oi_chg_24h": (oi / oi24 - 1) * 100 if oi and oi24 else None,
        "funding_pct": fund * 100 if fund is not None else None,
        "premium_bp": r["prem"] * 1e4 if r.get("prem") is not None else None,
        "ls_top": ls_top, "ls_top_acct": _extra_at(coin, "ls_top_acct", t_close), "ls_global": ls_glob,
        "taker_ratio": _extra_at(coin, "taker_ratio", t_close), "whale_retail": wr,
        "who_long": None if wr is None else "whales_longer" if wr > 1.1 else "retail_longer" if wr < 0.9 else "aligned",
        "ti_4h": (2 * tb - tq) / tq if tq else None,
        "er_24h": efficiency_ratio(closes) if len(closes) > 2 else None,
        "dist_high24_pct": (hi24 / r["c"] - 1) * 100, "dist_round_pct": (next_round_above(r["c"]) / r["c"] - 1) * 100,
        "false_break_24h": false_break(rows, i) if i >= 24 else None,
    }
