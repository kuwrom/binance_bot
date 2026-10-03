"""Triple-barrier outcome labeling (López de Prado AFML ch.3) on 1m MARK-price candles, the series our
stop-markets trigger on (workingType=MARK_PRICE). Same-candle ambiguity (both barriers inside one 1m bar) is
resolved conservatively: the stop counts first."""

from __future__ import annotations

from typing import Any


def label_bracket(candles_1m: list[list[float]], entry: float, stop: float, target: float) -> dict[str, Any]:
    """Short bracket: 'target' if the low reaches target before the high reaches stop, 'stop' vice versa,
    'time' if neither within the candles given. MFE/MAE in R (R = stop − entry)."""
    r = stop - entry
    if r <= 0 or not candles_1m:
        return {"outcome": "no_data"}
    mfe = mae = 0.0
    for c in candles_1m:
        hi, lo = c[2], c[3]
        mfe, mae = max(mfe, (entry - lo) / r), max(mae, (hi - entry) / r)
        if hi >= stop:
            return {"outcome": "stop", "mfe_r": round(mfe, 3), "mae_r": round(mae, 3)}
        if lo <= target:
            return {"outcome": "target", "mfe_r": round(mfe, 3), "mae_r": round(mae, 3)}
    return {"outcome": "time", "mfe_r": round(mfe, 3), "mae_r": round(mae, 3), "close_r": round((entry - candles_1m[-1][4]) / r, 3)}


def label_touch(candles_1m: list[list[float]], level: float, above: bool) -> dict[str, Any]:
    """Did price touch `level` (from above or below) within the candles? Used for limit-fill predictions."""
    if not candles_1m:
        return {"outcome": "no_data"}
    hit = any((c[2] >= level) if above else (c[3] <= level) for c in candles_1m)
    return {"outcome": "hit" if hit else "miss"}


async def fetch_mark_1m(ex, symbol: str, since_ms: int, hours: float) -> list[list[float]]:
    need = int(hours * 60) + 1
    out: list[list[float]] = []
    cursor = since_ms
    while len(out) < need:
        rows = await ex.public.fetch_ohlcv(symbol, "1m", since=cursor, limit=min(1000, need - len(out)),
                                           params={"price": "mark"})
        if not rows:
            break
        out.extend(rows)
        cursor = int(rows[-1][0]) + 60_000
        if len(rows) < 2:
            break
    end = since_ms + int(hours * 3_600_000)
    return [c for c in out if since_ms <= c[0] < end]


async def label_due(ex, journal, now_ms: int, limit: int = 40) -> int:
    """Label predictions whose horizon has passed. Returns how many were labelled."""
    done = []
    for p in journal.due_predictions(now_ms, limit):
        try:
            candles = await fetch_mark_1m(ex, p["symbol"], p["ts_ms"], p["hours"])
        except Exception:  # noqa: BLE001 - delisted / renamed: don't retry forever
            candles = []
        if p["kind"] == "bracket":
            lab = label_bracket(candles, p["entry"], p["stop"], p["target"])
        else:
            lab = label_touch(candles, p["target"], above=p["target"] >= p["entry"])
        done.append((p["id"], lab["outcome"], lab.get("mfe_r"), lab.get("mae_r")))
    if done:   # one unit per run (#79)
        await journal.write(lambda j: [j.label_prediction(*x) for x in done])
    return len(done)
