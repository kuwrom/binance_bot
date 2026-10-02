"""The long sleeve, round 2 (task #32): hourly versions of the two daily long edges in backtest/long_studies.py (the
20-day breakout over 5 days; residual-momentum leaders). Research only. Same simulator, matched placebos and report
as every strategy.

Grids fixed before any result was seen (6 trials):
- breakout_continuation (lib.breakout_20d_overlay, hourly): the close exceeds the highest high of the previous `look`
  hours on a bar with >= 3x the median hourly quote volume of the previous 24h, with >= $20M 24h quote volume.
  Enter at that close; stop under the bar's low - 0.5 ATR; targets 1.5R / 3R; time stop `time` hours; one entry per
  coin per 24h.
- rs_leader_pullback (lib.beta_hedged_rs_pair, hourly): at each 00:00 UTC bar, rank the coins with >= $20M 24h quote
  volume by their 168h return residual to BTC (beta from 72h). In the next 24h a top-`top` coin that dips to its EMA20
  (a low at or under it) and then closes green back above it is bought at that close; stop under the 6-bar low - 0.5
  ATR; targets 1.5R / 3R; 72h time stop; one entry per coin per 24h.
Breakeven at 1R (long_plan's default)."""

from __future__ import annotations

import statistics
from collections.abc import Iterable

from .features import Coin, Market
from .sim import Plan
from .strategies import Trial, atr_abs, grid, tradable
from .strategies_long import long_plan, low, vol24

H = 3_600_000
LIQUID_24H = 20e6
VOL_MULT = 3.0


def breakout_continuation(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    r, look, busy = coin.rows, p["look"], -1
    for i in range(max(look, 24), len(r)):
        if i <= busy or r[i]["c"] <= coin.high(i - look, i) or not tradable(coin, i):
            continue
        med = statistics.median(x["qv"] for x in r[i - 24:i])
        if not med or r[i]["qv"] < VOL_MULT * med or vol24(coin, i) < LIQUID_24H:
            continue
        pl = long_plan(coin, i, r[i]["l"] - 0.5 * atr_abs(coin, i), p["time"])
        if pl:
            yield i, pl
            busy = i + 24


def leaders(m: Market, top: int) -> dict[int, set[str]]:
    """{00:00 UTC bar ts: the top coins by 168h return residual to BTC}, cached on the market."""
    key = ("rs_leaders", top)
    if key in m.cache:
        return m.cache[key]
    out: dict[int, set[str]] = {}
    btc = m.btc
    for k, row in enumerate(btc.rows if btc else []):
        ts = row["ts"]
        if ts % (24 * H) or k < 168:
            continue
        b_ret = row["c"] / btc.rows[k - 168]["c"] - 1
        resid = {}
        for c in m.coins.values():
            i = c.pos.get(ts)
            if c is btc or i is None or i < 168 or vol24(c, i) < LIQUID_24H:
                continue
            beta, _ = m.beta_corr(c, i, 72)
            if beta is not None:
                resid[c.symbol] = c.rows[i]["c"] / c.rows[i - 168]["c"] - 1 - beta * b_ret
        out[ts] = set(sorted(resid, key=lambda s: -resid[s])[:top])
    m.cache[key] = out
    return out


def rs_leader_pullback(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    lead, r, e, busy = leaders(m, p["top"]), coin.rows, coin.ema20, -1
    for i in range(24, len(r)):
        day = r[i]["ts"] // (24 * H) * (24 * H)
        if i <= busy or coin.symbol not in lead.get(day, ()) or r[i]["ts"] == day:
            continue
        if r[i - 1]["l"] <= e[i - 1] and r[i]["c"] > e[i] and r[i]["c"] > r[i]["o"] and tradable(coin, i):
            pl = long_plan(coin, i, low(coin, i - 5, i + 1) - 0.5 * atr_abs(coin, i), 72)
            if pl:
                yield i, pl
                busy = i + 24


def trials_long2() -> list[Trial]:
    return (grid("breakout_continuation", breakout_continuation, {"look": [72, 168], "time": [24, 72]})
            + grid("rs_leader_pullback", rs_leader_pullback, {"top": [5, 10]}))


LIB_IDS = {"breakout_continuation": "lib.breakout_20d_overlay", "rs_leader_pullback": "lib.beta_hedged_rs_pair"}
