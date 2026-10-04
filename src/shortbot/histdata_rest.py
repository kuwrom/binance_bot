"""REST tail for histdata.py: the last hours the data.binance.vision archive has not published yet (daily files
appear the next morning UTC; monthly funding files only after the month ends). Read-only public endpoints via the
shared ccxt client, paced well under the IP limits the live guardian also uses (<= 4 in flight, ~2.5 req/s)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .histdata_vision import HOUR

MAX_IN_FLIGHT = 4
MIN_GAP_S = 0.4


class Pacer:
    """At most MAX_IN_FLIGHT concurrent calls and one call start per MIN_GAP_S."""

    def __init__(self, in_flight: int = MAX_IN_FLIGHT, gap_s: float = MIN_GAP_S):
        self.sem = asyncio.Semaphore(in_flight)
        self.gap, self.next_at = gap_s, 0.0
        self.lock = asyncio.Lock()
        self.calls = 0

    async def call(self, fn, *args):
        async with self.sem:
            async with self.lock:
                wait = self.next_at - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self.next_at = time.monotonic() + self.gap
            self.calls += 1
            return await fn(*args)


def _kl(rows: list) -> list[dict[str, Any]]:
    return [{"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
             "qv": float(r[7]), "n": int(r[8]), "tbq": float(r[10])} for r in rows]


def _limit(since: int, now: int, cap: int) -> int:
    return max(2, min(cap, (now - since) // HOUR + 2))


async def tail(ex, pacer: Pacer, raw: str, spot_raw: str | None, last: dict[str, int | None],
               start_ms: int, now_ms: int, full: bool = True) -> dict[str, Any]:
    """Fetch closed hourly bars / OI / funding after the last stored timestamps. `last` holds the newest stored ts
    per series (perp, prem, spot, oi, funding) or None. `full=False` (delisted coins): funding only.
    Errors per series are returned, never raised."""
    closed = now_ms - HOUR
    out: dict[str, Any] = {"errors": {}}

    def since(k: str) -> int:
        return (ts + HOUR) if (ts := last.get(k)) else start_ms

    async def kl(k: str, fn, sym: str) -> None:
        s = since(k)
        if s > closed:
            return
        try:
            rows = await pacer.call(fn, {"symbol": sym, "interval": "1h", "startTime": s,
                                         "limit": _limit(s, now_ms, 499)})
            out[k] = [b for b in _kl(rows) if b["ts"] <= closed]
        except Exception as e:  # noqa: BLE001 - a missing tail is a gap, not a failure
            out["errors"][k] = f"{type(e).__name__}: {e}"[:120]

    if full:
        await kl("perp", ex.public.fapiPublicGetKlines, raw)
        await kl("prem", ex.public.fapiPublicGetPremiumIndexKlines, raw)
        if spot_raw:
            await kl("spot", ex.spot.publicGetKlines, spot_raw)
    s_oi = (last.get("oi") or 0) + HOUR
    if full and s_oi <= now_ms:
        try:   # 1h OI stamped H = snapshot at H (same as the metrics hour-end keys); REST keeps ~30 days
            rows = await pacer.call(ex.public.fapiDataGetOpenInterestHist,
                                    {"symbol": raw, "period": "1h", "limit": _limit(max(s_oi, start_ms), now_ms, 500)})
            out["oi"] = {int(r["timestamp"]): {"oi": float(r["sumOpenInterest"]),
                                               "oi_val": float(r["sumOpenInterestValue"])}
                         for r in rows if int(r["timestamp"]) >= s_oi and int(r["timestamp"]) <= now_ms}
        except Exception as e:  # noqa: BLE001
            out["errors"]["oi"] = f"{type(e).__name__}: {e}"[:120]
    s_f = (last.get("funding") or start_ms - 1000) + 1000
    try:
        rows = await pacer.call(ex.public.fapiPublicGetFundingRate, {"symbol": raw, "startTime": s_f, "limit": 1000})
        out["funding"] = [[int(r["fundingTime"]) - int(r["fundingTime"]) % 1000, float(r["fundingRate"])]
                          for r in rows]
    except Exception as e:  # noqa: BLE001
        out["errors"]["funding"] = f"{type(e).__name__}: {e}"[:120]
    return out
