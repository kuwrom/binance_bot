"""Binance REST at the HTTP layer, for the weight guard (#70): a stand-in for the aiohttp session ccxt 4.5.84 drives, so
the real `fetch` -> `on_rest_response` -> `handle_errors` path runs. What it models, and where it comes from:

- ccxt's async `fetch` (ccxt/async_support/base/exchange.py ~168-300) calls `session.<method>(url, ...)` as an async
  context manager and reads `await response.text(errors=...)`, `response.headers`, `.status` and `.reason`; it copies
  the headers into a plain dict that keeps the server's casing.
- Every response carries the IP's used weight in `x-mbx-used-weight-1m`: "Every request will contain
  X-MBX-USED-WEIGHT-(intervalNum)(intervalLetter) in the response headers"
  (https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info,
  https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md). Binance sends it lowercase: measured
  2026-10-02 on fapi.binance.com over HTTP/1.1 and HTTP/2.
- 429 (over the limit) and 418 (IP ban) come with `Retry-After` in seconds (same pages; the official SDK parses it as an
  int: binance-connector-python common/src/binance_common/utils.py `parse_retry_after`). Their body is -1003
  TOO_MANY_REQUESTS (https://github.com/binance/binance-spot-api-docs/blob/master/errors.md).
- exchangeInfo `rateLimits` (official SDK fixture clients/derivatives_trading_usds_futures/tests/unit/rest_api/
  test_market_data_api.py ~1061). Measured 2026-10-02: fapi.binance.com REQUEST_WEIGHT 2400/min, demo-fapi 6000/min.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

TOO_MUCH = ("Too much request weight used; current limit is {limit} request weight per 1 MINUTE. Please use WebSocket "
            "Streams for live updates to avoid polling the API.")
BANNED = ("Way too much request weight used; IP banned until 1759400000000. Please use WebSocket Streams for live "
          "updates to avoid bans.")


def rate_limits(limit: int) -> list[dict]:
    return [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": limit},
            {"rateLimitType": "ORDERS", "interval": "MINUTE", "intervalNum": 1, "limit": 1200},
            {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 300}]


class FakeHost:
    """One Binance host. Each request adds `step` to the used weight its response reports; `answer_next(status)`
    makes the next requests answer 429/418 instead."""

    def __init__(self, name: str, limit: int = 2400, step: int = 1):
        self.name, self.limit, self.step = name, limit, step
        self.used = 0
        self.sent: list[tuple[str, str]] = []   # (method, path) in the order they reached the host
        self._limited: list[tuple[int, int]] = []   # queued (status, retry_after)

    def answer_next(self, status: int, retry_after: int = 30, times: int = 1) -> None:
        self._limited += [(status, retry_after)] * times

    def paths(self, suffix: str = "") -> list[str]:
        return [p for _, p in self.sent if p.endswith(suffix)]

    def respond(self, method: str, path: str) -> FakeResponse:
        self.sent.append((method, path))
        self.used += self.step
        headers = {"content-type": "application/json", "x-mbx-used-weight-1m": str(self.used)}
        if self._limited:
            status, retry = self._limited.pop(0)
            headers["retry-after"] = str(retry)
            msg = BANNED if status == 418 else TOO_MUCH.format(limit=self.limit)
            reason = "I'm a teapot" if status == 418 else "Too Many Requests"
            return FakeResponse(status, reason, headers, json.dumps({"code": -1003, "msg": msg}))
        if path.endswith("/exchangeInfo"):
            body = {"timezone": "UTC", "serverTime": 1759400000000, "rateLimits": rate_limits(self.limit),
                    "exchangeFilters": [], "assets": [], "symbols": []}
        elif path.endswith("/time"):
            body = {"serverTime": 1759400000000}
        elif path.endswith("/klines"):
            body = []
        else:
            body = {}
        return FakeResponse(200, "OK", headers, json.dumps(body))


class FakeResponse:
    def __init__(self, status: int, reason: str, headers: dict, text: str):
        self.status, self.reason, self.headers, self._text = status, reason, headers, text

    async def text(self, errors: str = "strict") -> str:
        return self._text

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class FakeSession:
    """aiohttp.ClientSession as ccxt uses it: get/post/put/delete return an async context manager."""

    def __init__(self, *hosts: FakeHost):
        self.hosts = {h.name: h for h in hosts}
        self.headers: dict = {}   # the session's default headers (ccxt merges them into each request)
        self.closed = False

    def _req(self, method: str, url) -> FakeResponse:
        u = urlsplit(str(url))
        return self.hosts[u.hostname].respond(method, u.path)

    def get(self, url, **kw):
        return self._req("GET", url)

    def post(self, url, **kw):
        return self._req("POST", url)

    def put(self, url, **kw):
        return self._req("PUT", url)

    def delete(self, url, **kw):
        return self._req("DELETE", url)

    async def close(self) -> None:
        self.closed = True


def wire(client, session: FakeSession):
    """Point a ccxt client at the fake (no ccxt throttle: the tests run on the guard's fake clock)."""
    client.session = session
    client.enableRateLimit = False
    client.timeout_on_exit = 0   # ccxt's close() pause
    return client


def fake_clock(monkeypatch, weight):
    """The guard's clocks: `t` (monotonic) and `wall` advance only through the guard's own sleeps."""
    import asyncio
    from types import SimpleNamespace
    c = SimpleNamespace(t=1000.0, wall=1_759_400_100.0, slept=0.0)

    async def sleep(s):
        c.t += s
        c.wall += s
        c.slept += s
        await asyncio.sleep(0)
    monkeypatch.setattr(weight, "_now", lambda: c.t)
    monkeypatch.setattr(weight, "_wall", lambda: c.wall)
    monkeypatch.setattr(weight, "_sleep", sleep)
    return c
