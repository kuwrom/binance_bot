"""Diagnostics for the hedge overlay (docs/HEDGE.md): realized rho^2 per trade, idiosyncratic share of stop-outs,
splits by BTC's move over the trade, the squeeze tail, and the pre-registered decision rules."""

from __future__ import annotations

import math
import statistics
from typing import Any

from .hedge import Ctx, Series, shrink_ratio, window
from .sim import H

REGIME_PCT = 1.0          # BTC over the trade: up >= +1%, down <= -1%, else chop (ex-post: descriptive only)
STOP_REASONS = ("stop", "stop_gap")


def _lr(s: Series, ts: int) -> float | None:
    a, b = s.close(ts - H), s.close(ts)
    return math.log(b / a) if a and b else None


def rho2(ctx: Ctx, t: dict, refs: list[Series]) -> float | None:
    """corr^2 of the coin's and the hedge's 1h log returns over the bars the trade was open (>= 4 hours)."""
    coin = ctx.s.get(t["symbol"])
    if coin is None or not refs:
        return None
    xs, ys = [], []
    for ts in range(int(t["entry_ts"]) + H, int(t["exit_ts"]) + H, H):
        y = _lr(coin, ts)
        rs = [r for r in (_lr(m, ts) for m in refs) if r is not None]
        if y is not None and rs:
            xs.append(statistics.fmean(rs))
            ys.append(y)
    if len(xs) < 4:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx, syy = sum((x - mx) ** 2 for x in xs), sum((y - my) ** 2 for y in ys)
    return (sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) ** 2 / sxx / syy) if sxx and syy else None


def idio_stop(ctx: Ctx, t: dict) -> bool | None:
    """A stop-out whose residual move (coin - shrunk beta x BTC) exceeded 2 sigma of the prior-72h residual,
    scaled to the hours held: the coin was squeezed on its own, not with the market."""
    if t.get("reason") not in STOP_REASONS:
        return None
    coin, btc = ctx.s.get(t["symbol"]), ctx.btc
    lg = ctx.leg_for(btc, window(t))
    if coin is None or lg is None:
        return None
    t_sig = int(t["entry_ts"])
    b = shrink_ratio((t.get("f") or {}).get("beta"))
    res = []
    for k in range(72):
        y, x = _lr(coin, t_sig - k * H), _lr(btc, t_sig - k * H)
        if y is not None and x is not None:
            res.append(y - b * x)
    if len(res) < 24:
        return None
    sig = statistics.stdev(res) * math.sqrt(max(1, int(t.get("bars") or 1)))
    resid = math.log(1 + t["risk_pct"] / 100) - b * math.log(1 + lg["move"])
    return resid > 2 * sig


def regime(btc_ret: float) -> str:
    return "up" if btc_ret * 100 >= REGIME_PCT else "down" if btc_ret * 100 <= -REGIME_PCT else "chop"


def _avg(xs: list[float]) -> float | None:
    return round(statistics.fmean(xs), 3) if xs else None


def splits(res: dict[str, dict], variants=("none", "btc_1", "btc_shrink", "alt_1", "alt_shrink")) -> dict:
    """avg R (n) by BTC's move over the trade, and the squeeze tail (short lost >= 1R) by BTC's direction."""
    out: dict[str, Any] = {"regime": {}, "tail": {}}
    for v in variants:
        if v not in res:
            continue
        rows = res[v]["rows"]
        for g in ("up", "chop", "down"):
            xs = [r["r"] for r in rows if regime(r["btc_ret"]) == g]
            out["regime"].setdefault(g, {})[v] = (_avg(xs), len(xs))
        for g, sel in (("squeeze+BTC down", lambda r: r["btc_ret"] < 0), ("squeeze+BTC up", lambda r: r["btc_ret"] >= 0)):
            xs = [r["r"] for r in rows if r["r0"] <= -1 and sel(r)]
            out["tail"].setdefault(g, {})[v] = (_avg(xs), len(xs))
    return out


def trade_diag(ctx: Ctx, trades: list[dict]) -> dict[str, Any]:
    """Mean realized rho^2 vs BTC / ETH / top-20 alts, and the share of stop-outs that were idiosyncratic."""
    r2: dict[str, list[float]] = {"BTC": [], "ETH": [], "ALT": []}
    idio = []
    for t in trades:
        w = window(t)
        for lab, refs in (("BTC", [ctx.btc]), ("ETH", [ctx.eth]), ("ALT", ctx.alts(t, w))):
            v = rho2(ctx, t, refs)
            if v is not None:
                r2[lab].append(v)
        i = idio_stop(ctx, t)
        if i is not None:
            idio.append(i)
    return {"rho2": {k: (_avg(v), len(v)) for k, v in r2.items()},
            "stops": len(idio), "idio_stop_pct": round(100 * sum(idio) / len(idio), 1) if idio else None}


def verdicts(stats: dict[str, dict]) -> dict[str, str]:
    """Pre-registered rules. risk: Sharpe per trade better in IS and OOS and var ratio < 0.9. adopt (lead): hedged
    avg R > 0 after costs and day-clustered t not lower than unhedged."""
    base = stats.get("none") or {}
    out = {}
    for v, s in stats.items():
        if v == "none" or not s.get("n"):
            continue
        risk = (all(s.get(k) is not None and base.get(k) is not None and s[k] > base[k] for k in ("sr_is", "sr_oos"))
                and (s.get("var_ratio") or 9) < 0.9)
        adopt = ((s.get("avg_r") or 0) > 0 and s.get("t_day") is not None
                 and s["t_day"] >= (base.get("t_day") if base.get("t_day") is not None else -99))
        out[v] = ("risk+" if risk else "risk-") + (" ADOPT" if adopt else "")
    return out

