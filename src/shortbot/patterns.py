"""Pattern / manipulation event study (read-only research). For every tradable perp, over the cached hourly data
(patterns_data.py): find events (patterns_detect.py), measure what a SHORT from the event's close would have done
(1h/4h/24h/72h return, 24h max adverse / favourable excursion), and compare with a control drawn from the SAME
coins at random non-event hours. Everything is split by liquidity bucket (OI and 24h volume) to answer "how thin
can we go". Results are anecdotes until n (distinct coins!) is large: 1 week = one market regime."""

from __future__ import annotations

import random
import statistics
from typing import Any

OI_BUCKETS_M = (0, 5, 10, 20, 30, 60, 150, float("inf"))
VOL_BUCKETS_M = (0, 2, 5, 10, 20, 50, float("inf"))
HORIZONS = (1, 4, 24, 72)
SQUEEZE_PCT = 15.0      # a 24h max adverse move beyond this = a squeeze that would have run any sane stop
COST_PCT = 0.1          # round-trip fees + slippage, per trade, in %


def align(coin: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per perp hour, with OI (USD), premium and spot close/volume joined on the hour."""
    spot = {b["ts"]: b for b in coin.get("spot") or []}
    oi, prem = coin.get("oi") or {}, coin.get("premium") or {}
    out = []
    for b in coin["perp"]:
        s = spot.get(b["ts"])
        # OI hist timestamps mark the END of the hour; the kline ts is its start
        out.append({**b, "oi": oi.get(b["ts"] + 3_600_000) or oi.get(b["ts"]), "prem": prem.get(b["ts"]),
                    "spot_c": s["c"] if s else None, "spot_qv": s["qv"] if s else None})
    return out


def forward(rows: list[dict[str, Any]], i: int) -> dict[str, Any] | None:
    """Short from the close of hour i. None if not even 24h of future exists."""
    p0 = rows[i]["c"]
    if i + 24 >= len(rows) or not p0:
        return None
    out: dict[str, Any] = {}
    for h in HORIZONS:
        out[f"ret_{h}h"] = round((p0 - rows[i + h]["c"]) / p0 * 100, 3) if i + h < len(rows) else None
    nxt = rows[i + 1:i + 25]
    out["mae_24h"] = round((max(r["h"] for r in nxt) - p0) / p0 * 100, 3)
    out["mfe_24h"] = round((p0 - min(r["l"] for r in nxt)) / p0 * 100, 3)
    return out


def liquidity(rows: list[dict[str, Any]], i: int) -> dict[str, Any]:
    qv24 = sum(r["qv"] for r in rows[max(0, i - 23):i + 1])
    oi = rows[i].get("oi")
    return {"vol24_m": round(qv24 / 1e6, 2), "oi_m": round(oi / 1e6, 2) if oi else None,
            "oi_bucket": bucket(oi / 1e6 if oi else None, OI_BUCKETS_M),
            "vol_bucket": bucket(qv24 / 1e6, VOL_BUCKETS_M)}


def bucket(v: float | None, edges: tuple[float, ...]) -> str:
    if v is None:
        return "n/a"
    for lo, hi in zip(edges, edges[1:]):
        if v < hi:
            return f"{lo:g}-{hi:g}M" if hi != float("inf") else f">{lo:g}M"
    return "n/a"


def controls(rows: list[dict[str, Any]], busy: set[int], rng: random.Random, k: int, start: int = 24) -> list[int]:
    """Up to k random hours of the same coin that are at least 24h away from any event (no overlap)."""
    free = [i for i in range(start, len(rows) - 24) if not any(abs(i - j) < 24 for j in busy)]
    return sorted(rng.sample(free, min(k, len(free))))


def summarize(evs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Distribution of short outcomes. n_coins matters more than n (events of one coin are not independent)."""
    ev = [e for e in evs if e.get("ret_24h") is not None]
    if not ev:
        return None
    r24 = [e["ret_24h"] for e in ev]
    mae = sorted(e["mae_24h"] for e in ev)
    coin_means: dict[str, list[float]] = {}
    for e in ev:
        coin_means.setdefault(e["symbol"], []).append(e["ret_24h"])
    out = {
        "n": len(ev), "n_coins": len(coin_means),
        "short_ret_24h_mean": round(statistics.fmean(r24), 2), "short_ret_24h_median": round(statistics.median(r24), 2),
        "coin_weighted_mean_24h": round(statistics.fmean(statistics.fmean(v) for v in coin_means.values()), 2),
        "win_rate_24h": round(sum(r > COST_PCT for r in r24) / len(r24) * 100, 1),
        "squeeze_rate": round(sum(m > SQUEEZE_PCT for m in mae) / len(mae) * 100, 1),
        "mae_p50": round(mae[len(mae) // 2], 2), "mae_p90": round(mae[int(len(mae) * 0.9)], 2),
    }
    for h in (1, 4, 72):
        xs = [e[f"ret_{h}h"] for e in ev if e.get(f"ret_{h}h") is not None]
        if xs:
            out[f"short_ret_{h}h_mean"] = round(statistics.fmean(xs), 2)
    return out


def by(evs: list[dict[str, Any]], key: str, min_n: int = 5) -> dict[str, Any]:
    groups: dict[str, list] = {}
    for e in evs:
        groups.setdefault(str(e.get(key)), []).append(e)
    return {k: s for k, g in sorted(groups.items()) if len(g) >= min_n and (s := summarize(g))}


def _btc_short(btc: dict[int, float], ts: int, h: int) -> float | None:
    a, b = btc.get(ts), btc.get(ts + h * 3_600_000)
    return (a - b) / a * 100 if a and b else None


def run(coins: list[dict[str, Any]], seed: int = 7, ctrl_per_event: int = 3) -> dict[str, Any]:
    """Events + matched same-coin controls, each with short outcomes, BTC-excess return, liquidity and context."""
    from .patterns_detect import DEDUP_H, context, detect
    rng = random.Random(seed)
    btc_c = next((c for c in coins if c["symbol"].startswith("BTC/")), None)
    btc = {b["ts"]: b["c"] for b in btc_c["perp"]} if btc_c else {}
    events, ctrl = [], []
    for c in coins:
        rows = align(c)
        if len(rows) < 120:
            continue
        found = detect(rows, c.get("funding"))

        def rec(i: int, kind: str) -> dict[str, Any] | None:
            f = forward(rows, i)
            if not f:
                return None
            bx = _btc_short(btc, rows[i]["ts"], 24)
            return {"symbol": c["symbol"], "kind": kind, "ts": rows[i]["ts"], **f, **liquidity(rows, i),
                    **context(rows, i), "excess_24h": round(f["ret_24h"] - bx, 3) if bx is not None else None,
                    "age_d": round((rows[i]["ts"] - c["onboard_ms"]) / 864e5) if c.get("onboard_ms") else None}

        events += [e for k, i in found if (e := rec(i, k))]
        busy = {i for _, i in found}
        n_ctrl = ctrl_per_event * max(1, len(found))
        ctrl += [e for i in controls(rows, busy, rng, n_ctrl, start=72) if (e := rec(i, "control"))]
    return {"events": events, "controls": ctrl, "detectors": sorted(DEDUP_H)}


def _t(a: list[float], b: list[float]) -> float | None:
    """Welch t of mean(a) - mean(b)."""
    if len(a) < 3 or len(b) < 3:
        return None
    va, vb = statistics.variance(a) / len(a), statistics.variance(b) / len(b)
    return round((statistics.fmean(a) - statistics.fmean(b)) / (va + vb) ** 0.5, 2) if va + vb else None


def report(res: dict[str, Any], min_n: int = 5) -> dict[str, Any]:
    ev, ctrl = res["events"], res["controls"]
    cx = [e["excess_24h"] for e in ctrl if e.get("excess_24h") is not None]
    out: dict[str, Any] = {"control": summarize(ctrl), "tests_run": 0, "kinds": {}}
    for kind in res["detectors"]:
        g = [e for e in ev if e["kind"] == kind]
        s = summarize(g)
        if not s:
            continue
        gx = [e["excess_24h"] for e in g if e.get("excess_24h") is not None]
        out["tests_run"] += 1
        out["kinds"][kind] = {**s, "excess_24h_mean": round(statistics.fmean(gx), 2) if gx else None,
                              "t_vs_control": _t(gx, cx),
                              "by_oi": by(g, "oi_bucket", min_n), "by_vol": by(g, "vol_bucket", min_n),
                              "by_session": by(g, "session", min_n), "by_leverage": by(g, "leverage", min_n),
                              "by_spot": by(g, "has_spot", min_n)}
    out["how_to_read"] = ("short_ret > 0 = price fell (short won). excess = short return minus BTC's. t_vs_control "
                          "compares BTC-excess returns with same-coin random hours; need |t| > 3 given the number of "
                          "tests. n_coins matters more than n. One week = one regime: hypotheses only.")
    return out
