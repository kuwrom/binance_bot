"""Hedge overlay for backtested shorts (docs/HEDGE.md): each short gets a LONG hedge leg (BTC perp, ETH perp or an
equal-weight sector basket) sized `ratio x the short's initial notional`, opened at the short's entry and closed at
its final exit. The short itself is unchanged. Everything is reported in the SHORT's R (hedge P&L % of the short's
notional / the short's risk %).

Conventions (pre-registered in docs/HEDGE.md, conservative where there is a choice):
- A market short enters at the signal bar's close: the hedge buys that bar's close. A limit / sell-stop fill is at
  an unknown minute inside the fill bar: the hedge buys (open + close) / 2 of that bar.
- A time / eod / funding-budget exit is at a bar close: the hedge sells that close. Stops, targets and breakeven
  fill inside the exit bar: the hedge sells (open + close) / 2.
- Taker fee on both legs, slippage = Costs.slip(the instrument's 24h volume at entry) on both legs (1x: a hedge
  exit is a plain market order).
- Funding: the sim's print window (a close-time entry comes right after that hour's print, a close-time exit right
  after the next one), paid on the notional at the print bar's open. A LONG PAYS positive funding.
Read-only: hist.db is opened with mode=ro."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .features import primary_sector
from .sim import COSTS, Costs, H
from .stats import cluster_t, max_dd_r, sharpe

CLOSE_EXITS = {"time", "eod", "fund_budget"}
RATIO_CAP = 3.0
BASKET_MAX, BASKET_MIN, BASKET_VOL = 10, 3, 5e6
ALT_N, ALT_LOOKBACK = 20, 168                       # top-20 alts by 7-day quote volume ending at the entry bar
NOT_ALTS = ("BTC/", "ETH/", "USDC/", "PAXG/", "XAUT/")
SHRINK_LO, SHRINK_HI = 0.5, 1.5                     # 0.5 x beta + 0.5, clamped (lead's amendment, pre-results)
# variant -> (instrument, ratio rule)
VARIANTS: dict[str, tuple[str, str]] = {
    "none": ("", "zero"), "btc_1": ("BTC", "one"), "btc_ols": ("BTC", "ols"), "btc_shrink": ("BTC", "shrink"),
    "btc_mv": ("BTC", "mv"), "btc_cond": ("BTC", "cond"), "eth_1": ("ETH", "one"), "eth_shrink": ("ETH", "shrink"),
    "sec_1": ("SEC", "one"), "sec_shrink": ("SEC", "shrink"), "alt_1": ("ALT", "one"),
    "alt_shrink": ("ALT", "shrink"),
}


@dataclass
class Series:
    """One perp's hourly bars (ts = bar OPEN, ms) and funding prints (ts rounded to the minute)."""
    symbol: str
    bars: dict[int, tuple[float, float, float]]          # ts -> (open, close, quote volume)
    funding: list[tuple[int, float]] = field(default_factory=list)
    sector: str = "other"
    cum: dict[int, float] = field(default_factory=dict)  # ts -> cumulative quote volume through that bar

    def __post_init__(self) -> None:
        s = 0.0
        for ts in sorted(self.bars):
            s += self.bars[ts][2]
            self.cum[ts] = s

    def vol_back(self, ts: int, hours: int) -> float:
        """Quote volume of the `hours` bars ending at bar ts (0 if the coin had no bar there)."""
        if ts not in self.cum:
            return 0.0
        prior = [k for k in (ts - hours * H,) if k in self.cum]
        return self.cum[ts] - (self.cum[prior[0]] if prior else 0.0)

    def close(self, ts: int) -> float | None:
        b = self.bars.get(ts)
        return b[1] if b else None

    def px(self, ts: int, how: str) -> float | None:
        b = self.bars.get(ts)
        if not b:
            return None
        return b[1] if how == "close" else b[0] if how == "open" else (b[0] + b[1]) / 2

    def interp(self, t_ms: int) -> float | None:
        """Price at an arbitrary ms time: linear open -> close inside its hour (live trades)."""
        ts = t_ms // H * H
        b = self.bars.get(ts)
        return b[0] + (b[1] - b[0]) * (t_ms - ts) / H if b else None

    def vol24(self, ts: int) -> float:
        return sum(self.bars[k][2] for k in range(ts - 23 * H, ts + H, H) if k in self.bars)

    def print_px(self, ts: int) -> float | None:
        b = self.bars.get(ts) or self.bars.get(ts - H)
        return (b[0] if ts in self.bars else b[1]) if b else None


def load_series(db_path: str | Path = "data/hist.db", raws: set[str] | None = None) -> dict[str, Series]:
    """{ccxt symbol: Series} for every perp in hist.db (or just `raws`), read-only."""
    db = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    try:
        meta = {r: json.loads(m) for r, m in db.execute("SELECT symbol, meta FROM coins")}
        out: dict[str, Series] = {}
        bars: dict[str, dict] = {}
        for raw, ts, o, c, qv in db.execute("SELECT symbol, ts, o, c, qv FROM bars WHERE kind='perp'"):
            if raws is None or raw in raws:
                bars.setdefault(raw, {})[int(ts)] = (o, c, qv or 0.0)
        fund: dict[str, list] = {}
        for raw, ts, rate in db.execute("SELECT symbol, ts, rate FROM funding ORDER BY ts"):
            if raw in bars:
                fund.setdefault(raw, []).append((int(round(ts / 60_000)) * 60_000, float(rate)))
        for raw, b in bars.items():
            m = meta.get(raw) or {}
            sym = m.get("symbol") or f"{raw[:-4]}/USDT:USDT"
            out[sym] = Series(sym, b, fund.get(raw, []), primary_sector(m.get("tags") or []))
        return out
    finally:
        db.close()


def leg(s: Series, px_in: float, px_out: float, f_lo: int, f_hi: int, vol_ts: int,
        costs: Costs = COSTS) -> dict[str, float]:
    """P&L of a LONG of 1 unit of notional bought at px_in, sold at px_out; funding prints with f_lo < ts <= f_hi."""
    move = px_out / px_in
    slip = costs.slip(s.vol24(vol_ts))
    fees = costs.taker_bps / 1e4 * (1 + move)
    slip_c = slip * (1 + move)
    fund = 0.0
    if costs.funding:
        for ts, rate in s.funding:
            if f_lo < ts <= f_hi:
                p = s.print_px(ts)
                fund -= rate * (p if p else px_in) / px_in      # a long PAYS positive funding
    return {"move": move - 1, "gross": move - 1, "fees": fees, "slip": slip_c, "funding": fund,
            "pnl": move - 1 - fees - slip_c + fund}


def window(t: dict) -> tuple[int, str, int, int, str, int]:
    """(entry bar ts, entry price, funding-from (exclusive), exit bar ts, exit price, funding-to (inclusive))."""
    e, x = int(t["entry_ts"]), int(t["exit_ts"])
    if t.get("entry_kind", "market") == "market":
        ein = (e, "close", e + H)
    else:
        ein = (e, "mid", e)
    xout = (x, "close", x + H) if t.get("reason") in CLOSE_EXITS else (x, "mid", x)
    return (*ein, *xout)


def signal_ts(t: dict) -> int:
    return int(t["entry_ts"]) - (int(t.get("entry_i", 0)) - int(t.get("signal_i", 0))) * H


def _ret(s: Series, ts: int, h: int, log: bool) -> float | None:
    a, b = s.close(ts - h * H), s.close(ts)
    if not a or not b:
        return None
    return math.log(b / a) if log else b / a - 1


def beta(coin: Series, refs: list[Series], t_end: int, n: int = 72, h: int = 1, log: bool = True) -> float | None:
    """Cov/Var of the coin's h-hour returns on the (equal-weight) reference's, windows ending in the n hours up to
    bar t_end (h = 1, log: the `beta` feature; h = 4, simple, overlapping: the minimum-variance ratio)."""
    xs, ys = [], []
    for k in range(n - h + 1):
        ts = t_end - k * H
        y = _ret(coin, ts, h, log)
        rs = [r for r in (_ret(s, ts, h, log) for s in refs) if r is not None]
        if y is not None and rs:
            xs.append(statistics.fmean(rs))
            ys.append(y)
    if len(xs) < (n - h + 1) // 2:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx if sxx else None


def clip_ratio(b: float | None) -> float:
    return 1.0 if b is None else min(RATIO_CAP, max(0.0, b))


def shrink_ratio(b: float | None) -> float:
    return 1.0 if b is None else min(SHRINK_HI, max(SHRINK_LO, 0.5 * b + 0.5))


def tide_up(f: dict) -> bool:
    b24, b4 = f.get("btc_ret_24h"), f.get("btc_ret_4h")
    return (b24 is not None and b24 >= 0) or (b4 is not None and b4 >= 0)


class Ctx:
    """Prices + caches for the overlay: BTC, ETH, sector index, legs cached by (instrument, window)."""

    def __init__(self, series: dict[str, Series], costs: Costs = COSTS):
        self.s, self.costs = series, costs
        self.btc = next(v for k, v in series.items() if k.startswith("BTC/"))
        self.eth = next(v for k, v in series.items() if k.startswith("ETH/"))
        self.sectors: dict[str, list[Series]] = {}
        for v in series.values():
            self.sectors.setdefault(v.sector, []).append(v)
        self._legs: dict[tuple, dict | None] = {}
        self._basket: dict[tuple, list[Series]] = {}
        self._alts: dict[int, list[Series]] = {}
        self.alt_pool = [v for k, v in series.items() if not k.startswith(NOT_ALTS)]

    def alts(self, t: dict, w: tuple) -> list[Series]:
        """Equal-weight top-20 alts (7d volume at the entry bar; BTC, ETH, stables, gold excluded), not the coin."""
        e = w[0]
        if e not in self._alts:
            vol = sorted(((m.vol_back(e, ALT_LOOKBACK), m) for m in self.alt_pool if e in m.bars),
                         key=lambda x: -x[0])
            self._alts[e] = [m for _, m in vol[:ALT_N + 1]]
        return [m for m in self._alts[e] if m.symbol != t["symbol"] and w[3] in m.bars][:ALT_N]

    def leg_for(self, s: Series, w: tuple) -> dict | None:
        key = (s.symbol, w)
        if key not in self._legs:
            e, ehow, flo, x, xhow, fhi = w
            a, b = s.px(e, ehow), s.px(x, xhow)
            self._legs[key] = leg(s, a, b, flo, fhi, e, self.costs) if a and b else None
        return self._legs[key]

    def basket(self, t: dict, w: tuple) -> list[Series]:
        sym, sec = t["symbol"], (t.get("f") or {}).get("sector") or getattr(self.s.get(t["symbol"]), "sector", "other")
        key = (sym, sec, w[0], w[3])
        if key not in self._basket:
            cand = [(m.vol24(w[0]), m) for m in (self.sectors.get(sec, []) if sec is not None else [])
                    if m.symbol != sym and w[0] in m.bars and w[3] in m.bars]
            cand = sorted([c for c in cand if c[0] >= BASKET_VOL], key=lambda c: -c[0])[:BASKET_MAX]
            self._basket[key] = [m for _, m in cand] if len(cand) >= BASKET_MIN else []
        return self._basket[key]

    def instrument(self, t: dict, inst: str, w: tuple) -> tuple[list[Series], str]:
        if inst == "ETH":
            return [self.eth], "ETH"
        if inst == "ALT":
            return self.alts(t, w), "ALT"
        if inst == "SEC":
            b = self.basket(t, w)
            return (b, "SEC") if b else ([self.btc], "BTC")      # < 3 liquid members: BTC fallback
        return [self.btc], "BTC"

    def ratio(self, t: dict, rule: str, refs: list[Series], label: str) -> float:
        f = t.get("f") or {}
        coin = self.s.get(t["symbol"])
        if rule == "zero" or any(coin is r for r in refs):
            return 0.0
        if rule == "one":
            return 1.0
        if rule == "cond" and not tide_up(f):
            return 0.0
        if rule == "mv":
            return clip_ratio(beta(coin, refs, signal_ts(t), h=4, log=False) if coin else None)
        b = f.get("beta") if label == "BTC" else (beta(coin, refs, signal_ts(t)) if coin else None)
        return clip_ratio(b) if rule == "ols" else shrink_ratio(b)

    def overlay(self, t: dict, variant: str) -> dict[str, Any] | None:
        """One hedged trade row (R in the short's R); None if the hedge prices are missing."""
        inst, rule = VARIANTS[variant]
        w = window(t)
        btc = self.leg_for(self.btc, w)
        if btc is None:
            return None
        row = {"symbol": t["symbol"], "day": t["day"], "entry_ts": t["entry_ts"], "exit_ts": t["exit_ts"],
               "r0": t["r"], "risk_pct": t["risk_pct"], "btc_ret": btc["move"], "ratio": 0.0, "hedge_r": 0.0,
               "cost_r": 0.0, "inst": "", "reason": t.get("reason")}
        if rule != "zero":
            refs, label = self.instrument(t, inst, w)
            found = [self.leg_for(s, w) for s in refs]
            legs = [lg for lg in found if lg is not None]
            if len(legs) < len(found):   # a reference without bars over the trade: no hedge row
                return None
            q = self.ratio(t, rule, refs, label)
            pnl = statistics.fmean(lg["pnl"] for lg in legs)
            cost = statistics.fmean(lg["fees"] + lg["slip"] - lg["funding"] for lg in legs)
            k = q * 100 / t["risk_pct"]
            row.update(ratio=q, hedge_r=pnl * k, cost_r=-cost * k, inst=label)
        row["r"] = row["r0"] + row["hedge_r"]
        return row


def _pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))]


def _corr(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    return sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True)) / sa / sb if sa and sb else None


def _rnd(x: float | None, n: int) -> float | None:
    return None if x is None else round(x, n)


def summarize(rows: list[dict], mid: int) -> dict[str, Any]:
    """Stats of hedged rows (field r) with the unhedged r0 alongside, in the short's R."""
    if not rows:
        return {"n": 0}
    rs, r0 = [x["r"] for x in rows], [x["r0"] for x in rows]
    half = lambda a: [x["r"] for x in rows if (x["entry_ts"] < mid) == a]  # noqa: E731
    mean = lambda xs: round(statistics.fmean(xs), 3) if xs else None  # noqa: E731
    v, v0 = (statistics.variance(rs), statistics.variance(r0)) if len(rs) > 1 else (0.0, 0.0)
    sr_half = lambda a: sharpe(half(a))  # noqa: E731
    return {
        "n": len(rs), "coins": len({x["symbol"] for x in rows}), "avg_r": mean(rs),
        "win_pct": round(100 * sum(r > 0 for r in rs) / len(rs), 1),
        "sd_r": round(math.sqrt(v), 3), "sr": _rnd(sharpe(rs), 3),
        "max_dd_r": max_dd_r(rows), "p5": round(_pct(rs, 0.05), 2), "p1": round(_pct(rs, 0.01), 2),
        "corr_btc": _rnd(_corr(rs, [x["btc_ret"] for x in rows]), 2),
        "is": mean(half(True)), "oos": mean(half(False)),
        "sr_is": _rnd(sr_half(True), 3), "sr_oos": _rnd(sr_half(False), 3),
        "t_day": cluster_t(rows, "day")[0], "var_ratio": round(v / v0, 3) if v0 else None,
        "hedge_r": mean([x["hedge_r"] for x in rows]), "cost_r": mean([x["cost_r"] for x in rows]),
        "ratio": mean([x["ratio"] for x in rows]),
    }
