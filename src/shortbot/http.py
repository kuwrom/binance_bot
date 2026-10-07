"""Guarded Binance REST for scripts: the one way an ad-hoc script, a study or a watcher reads fapi.binance.com.

    .venv/bin/python - <<'PY'
    from shortbot import http
    bars = http.get("/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "1h", "limit": 500})
    PY

Rule (docs/SCANNING.md "Weight guard"): scripts never call fapi.binance.com directly (urllib, requests, httpx, curl).
They use this helper, `shortbot.lab`, or for bulk history the data.binance.vision archive (`shortbot.histdata`,
`shortbot.histdata_vision`), which is a CDN and costs no API weight. On 2026-10-07 a raw-urllib script spent the IP's
whole 2,400/min in under a minute and got the desk's IP banned for 14 minutes; the guardian went blind.

Each call goes through the weight guard (weight.py) as RESEARCH (or DESK for a session's watcher), paced, with its
cost known up front from Binance's published per-endpoint weights (COSTS below), so the host-wide ledger
(weight_ledger.py) holds it in flight for every other process. Its answer is recorded like a ccxt client's: the
used-weight reading, and a 429/418/-1003 with Retry-After stops research and desk calls in every process on the host.
Thread-safe; a loop or a thread pool can't burst past the pacing cap (weight.PACE_WEIGHT_CAP per 10 s).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit

from . import weight

log = logging.getLogger("shortbot.http")

BASE = "https://fapi.binance.com"
VISION = "https://data.binance.vision"
TIMEOUT_S = 15.0
HISTORY_HINT_S = 3 * 86400   # klines starting further back than this belong to the archive

# Request weights, https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api (read
# 2026-10-07). Klines and its variants: by LIMIT [1,100) 1, [100,500) 2, [500,1000] 5, > 1000 10; default 500, max 1500.
KLINES = ("/fapi/v1/klines", "/fapi/v1/continuousKlines", "/fapi/v1/indexPriceKlines", "/fapi/v1/markPriceKlines",
          "/fapi/v1/premiumIndexKlines")
# (with symbol, without symbol)
BY_SYMBOL = {"/fapi/v1/ticker/24hr": (1, 40), "/fapi/v1/ticker/price": (1, 2), "/fapi/v2/ticker/price": (1, 2),
             "/fapi/v1/ticker/bookTicker": (1, 2), "/fapi/v1/premiumIndex": (1, 10)}
# depth by limit: 5/10/20/50 -> 2, 100 -> 5, 500 -> 10, 1000 -> 20; default 500
DEPTH = ((50, 2), (100, 5), (500, 10), (1000, 20))
FLAT = {"/fapi/v1/exchangeInfo": 1, "/fapi/v1/openInterest": 1, "/fapi/v1/time": 1, "/fapi/v1/ping": 1,
        "/fapi/v1/trades": 5, "/fapi/v1/aggTrades": 20, "/fapi/v1/historicalTrades": 20,
        "/fapi/v1/fundingRate": 1, "/fapi/v1/fundingInfo": 0}   # funding: its own 500/5min/IP limit (paced by count)
# /futures/data/* (openInterestHist, takerlongshortRatio, globalLongShortAccountRatio, topLongShort*): weight 0, own
# limit 1,000 requests/5min/IP (paced by count, weight.PACE_LANES)
DATA_PREFIX = "/futures/data/"
UNKNOWN_COST = 5   # an endpoint not in the table: assume a heavy one


class BinanceHTTPError(Exception):
    """Binance answered an error. `limited`: a 429 / 418 / -1003, already recorded host-wide (back off: the guard
    holds research and desk calls until Retry-After)."""

    def __init__(self, status: int, body: str, retry_after: str | None):
        self.status, self.body, self.retry_after = status, body, retry_after
        self.limited = status in (418, 429) or '"code":-1003' in body.replace(" ", "")
        super().__init__(f"HTTP {status}{f' (Retry-After {retry_after}s)' if retry_after else ''}: {body[:200]}")


def klines_cost(limit: int | None) -> int:
    n = 500 if limit is None else int(limit)
    return 1 if n < 100 else 2 if n < 500 else 5 if n <= 1000 else 10


def cost(path: str, params: Mapping[str, Any] | None = None) -> int:
    """The request weight of GET `path` with `params`, from Binance's published table."""
    p = params or {}
    if path in KLINES:
        return klines_cost(p.get("limit"))
    if path in BY_SYMBOL:
        one, every = BY_SYMBOL[path]
        return one if p.get("symbol") else every
    if path == "/fapi/v1/depth":
        n = int(p.get("limit", 500))
        return next((w for top, w in DEPTH if n <= top), 20)
    if path.startswith(DATA_PREFIX):
        return 0
    return FLAT.get(path, UNKNOWN_COST)


def _send(url: str, timeout: float) -> tuple[int, dict[str, str], str]:
    """One GET: (status, headers, body). An HTTP error status is an answer, not an exception (tests replace this)."""
    req = urllib.request.Request(url, headers={"User-Agent": "shortbot-http"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers.items()), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()), e.read().decode("utf-8", "replace")


def _history_hint(path: str, params: Mapping[str, Any]) -> None:
    start = params.get("startTime")
    if path in KLINES and start is not None and int(start) / 1000 < time.time() - HISTORY_HINT_S:
        log.warning("klines from %s days back: bulk history belongs to data.binance.vision (shortbot.histdata), "
                    "which costs no API weight", int((time.time() - int(start) / 1000) // 86400))


def get(path: str, params: Mapping[str, Any] | None = None, *, cls: str = weight.RESEARCH,
        wait_s: float = weight.ENTRY_WAIT_S, base: str = BASE, timeout: float = TIMEOUT_S) -> Any:
    """GET a public Binance endpoint through the weight guard; the parsed JSON. Waits its turn up to `wait_s`
    (weight.BudgetWait after that); BinanceHTTPError on an error answer (`.limited` for 429/418/-1003).
    `cls`: RESEARCH (paced; the default) or DESK (a session's watcher: waits above 80% or during a ban, unpaced)."""
    if cls not in (weight.RESEARCH, weight.DESK):
        raise ValueError(f"shortbot.http serves research and desk reads, not {cls!r}: protective calls go through "
                         "the desk's own clients")
    if not path.startswith("/") or urlsplit(path).netloc:
        raise ValueError(f"a path like /fapi/v1/klines, not {path!r}")
    p = dict(params or {})
    _history_hint(path, p)
    url = f"{base.rstrip('/')}{path}{'?' + urlencode(p) if p else ''}"
    with weight.tagged(cls, wait_s=wait_s, paced=cls == weight.RESEARCH):
        ticket = weight.admit_blocking(url, "GET", cost(path, p))
        try:
            status, headers, body = _send(url, timeout)
            weight.record(url, status, headers, body)
        finally:
            weight.release(ticket)
    if status >= 400:
        raise BinanceHTTPError(status, body, next((v for k, v in headers.items() if k.lower() == "retry-after"), None))
    return json.loads(body) if body else None
