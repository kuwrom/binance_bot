"""Pure bar math for the live-pipeline replay (task #29): REST-shaped klines, the rolling 24h ticker and the
predicted funding rate, all AS OF a moment t (epoch ms), from 1-minute bars closed by then. No I/O.

- Binance REST returns the still-forming bar last (exchange.py fetch_ohlcv / flow_klines) and the signals compute on
  it, so a replay at 14:36 builds the 14:00 bar from the closed minutes 14:00-14:35 (a flat bar at the last close
  while none has closed yet).
- The 24h ticker is rolling, with a minute-aligned openTime = floor_minute(now) - 24h (checked on
  /fapi/v1/ticker/24hr 2026-10-02: closeTime - openTime = 24 h 47 s). The replay uses the closed minutes of that
  window, so `last` is at most one minute old.
- Bars are tuples (ts, o, h, l, c, v, qv, n, tbv, tbq): base volume, quote volume, trades, taker-buy base and quote
  volume, as in the data.binance.vision kline files.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence

MIN, HOUR, DAY = 60_000, 3_600_000, 86_400_000
TS, O, H, L, C, V, QV, N, TBV, TBQ = range(10)   # noqa: E741 - a kline's columns, O = open
_TF = {"1m": MIN, "3m": 3 * MIN, "5m": 5 * MIN, "15m": 15 * MIN, "30m": 30 * MIN, "1h": HOUR, "2h": 2 * HOUR,
       "4h": 4 * HOUR, "6h": 6 * HOUR, "8h": 8 * HOUR, "12h": 12 * HOUR, "1d": DAY}
Bar = tuple


def tf_ms(tf: str) -> int:
    if tf not in _TF:
        raise ValueError(f"unsupported timeframe {tf!r}")
    return _TF[tf]


def between(bars: Sequence[Bar], a: int, b: int) -> list[Bar]:
    """Bars with a <= ts < b (bars sorted by ts)."""
    keys = [x[TS] for x in bars]
    return list(bars[bisect_left(keys, a):bisect_left(keys, b)])


def merge(group: Sequence[Bar], ts: int) -> Bar:
    return (ts, group[0][O], max(x[H] for x in group), min(x[L] for x in group), group[-1][C],
            sum(x[V] for x in group), sum(x[QV] for x in group), sum(x[N] for x in group),
            sum(x[TBV] for x in group), sum(x[TBQ] for x in group))


def group(bars: Sequence[Bar], step: int) -> list[Bar]:
    """Merge consecutive bars into `step`-long bars aligned to the epoch (UTC midnight for days)."""
    out: list[Bar] = []
    cur: list[Bar] = []
    for b in bars:
        if cur and b[TS] // step != cur[0][TS] // step:
            out.append(merge(cur, cur[0][TS] // step * step))
            cur = []
        cur.append(b)
    if cur:
        out.append(merge(cur, cur[0][TS] // step * step))
    return out


def _sources(fine: Sequence[Bar], t_ms: int, a: int, coarse: Sequence[Bar], coarse_step: int | None,
             fine_step: int = MIN) -> list[Bar]:
    """Closed source bars from `a` up to t: coarse bars (each coarse_step long, fully closed) first, then the fine
    bars (1m, or 5m for stage 2's auxiliary series) after the coarse coverage."""
    cs = [b for b in between(coarse, a, t_ms) if b[TS] + (coarse_step or 0) <= t_ms] if coarse_step else []
    cov = cs[-1][TS] + coarse_step if cs else a
    fs = [b for b in between(fine, max(a, cov), t_ms) if b[TS] + fine_step <= t_ms]
    return cs + fs


def klines(fine: Sequence[Bar], tf: str, t_ms: int, limit: int, coarse: Sequence[Bar] = (),
           coarse_step: int | None = None, fine_step: int = MIN) -> list[Bar]:
    """The last `limit` bars of `tf` as of t_ms, the forming one last, like the REST klines endpoint.
    `coarse` (optional) holds closed bars of `coarse_step` (a divisor of tf, e.g. hourly for 1h/4h, daily for 1d)
    that stand in for the 1m bars over closed history."""
    step = tf_ms(tf)
    if step < fine_step:
        raise ValueError(f"{tf} bars need fine bars of {step // MIN}m or less, have {fine_step // MIN}m")
    cur = t_ms // step * step
    first = cur - (max(limit, 1) - 1) * step
    src = _sources(fine, t_ms, first, coarse, coarse_step, fine_step)
    out = group(src, step)
    if not out or out[-1][TS] != cur:
        prior = src[-1][C] if src else _last_close(fine, coarse, coarse_step, first, t_ms, fine_step)
        if prior is not None:
            out.append((cur, prior, prior, prior, prior, 0.0, 0.0, 0, 0.0, 0.0))
    return out[-limit:]


def _last_close(fine: Sequence[Bar], coarse: Sequence[Bar], coarse_step: int | None, before: int,
                t_ms: int, fine_step: int = MIN) -> float | None:
    """Close of the latest bar closed before `before` (a coin with no trade inside the requested window)."""
    f = [b for b in between(fine, 0, before) if b[TS] + fine_step <= t_ms]
    if f:
        return f[-1][C]
    c = [b for b in between(coarse, 0, before) if b[TS] + (coarse_step or 0) <= t_ms] if coarse_step else []
    return c[-1][C] if c else None


def ticker(fine: Sequence[Bar], t_ms: int, coarse: Sequence[Bar] = (), coarse_step: int = HOUR) -> dict | None:
    """ccxt-shaped rolling 24h ticker as of t_ms from the closed minutes of [floor_min(t) - 24h, floor_min(t)).
    `coarse`: closed hourly bars used for the whole hours inside the window. None without data."""
    end = t_ms // MIN * MIN
    start = end - DAY
    if coarse:
        mid = between(coarse, -(-start // coarse_step) * coarse_step, end)
        mid = [b for b in mid if b[TS] + coarse_step <= end]
        if mid:
            lo, hi = mid[0][TS], mid[-1][TS] + coarse_step
            w = between(fine, start, lo) + mid + between(fine, hi, end)
        else:
            w = between(fine, start, end)
    else:
        w = between(fine, start, end)
    if not w:
        return None
    o, last = w[0][O], w[-1][C]
    return {"timestamp": t_ms, "open": o, "last": last, "close": last, "high": max(b[H] for b in w),
            "low": min(b[L] for b in w), "change": last - o, "percentage": (last - o) / o * 100 if o else None,
            "baseVolume": sum(b[V] for b in w), "quoteVolume": sum(b[QV] for b in w)}


def predicted_funding(prem: Sequence[Bar], t_ms: int, last_settle_ms: int, interval_h: int,
                      interest_8h: float = 0.0001, clamp: float = 0.0005, cap: float = 0.02,
                      fine_step: int = MIN) -> float | None:
    """Binance's estimate of the next funding rate (binance.com/en/support/faq/360033525031, 2026-10-02):
    F = [P + clamp(I - P, -0.05%, +0.05%)] / (8 / N), with N the interval in hours, I = 0.01% and P the average
    premium index since the last settlement: time-weighted (1..n) for N > 1, equally weighted for N = 1; capped at
    +-2% for most contracts. Approximate here: one premium sample per closed minute (Binance samples every 5 s)."""
    xs = [b[C] for b in between(prem, last_settle_ms, t_ms) if b[TS] + fine_step <= t_ms]
    if not xs:
        return None
    if interval_h == 1:
        p = sum(xs) / len(xs)
    else:
        p = sum(i * x for i, x in enumerate(xs, 1)) / (len(xs) * (len(xs) + 1) / 2)
    f = (p + min(max(interest_8h - p, -clamp), clamp)) / (8 / interval_h)
    return min(max(f, -cap), cap)


def funding_interval_h(settles: Sequence[int], default: int = 8) -> int:
    """Funding interval from the last two settlement times (Binance moves coins between 8 h, 4 h and 1 h)."""
    if len(settles) < 2:
        return default
    h = round((settles[-1] - settles[-2]) / HOUR)
    return h if h in (1, 2, 4, 8) else default


def taker_ratios(fine: Sequence[Bar], t_ms: int, limit: int) -> list[float]:
    """Taker buy/sell volume ratio per closed hour (the takerlongshortRatio statistic), oldest first."""
    cur = t_ms // HOUR * HOUR
    hours = group([b for b in between(fine, cur - limit * HOUR, cur)], HOUR)
    return [b[TBV] / (b[V] - b[TBV]) for b in hours if b[V] - b[TBV] > 0]
