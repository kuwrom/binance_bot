"""Round-4 placebo corrections (docs/failures/ROUND4.md). `--placebo=matched` replaces the legacy same-hour draw.

Legacy (strategies_more.placebo_signals, mode "time"): `per` random OTHER tradable coins at the donor's signal hour.
The review found two flaws:
1. Volatility mismatch: signal coins are 2-3x as volatile as a random coin that hour, so the same fees and slippage
   are FEWER R for the donor, and the net paired edge is inflated by the cost gap.
2. Parity: the placebo lacks the donor's funding-budget exit, and a resting-limit donor's placebo enters at the
   SIGNAL bar instead of the fill bar.

Matched placebo (this module), per donor trade:
- anchor bar = the donor's fill bar for non-market entries, else its signal bar;
- cross-section at that hour = every tradable coin with enough bars after the anchor for the donor's time stop;
- the cell = the donor's cross-sectional ATR% DECILE x its 24h-volume bucket (VOL_EDGES in $M, the report's
  liquidity edges); `per` coins are drawn from the cell without replacement, and missing draws are filled with the
  NEAREST coins by |ATR percentile gap| + |volume percentile gap| (meta match = "cell" | "nearest");
- the plan carries the donor's exits in ATR / R units, breakeven, trail AND funding budget.
The same-coin placebo keeps its random-hours draw; it only gains the parity fixes (budget exit)."""

from __future__ import annotations

import bisect
import random
from typing import Any

from .features import Coin, Market
from .sim import Plan
from .strategies import WARMUP, atr_abs, tradable
from .strategies_more import vol24_at

VOL_EDGES = (2e6, 5e6, 20e6, 100e6)          # report.buckets liquidity edges ($ 24h quote volume)
N_DECILES = 10


def anchor_i(d: dict[str, Any]) -> int:
    """The bar the placebo enters at: the fill bar for limit / sell-stop entries, else the signal bar."""
    return d["entry_i"] if d.get("entry_kind", "market") != "market" and d.get("entry_i") is not None else d["signal_i"]


def vol_bucket(v: float) -> int:
    return bisect.bisect_right(VOL_EDGES, v)


def _pctile(s: list[float], x: float) -> float:
    """Mid-rank percentile of x in the SORTED list s (share below, plus half the ties), in [0, 1]."""
    lo, hi = bisect.bisect_left(s, x), bisect.bisect_right(s, x)
    return (lo + (hi - lo) / 2) / len(s) if s else 0.5


def _decile(p: float) -> int:
    return min(N_DECILES - 1, int(p * N_DECILES))


class XSec:
    """The tradable cross-section at one hour: rows (coin, i, ATR pctile, volume pctile), sorted ATR% / volume
    lists for the donor's own percentile, and the rows grouped by (ATR decile, volume bucket) cell."""

    def __init__(self, rows: list[tuple[Coin, int, float, float]]):
        self.atrs, self.vols = sorted(x[2] for x in rows), sorted(x[3] for x in rows)
        self.rows = [(c, i, _pctile(self.atrs, a), _pctile(self.vols, v)) for c, i, a, v in rows]
        self.cells: dict[tuple[int, int], list[tuple[Coin, int]]] = {}
        for (c, i, pa, _), (_, _, _, v) in zip(self.rows, rows, strict=True):
            self.cells.setdefault((_decile(pa), vol_bucket(v)), []).append((c, i))


def cross_section(m: Market, ts: int) -> XSec:
    """Every tradable coin with a bar at `ts` (ATR%, 24h $ volume); cached per hour."""
    key = ("xsec", ts)
    if key not in m.cache:
        rows = []
        for c in m.coins.values():
            i = c.pos.get(ts)
            if i is not None and tradable(c, i):
                rows.append((c, i, c.atr[i], vol24_at(m, c, i)))
        m.cache[key] = XSec(rows)
    return m.cache[key]


def match_coins(m: Market, coin: Coin, i: int, need_bars: int, per: int, rng: random.Random
                ) -> list[tuple[Coin, int, str]]:
    """`per` other coins at the hour of coin's bar i: same ATR decile and volume bucket first, nearest after."""
    xs = cross_section(m, coin.rows[i]["ts"])
    if not xs.rows:
        return []
    v0 = vol24_at(m, coin, i)
    pa0, pv0 = _pctile(xs.atrs, coin.atr[i]), _pctile(xs.vols, v0)
    ok = lambda c, j: c is not coin and j + need_bars < len(c.rows)  # noqa: E731
    cell = [(c, j) for c, j in xs.cells.get((_decile(pa0), vol_bucket(v0)), []) if ok(c, j)]
    pick = [(c, j, "cell") for c, j in rng.sample(cell, min(per, len(cell)))]
    if len(pick) < per:
        taken = {id(c) for c, _ in cell}
        rest = sorted((abs(pa - pa0) + abs(pv - pv0), c.symbol, c, j) for c, j, pa, pv in xs.rows
                      if ok(c, j) and id(c) not in taken)
        pick += [(c, j, "nearest") for _, _, c, j in rest[:per - len(pick)]]
    return pick


def placebo_plan(c: Coin, i: int, d: dict[str, Any], meta: dict[str, Any], **extra: Any) -> Plan:
    """The donor's exits on coin c at bar i (market entry at the close): stop in ATR, targets in R, same time stop,
    breakeven, trail and funding budget. A long donor (meta side "long", strategies_long) gets the mirrored plan."""
    px = c.rows[i]["c"]
    s = -1 if meta.get("side") == "long" else 1
    stop = px + s * meta["stop_atr"] * atr_abs(c, i)
    risk = s * (stop - px)
    tg = tuple((px - s * r * risk, f) for r, f in meta["targets_r"])
    return Plan(stop=stop, targets=tg, time_stop=d["time_stop"], be_r=d.get("be_r"), side=meta.get("side", "short"),
                trail_act_r=d.get("trail_act_r"), trail_dist_r=d.get("trail_dist_r") or 1.0,
                fund_budget_r=d.get("fund_budget_r"), fund_follow_r=d.get("fund_follow_r") or 0.5,
                meta=dict(meta, donor=(d["symbol"], d["signal_i"]), **extra))


def matched_signals(m: Market, donors: list[dict[str, Any]], per: int = 3, seed: int = 11, mode: str = "time"
                    ) -> list[tuple[Coin, int, Plan]]:
    """Corrected placebos. mode "time": the volatility- and liquidity-matched same-hour draw at the anchor bar.
    mode "coin": the legacy same-coin random-hours draw, with the parity fixes (budget exit)."""
    rng = random.Random(seed)
    out = []
    for d in donors:
        coin, meta = m.coins.get(d["symbol"]), d.get("meta") or {}
        if not coin or not meta.get("stop_atr"):
            continue
        need = d["time_stop"] + 2
        if mode == "time":
            a = anchor_i(d)
            for c, i, how in match_coins(m, coin, a, need, per, rng):
                out.append((c, i, placebo_plan(c, i, d, meta, match=how, anchor=a)))
            continue
        hi = len(coin.rows) - need
        for _ in range(per):
            if hi <= WARMUP:
                break
            for _try in range(30):
                i = rng.randrange(WARMUP, hi)
                if tradable(coin, i):
                    out.append((coin, i, placebo_plan(coin, i, d, meta)))
                    break
    return out
