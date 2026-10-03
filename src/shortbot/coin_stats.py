"""Coin memory's measurements (docs/COIN_MEMORY.md): pure functions over one coin's bars and its stored state.

A coin's scale persists (vol rho 0.46, wick rho 0.40, learning #54) and its quiet/active state is partly predictable
(#56); its direction doesn't (#55), and coin-specific tells lasted about a day (#79, #80). So this measures scale
(swings, the noise band) and state (quiet / active / trend, OI, long/short, flow) and flags events; nothing here
predicts direction. `break_score` is #81's four machine checks on an open short: two or more true = a real break, one
alone = noise. The judgement (news, a catalyst) stays with the session that reads the alert.

Bars are [ts, o, h, l, c, v], the forming bar last (DataHub); flow bars add taker-buy volume."""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from typing import Any

from .guardian_watch import SQUEEZE_OI_1H_PCT
from .market.market_map import BTC_IMPULSE_15M
from .tape_math import sweep_state, taker_imbalance

M15, H1 = 900_000, 3_600_000
SWING_ATR_MULT = 2.0      # a swing = a reversal of at least 2x the bars' mean true range
TREND_EFF = 0.5           # a 15m block whose net move is >= half its path is a trend block
DAY_BLOCKS = 96           # the trailing day a block's volume is compared with
FLUSH_OI_15M_PCT = -3.0   # OI -3% in 15m with price falling: longs flushed
RETAIL_FLIP_LS = 0.05     # global account L/S up this much within 60 min while price sags (#60)
ENTRY_NEAR_SWING = 0.5    # a resting entry within half the median swing of the price
PART_OI_1H_PCT = 1.0      # participation: OI up at least this much in the hour of the move...
PART_TI = 0.05            # ...and taker buyers ahead (taker imbalance >= +0.05: buy share >= 52.5%)
BREAK_REAL = 2            # #81: two or more checks = a real break; one alone = noise
SERIES_MAX = 288          # the state's own series: 3 days at 15m (L/S, OI and price; long_short() gives one point)
BLOCKS_KEPT = 16          # 15m blocks kept in the state for break_score (4h)


def pct(a: float | None, b: float | None) -> float | None:
    """% change from a to b."""
    return (b / a - 1) * 100 if a and b is not None else None


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def blocks(bars: Sequence[Sequence[float]], ms: int = M15) -> list[list[float]]:
    """Bars aggregated on the `ms` grid: [ts, o, h, l, c, v]; the last block holds the forming bar."""
    out: list[list[float]] = []
    for b in bars:
        t = int(b[0]) // ms * ms
        if out and out[-1][0] == t:
            o = out[-1]
            o[2], o[3], o[4], o[5] = max(o[2], b[2]), min(o[3], b[3]), b[4], o[5] + b[5]
        else:
            out.append([t, b[1], b[2], b[3], b[4], b[5]])
    return out


def atr_pct(bars: Sequence[Sequence[float]]) -> float | None:
    """Mean true range over the bars, in % of each bar's close."""
    trs = [(max(b[2], a[4]) - min(b[3], a[4])) / b[4] * 100 for a, b in zip(bars, bars[1:]) if b[4]]
    return statistics.fmean(trs) if trs else None


def zigzag(closes: Sequence[float], thr_pct: float) -> list[tuple[int, float]]:
    """Swing pivots [(index, close)]: an extreme becomes a pivot once price reverses thr_pct % from it. The last item
    is the running extreme of the current leg (unconfirmed)."""
    if len(closes) < 2 or thr_pct <= 0:
        return []
    piv: list[tuple[int, float]] = []
    d, ext, hi, lo = 0, 0, 0, 0
    for i in range(1, len(closes)):
        c = closes[i]
        if d == 0:
            hi, lo = (i if c > closes[hi] else hi), (i if c < closes[lo] else lo)
            if (pct(closes[lo], c) or 0) >= thr_pct:
                piv, d, ext = [(lo, closes[lo])], 1, i
            elif (pct(closes[hi], c) or 0) <= -thr_pct:
                piv, d, ext = [(hi, closes[hi])], -1, i
        elif (c >= closes[ext]) if d == 1 else (c <= closes[ext]):
            ext = i
        elif abs(pct(closes[ext], c) or 0) >= thr_pct:
            piv.append((ext, closes[ext]))
            d, ext = -d, i
    return piv + [(ext, closes[ext])] if d else []


def swing_stats(bars: Sequence[Sequence[float]], atr_mult: float = SWING_ATR_MULT) -> dict[str, Any]:
    """Median swing size (%) and duration (min) between confirmed pivots, and the current leg: its direction, age
    and size so far. The threshold is atr_mult x the bars' mean true range."""
    atr = atr_pct(bars)
    if atr is None or len(bars) < 3:
        return {"n": 0}
    step_min = (bars[1][0] - bars[0][0]) / 60_000
    closes = [b[4] for b in bars]
    piv = zigzag(closes, atr_mult * atr)
    legs = [(abs(pct(a[1], b[1]) or 0), (b[0] - a[0]) * step_min) for a, b in zip(piv[:-1], piv[1:-1])]
    out: dict[str, Any] = {"n": len(legs), "thr_pct": _r(atr_mult * atr)}
    if legs:
        out.update(median_pct=_r(statistics.median(x[0] for x in legs)),
                   median_min=round(statistics.median(x[1] for x in legs)))
    if len(piv) >= 2:
        start = piv[-2]
        out.update(leg_dir="up" if piv[-1][1] > start[1] else "down", leg_pct=_r(pct(start[1], closes[-1])),
                   leg_age_min=round((len(closes) - 1 - start[0]) * step_min))
    return out


def _block_moves(bars_5m: Sequence[Sequence[float]]) -> list[tuple[int, float, float]]:
    """(block ts, path %, net %) of each closed 15m block: path = the sum of |5m close-to-close moves|, net = their
    sum. The forming bar's block is not closed."""
    if len(bars_5m) < 2:
        return []
    forming, prev = int(bars_5m[-1][0]) // M15 * M15, None
    out: dict[int, tuple[float, float]] = {}
    for b in bars_5m:
        t = int(b[0]) // M15 * M15
        if t == forming:
            break
        if prev:
            d = pct(prev, b[4]) or 0.0
            p, n = out.get(t, (0.0, 0.0))
            out[t] = (p + abs(d), n + d)
        prev = b[4]
    return [(t, p, n) for t, (p, n) in out.items()]


def regime_state(bars_5m: Sequence[Sequence[float]], day: int = DAY_BLOCKS) -> dict[str, Any]:
    """The last closed 15m block against the coin's own trailing day (#56): rel_vol = its path / the median path of
    the day's blocks before it, efficiency = |net| / path. efficiency >= 0.5: trend_up / trend_down; otherwise quiet
    (rel_vol < 1) or active."""
    moves = _block_moves(bars_5m)
    if len(moves) < 2:
        return {"state": None}
    t, p, net = moves[-1]
    med = statistics.median(x[1] for x in moves[-day - 1:-1])
    rel_vol, eff = (p / med if med else None), (abs(net) / p if p else 0.0)
    state = ("trend_up" if net > 0 else "trend_down") if p and eff >= TREND_EFF else \
        "quiet" if rel_vol is not None and rel_vol < 1 else "active"
    return {"state": state, "rel_vol": _r(rel_vol), "efficiency": _r(eff), "path_pct": _r(p), "block_ts": t}


def rel(x: float | None, series: Sequence[float | None]) -> dict[str, Any] | None:
    """x against the coin's own series (its last 3 days): z-score and % change from the series' first point."""
    xs = [v for v in series if v is not None]
    if x is None or len(xs) < 4:
        return None
    sd = statistics.pstdev(xs)
    return {"z": _r((x - statistics.fmean(xs)) / sd) if sd else 0.0, "chg_pct": _r(pct(xs[0], x)), "n": len(xs)}


def accepted(closed: Sequence[Sequence[float]], lookback: int = 32) -> dict[str, Any] | None:
    """Acceptance on closed 15m blocks: the last 2 closes above the prior high or round (tape_math.sweep_state), or
    below the prior `lookback`-block low. {"side", "level"} or None."""
    up = sweep_state([list(b) for b in closed], lookback=lookback)
    if up.get("state") == "accepted":
        return {"side": "up", "level": up["swept"], "level_type": up["level_type"]}
    if len(closed) >= lookback + 3:
        low = min(b[3] for b in closed[-(lookback + 3):-3])
        if all(b[4] < low for b in closed[-2:]):
            return {"side": "down", "level": low, "level_type": "prior_low"}
    return None


def noise_band(state: dict[str, Any]) -> float | None:
    """The coin's noise band (%): the larger of its fake-out p90 at levels (sweep study, shrunk) and median swing."""
    xs = [x for x in (state.get("fakeout_p90"), (state.get("swing_5m") or {}).get("median_pct")) if x]
    return max(xs) if xs else None


def retail_flip(series: Sequence[Sequence[Any]], window_ms: int = H1) -> dict[str, Any] | None:
    """#60: global account L/S up >= RETAIL_FLIP_LS within the last 60 min while price sags. Series points are
    [ts, price, oi, ls_global, ls_top], the newest last."""
    if len(series) < 2 or series[-1][3] is None:
        return None
    now = series[-1]
    base = [p for p in series[:-1] if p[0] >= now[0] - window_ms and p[3] is not None]
    if not base:
        return None
    low = min(base, key=lambda p: p[3])
    if now[3] - low[3] >= RETAIL_FLIP_LS and now[1] < low[1]:
        return {"ls_from": low[3], "ls_to": now[3], "price_pct": _r(pct(low[1], now[1]))}
    return None


def events(state: dict[str, Any], prev: dict[str, Any] | None, ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """This pass's events; `new` = not among the previous pass's. ctx: btc_15m_pct, liq (burst names for this coin),
    entries (resting limit prices for this coin)."""
    out: list[dict[str, Any]] = []
    px15, oi15, oi1h = state.get("px_15m_pct"), state.get("oi_15m_pct"), state.get("oi_1h_pct")
    if (oi15 is not None and oi15 <= FLUSH_OI_15M_PCT and (px15 or 0) < 0) or "liq_long_burst" in ctx.get("liq", ()):
        out.append({"name": "flush", "detail": f"OI {oi15}% and price {px15}% in 15m"
                    + ("; long liquidation burst" if "liq_long_burst" in ctx.get("liq", ()) else "")})
    if flip := retail_flip(state.get("series") or []):
        out.append({"name": "retail_flip", "detail": f"global L/S {flip['ls_from']} -> {flip['ls_to']} within 60 "
                    f"min, price {flip['price_pct']}%"})
    if oi1h is not None and oi1h >= SQUEEZE_OI_1H_PCT:
        out.append({"name": "oi_surge", "detail": f"OI +{oi1h}% in 1h, price {state.get('px_1h_pct')}%"})
    btc = ctx.get("btc_15m_pct")
    if btc is not None and abs(btc) >= BTC_IMPULSE_15M:
        out.append({"name": "btc_impulse", "detail": f"BTC {btc:+.2f}% in 15m"})
    if acc := state.get("accepted"):
        out.append({"name": "level_accepted", "detail": f"2 closes {'above' if acc['side'] == 'up' else 'below'} "
                    f"{acc['level_type']} {acc['level']:g}"})
    swing, px = (state.get("swing_5m") or {}).get("median_pct"), state.get("price")
    for lim in ctx.get("entries") or ():
        gap = pct(px, lim)
        if swing and gap is not None and 0 <= gap <= ENTRY_NEAR_SWING * swing:
            out.append({"name": "entry_near", "detail": f"resting entry {lim:g} is {gap:.2f}% away (median swing "
                        f"{swing}%)"})
    if prev and None not in (state.get("news"), prev.get("news")) and state["news"] > prev["news"]:
        out.append({"name": "headline", "detail": f"{state['news']} news items (text: coin_news)"})
    seen = {e["name"] for e in (prev or {}).get("events") or ()}
    return [{**e, "new": e["name"] not in seen} for e in out]


def break_score(trade: dict[str, Any], state: dict[str, Any], level: float | None = None) -> dict[str, Any]:
    """#81's machine checks on an open short. beyond_noise: the move up from its low since entry (within the kept 4h)
    exceeds the noise band; accepted: 2 closed 15m blocks beyond a level (`level`: the current read's invalidation,
    else acceptance above the prior high); participation: price up with OI up and taker buyers ahead over the hour;
    source: BTC up >= BTC_IMPULSE_15M in 15m (the market is lifting it). score >= 2: a real break; 1: noise."""
    b15, price = state.get("blocks15") or [], state.get("price")
    opened = trade.get("opened_ms") or 0
    lows = [b[3] for b in b15 if b[0] + M15 > opened] or [b[3] for b in b15]
    move = pct(min(lows), price) if lows and price else None
    band = noise_band(state)
    closed = b15[:-1]
    acc = state.get("accepted") or {}
    checks = {
        "beyond_noise": bool(move is not None and band and move > band),
        "accepted": bool(acc.get("side") == "up" or (level and len(closed) >= 2
                                                       and all(b[4] > level for b in closed[-2:]))),
        "participation": bool((state.get("px_1h_pct") or 0) > 0 and (state.get("oi_1h_pct") or 0) >= PART_OI_1H_PCT
                              and (state.get("ti_1h") or 0) >= PART_TI),
        "source": (state.get("btc_15m_pct") or 0) >= BTC_IMPULSE_15M}
    score = sum(checks.values())
    return {"score": score, "of": 4, "verdict": "real break" if score >= BREAK_REAL else "noise" if score else "none",
            "checks": checks, "move_pct": _r(move), "band_pct": _r(band),
            "level": float(f"{lv:.6g}") if (lv := level or acc.get("level")) else None,
            "oi_1h_pct": state.get("oi_1h_pct"), "ti_1h": state.get("ti_1h"), "btc_15m_pct": state.get("btc_15m_pct")}


def measure(bars5: Sequence[Sequence[float]], bars1h: Sequence[Sequence[float]], oi: Sequence[dict[str, Any]],
            ls: dict[str, Any], flow: Sequence[Sequence[float]], prev: dict[str, Any] | None, now_ms: int,
            funding_hist: Sequence[float] = (), **extra: Any) -> dict[str, Any]:
    """One coin's state from one pass: 5m bars (~3 days), 1h bars (~20 days), 5m OI history (>= 1h), the latest
    long/short ratios, 5m flow bars (>= 1h) and the previous state (whose series it extends: one point per 15m slot,
    3 days kept). funding_hist (rates, oldest first) gives rel.funding; `extra` is stored as given (btc_15m_pct,
    fakeout_p90, funding, news, macro)."""
    if len(bars5) < 14:
        raise ValueError(f"only {len(bars5)} 5m bars")
    price, b15 = bars5[-1][4], blocks(bars5)
    ois = [float(o["oi"]) for o in oi if o.get("oi") is not None]
    last_oi = ois[-1] if ois else None
    ls_g, ls_t = ls.get("global_account_ls"), ls.get("top_trader_position_ls")
    slot = now_ms // M15 * M15
    hist = [p for p in (prev or {}).get("series") or () if slot - SERIES_MAX * M15 < p[0] < slot]
    ti = taker_imbalance([list(b) for b in flow[-12:]]) if flow else None
    return {
        "ts_ms": now_ms, "price": price,
        "px_15m_pct": _r(pct(bars5[-4][4], price)), "px_1h_pct": _r(pct(bars5[-13][4], price)),
        "oi": last_oi, "oi_15m_pct": _r(pct(ois[-4], ois[-1])) if len(ois) >= 4 else None,
        "oi_1h_pct": _r(pct(ois[max(0, len(ois) - 13)], ois[-1])) if len(ois) >= 2 else None,
        "ls": {"global": ls_g, "top": ls_t}, "ti_1h": _r(ti, 3),
        "swing_5m": swing_stats(bars5), "swing_1h": swing_stats(bars1h), "regime": regime_state(bars5),
        "accepted": accepted(b15[:-1]),
        "rel": {"price": rel(price, [p[1] for p in hist]), "oi": rel(last_oi, [p[2] for p in hist]),
                "ls_global": rel(ls_g, [p[3] for p in hist]), "ls_top": rel(ls_t, [p[4] for p in hist]),
                "funding": rel(extra.get("funding"), list(funding_hist))},
        "blocks15": [[int(b[0]), *(float(f"{x:.6g}") for x in b[1:5])] for b in b15[-BLOCKS_KEPT:]],
        "series": [*hist, [slot, price, last_oi, ls_g, ls_t]], **extra}
