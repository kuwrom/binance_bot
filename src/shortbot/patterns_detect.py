"""Pre-registered event detectors on hourly perp rows (patterns.align). Thresholds come from the literature
(docs/PATTERNS.md) and are fixed BEFORE looking at outcomes; every detector tried is counted in the report.
All detectors use only data up to and including hour i (no look-ahead); the event's entry is the close of i.

pump_kk      Kamps & Kleinberg 2018 "balanced": volume > 4x its 12h mean AND high > 5% above the 12h mean close
pump_fail    first red 1h close within 6h after a pump_kk (the 'wait for failure' entry)
fake_volume  volume z > 3 (72h) while |return| and range stay below their 72h medians and |dOI| < 2% of volume
sweep_high   wick >= 1.5 ATR(14) past the prior 24h high, then a close back below it (same bar or next 2)
squeeze_up   1h return > 3 ATR% with OI DOWN >= 3% in the hour (shorts forced out) -> short the exhaustion
funding_hi   a funding print >= +0.05% (longs pay heavily)
funding_neg  a funding print <= -0.05% (crowded shorts)
premium_hi   perp premium >= 0.5% over the index at the hour's close
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime
from typing import Any

from .sessions import session_bucket

DEDUP_H = {"pump_kk": 72, "pump_fail": 72, "fake_volume": 24, "sweep_high": 24, "squeeze_up": 24,
           "funding_hi": 24, "funding_neg": 24, "premium_hi": 24}


def _atr_pct(rows: list[dict], i: int, n: int = 14) -> float:
    tr = [(r["h"] - r["l"]) / r["c"] * 100 for r in rows[max(0, i - n):i] if r["c"]]
    return statistics.fmean(tr) if tr else 0.0


def _oi_chg(rows: list[dict], i: int, back: int) -> float | None:
    a, b = rows[i - back].get("oi") if i >= back else None, rows[i].get("oi")
    return (b / a - 1) * 100 if a and b else None


def context(rows: list[dict], i: int) -> dict[str, Any]:
    """What the pump/event looked like: leverage (OI), perp premium, spot share, time bucket."""
    r = rows[i]
    oi3 = _oi_chg(rows, i, 3)
    spot = sum(x["spot_qv"] or 0 for x in rows[max(0, i - 2):i + 1])
    perp = sum(x["qv"] for x in rows[max(0, i - 2):i + 1])
    lev = ("n/a" if oi3 is None else "oi_up>10%" if oi3 >= 10 else "oi_down" if oi3 <= -3 else "oi_flat")
    return {"session": session_bucket(datetime.fromtimestamp(r["ts"] / 1000 + 3600, UTC)),
            "oi_chg_3h": round(oi3, 2) if oi3 is not None else None, "leverage": lev,
            "premium_bp": round(r["prem"] * 1e4, 1) if r.get("prem") is not None else None,
            "spot_share": round(spot / (spot + perp), 3) if spot + perp and rows[i].get("spot_c") else None,
            "has_spot": rows[i].get("spot_c") is not None}


def detect(rows: list[dict], funding: list[list[float]] | None = None) -> list[tuple[str, int]]:
    """(kind, hour index) events, each kind de-duplicated per coin (DEDUP_H) so one episode counts once."""
    raw: list[tuple[str, int]] = []
    for i in range(72, len(rows)):
        r, prev12 = rows[i], rows[i - 12:i]
        vmean = statistics.fmean(x["qv"] for x in prev12)
        cmean = statistics.fmean(x["c"] for x in prev12)
        if vmean and r["qv"] > 4 * vmean and r["h"] > 1.05 * cmean:
            raw.append(("pump_kk", i))
        win = rows[i - 72:i]
        vs = [x["qv"] for x in win]
        sd = statistics.pstdev(vs)
        rets = sorted(abs(x["c"] / x["o"] - 1) for x in win if x["o"])
        rngs = sorted((x["h"] - x["l"]) / x["c"] for x in win if x["c"])
        doi = abs((r.get("oi") or 0) - (rows[i - 1].get("oi") or 0)) if r.get("oi") and rows[i - 1].get("oi") else None
        if (sd and (r["qv"] - statistics.fmean(vs)) / sd > 3 and r["o"] and r["c"]
                and abs(r["c"] / r["o"] - 1) < rets[len(rets) // 2] and (r["h"] - r["l"]) / r["c"] < rngs[len(rngs) // 2]
                and doi is not None and doi < 0.02 * r["qv"]):
            raw.append(("fake_volume", i))
        for k in range(0, 3):  # wick in bar j = i-k; i is the FIRST close back below the swept high
            j = i - k
            hj = max(x["h"] for x in rows[j - 24:j])
            atr = _atr_pct(rows, j) / 100 * rows[j]["c"]
            if (atr and rows[j]["h"] >= hj + 1.5 * atr and r["c"] < hj
                    and all(rows[m]["c"] >= hj for m in range(j, i))):
                raw.append(("sweep_high", i))
                break
        ret1 = (r["c"] / rows[i - 1]["c"] - 1) * 100 if rows[i - 1]["c"] else 0
        oi1 = _oi_chg(rows, i, 1)
        if ret1 > 3 * _atr_pct(rows, i) and oi1 is not None and oi1 <= -3:
            raw.append(("squeeze_up", i))
        if r.get("prem") is not None and r["prem"] >= 0.005:
            raw.append(("premium_hi", i))
    for ts, rate in funding or []:
        at = _index_at(rows, ts)
        if at is not None and at >= 72:
            if rate >= 0.0005:
                raw.append(("funding_hi", at))
            elif rate <= -0.0005:
                raw.append(("funding_neg", at))
    raw += [("pump_fail", j) for j in _pump_fail(rows, [i for k, i in raw if k == "pump_kk"])]
    return _dedup(raw)


def _pump_fail(rows: list[dict], pumps: list[int]) -> list[int]:
    out = []
    for i in pumps:
        for j in range(i + 1, min(i + 7, len(rows))):
            if rows[j]["c"] < rows[j]["o"]:
                out.append(j)
                break
    return out


def _index_at(rows: list[dict], ts: float) -> int | None:
    """Hour whose CLOSE is at or just before ts (a funding print at ts is known from then on)."""
    lo, hi = 0, len(rows) - 1
    if not rows or ts < rows[0]["ts"] + 3_600_000:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if rows[mid]["ts"] + 3_600_000 <= ts:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _dedup(raw: list[tuple[str, int]]) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    last: dict[str, int] = {}   # kind -> its last kept hour
    for kind, i in sorted(set(raw), key=lambda x: (x[0], x[1])):
        if i - last.get(kind, -10_000) >= DEDUP_H[kind]:
            out.append((kind, i))
            last[kind] = i
    return out
