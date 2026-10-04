"""Cross-coin strategies and controls for the backtest.

laggard   docs/LADDER.md A: a sector leader (highest 24h volume) shows ACCEPTANCE below its prior 12h low (2 closes),
          leader 4h return < -2%; short members that fell less than half as much (and correlate >= corr with the
          leader over 72h) at t0 + 1 bar. Target = the catch-up gap, stop = laggard 12h high + 0.5 ATR, 12h time stop.
delisting only when the data carries delist_ms: short `days_before` the delivery (assumes the announcement came
          earlier, which is Binance's usual ~1 week notice), exit 2h before settlement.
placebo   the donor trades' exits in portable units (stop in ATR, targets in R, same time stop / breakeven / trail)
          on (a) random hours of the SAME coin and (b) the SAME hour on random other coins: the edge is strategy minus
          placebo, not strategy minus zero.
random    random hours of every tradable coin, 2 ATR stop, half at 1R / rest at 2R, 24h: the market-drift baseline."""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Iterable
from typing import Any

from . import strategies as _S
from .features import Coin, Market
from .sim import H, Plan
from .strategies import WARMUP, Trial, atr_abs, plan, tradable


def floor() -> float:
    """strategies.MIN_VOL24 as set now (run.py --min-vol24), not as imported."""
    return _S.MIN_VOL24

def _cum_qv(m: Market, coin: Coin) -> list[float]:
    key = ("cumqv", coin.symbol)
    if key not in m.cache:
        s, out = 0.0, []
        for r in coin.rows:
            s += r["qv"]
            out.append(s)
        m.cache[key] = out
    return m.cache[key]


def vol24_at(m: Market, coin: Coin, i: int) -> float:
    cq = _cum_qv(m, coin)
    return cq[i] - (cq[i - 24] if i >= 24 else 0.0)


def corr_ts(a: Coin, b: Coin, ts: int, n: int = 72) -> float | None:
    """Correlation of 1h log returns of two coins over the n hours ending at bar `ts`."""
    xs, ys = [], []
    for k in range(n):
        t1, t0 = ts - k * H, ts - (k + 1) * H
        a0, a1, b0, b1 = a.close_at(t0), a.close_at(t1), b.close_at(t0), b.close_at(t1)
        if a0 and a1 and b0 and b1:
            xs.append(math.log(a1 / a0))
            ys.append(math.log(b1 / b0))
    if len(xs) < n // 2:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx, syy = sum((x - mx) ** 2 for x in xs), sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx and syy else None


def laggard(m: Market, p: dict) -> Iterable[tuple[Coin, int, Plan]]:
    clock = [r["ts"] for r in m.btc.rows] if m.btc else []
    for sector, members in m.sectors.items():
        if sector == "other" or len(members) < 4:
            continue
        last_ev = -10 ** 15
        for ts in clock[WARMUP:]:
            live = [(c, c.pos[ts]) for c in members if ts in c.pos and c.pos[ts] >= WARMUP]
            live = [(c, i) for c, i in live if vol24_at(m, c, i) >= floor()]
            if len(live) < 4 or ts - last_ev < 4 * H:
                continue
            lc, li = max(live, key=lambda x: vol24_at(m, x[0], x[1]))
            lr = lc.rows

            def below(k: int) -> bool:
                return lr[k]["c"] < min(x["l"] for x in lr[k - 13:k - 1]) and lr[k - 1]["c"] < min(
                    x["l"] for x in lr[k - 13:k - 1])
            l4 = lc.ret(li, 4)
            if not (below(li) and not below(li - 1) and l4 is not None and l4 < -2):
                continue
            last_ev = ts
            for c, i in live:
                if c is lc or i + 1 >= len(c.rows):
                    continue
                r4 = c.ret(i, 4)
                if r4 is None or r4 <= 0.5 * l4:
                    continue
                cr = corr_ts(c, lc, ts)
                if cr is None or cr < p["corr"]:
                    continue
                j = i + 1                                  # t0 + 1 bar
                if not tradable(c, j):
                    continue
                e = c.rows[j]["c"]
                stop = c.high(j - 11, j + 1) + 0.5 * atr_abs(c, j)
                gap = max(0.5, r4 - l4)
                pl = plan(c, j, stop, 12, targets=((e * (1 - gap / 100), 1.0),), be_r=None)
                if pl:
                    yield c, j, pl


def delisting(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    if not coin.delist_ms:
        return
    t_entry = (coin.delist_ms - p["days_before"] * 86_400_000) // H * H     # a bar CLOSE
    i = coin.pos.get(t_entry - H)
    if i is None or i < WARMUP:
        return
    hold = int((coin.delist_ms - 2 * H - t_entry) // H)
    if hold < 2:
        return
    stop = coin.high(i - 23, i + 1) + 2 * atr_abs(coin, i)
    pl = plan(coin, i, stop, hold, be_r=None)
    if pl:
        yield i, pl


# ---------------------------------------------------------------- controls
def placebo_signals(m: Market, donors: list[dict[str, Any]], per: int = 3, seed: int = 11, mode: str = "coin"
                    ) -> list[tuple[Coin, int, Plan]]:
    """`per` control entries per donor trade with the donor's exits in ATR / R units, market entry.
    mode "coin": the SAME coin at random hours (is the trigger better than being in this coin at all?).
    mode "time": the SAME hour on random OTHER tradable coins (is it better than shorting anything right then?
    this one cancels the market regime, e.g. a falling second half)."""
    rng = random.Random(seed)
    pool = list(m.coins.values())
    out = []
    for d in donors:
        coin, meta = m.coins.get(d["symbol"]), d.get("meta") or {}
        if not coin or not meta.get("stop_atr"):
            continue
        for _ in range(per):
            pick = None
            for _try in range(30):
                if mode == "coin":
                    c, hi = coin, len(coin.rows) - d["time_stop"] - 2
                    if hi <= WARMUP:
                        break
                    i = rng.randrange(WARMUP, hi)
                else:
                    c = rng.choice(pool)
                    i = c.pos.get(coin.rows[d["signal_i"]]["ts"])
                    if c is coin or i is None or i + d["time_stop"] + 2 >= len(c.rows):
                        continue
                if tradable(c, i):
                    pick = (c, i)
                    break
            if not pick:
                continue
            c, i = pick
            px = c.rows[i]["c"]
            stop = px + meta["stop_atr"] * atr_abs(c, i)
            risk = stop - px
            tg = tuple((px - r * risk, f) for r, f in meta["targets_r"])
            out.append((c, i, Plan(stop=stop, targets=tg, time_stop=d["time_stop"], be_r=d.get("be_r"),
                                   trail_act_r=d.get("trail_act_r"), trail_dist_r=d.get("trail_dist_r", 1.0),
                                   meta=dict(meta, donor=(d["symbol"], d["signal_i"])))))
    return out


def random_entries(m: Market, p: dict) -> Iterable[tuple[Coin, int, Plan]]:
    rng = random.Random(p.get("seed", 5))
    coins = [c for c in m.coins.values() if len(c.rows) > WARMUP + 30]
    for _ in range(p["n"]):
        coin = rng.choice(coins)
        i = rng.randrange(WARMUP, len(coin.rows) - 26)
        if tradable(coin, i):
            pl = plan(coin, i, coin.rows[i]["c"] + 2 * atr_abs(coin, i), 24)
            if pl:
                yield coin, i, pl


RANDOM = Trial("random", {"n": 3000}, random_entries, cross=True, control=True,
               notes="market-drift baseline: random hours, 2 ATR stop, 1R/2R targets, 24h")
