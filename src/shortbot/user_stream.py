"""The guardian's user-data stream (#54). Binance pushes our order events; the guardian runs the job that already
reconciles them (the fill poll, the positions pass) at once instead of at its next tick, 5 s or 60 s later. Latency,
not a source of truth: the REST polls still run on schedule, and every handler is idempotent (client ids #9,
compare-and-set), so a missed, late, duplicated or reordered event can't make a wrong state. What wakes what:
user_events.py.

Binance USDⓈ-M:
- POST /fapi/v1/listenKey (weight 1) returns the account's active key, extended 60 min, or a new one. PUT extends it
  (no key sent: the account's) and answers {"listenKey"}; -1125 when none is active. "User data streams will close
  after 60 minutes" without a keepalive (binance-connector-python, derivatives_trading_usds_futures
  rest_api/api/user_data_streams_api.py and its KeepaliveUserDataStreamResponse). One key per account, shared by every
  process on it: it is never DELETEd (that would end theirs, probe_stream.py). Both calls are Trader primitives.
- URL <host>/private/ws?listenKey=K. Change log 2026-03-05: user-data events moved to the `/private` path; the
  Important-WebSocket-Change-Notice gives wss://fstream.binance.com/private/ws?listenKey=... and keeps the legacy
  paths until 2026-04-23. Demo: wss://demo-fstream.binance.com (General Info); events arrived there on 2026-10-02
  (probe_result 387).
- A connection lives 24 h: renewed after 23 h, as the official SDK does (binance_common constants
  AUTO_RECONNECT_INTERVAL_SECONDS; its README: "renewed ... before the 24 hours expiration"). The server pings every
  3 min and websockets answers; it pings every 60 s itself, so a half-open socket closes.
- A made-up key still connects on `/private` (measured 2026-10-02), so a connection proves nothing. The stream is up
  only while its key is confirmed: by the POST it connected with and every keepalive since, with no listenKeyExpired.
  A fill the poll found that the stream never reported (`expect`) is a miss; MISSES_RECONNECT in a row reconnect.

Isolation: a supervised guardian task on the guardian's loop. Per event it parses JSON and looks up dicts; every await
is time-boxed; a handler is Lane.trigger, which only brings a job's next run forward. Down for DOWN_ALERT_S: one
alert, and one when it's back. Measured: events per type and kind, triggers per job, hits and misses, E->receive lag
(exchange vs local clock, skew included: probe 387 saw -70 ms), entry partial->full times; the protective lane adds
each triggered run's delay after the event and its lead over the schedule (guardian_loop.Lane.trigger). Written to kv
`user_stream` every STATS_S and at each connect (`last_connect_at`): a restarted guardian's stream shows up at once (the
restart drill, #46)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
import time
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from . import user_events as ue
from .guardian_loop import send_safe
from .redact import redact

log = logging.getLogger("shortbot.user_stream")
HOSTS = {"demo": "wss://demo-fstream.binance.com", "live": "wss://fstream.binance.com"}
KEEPALIVE_S = 30 * 60      # the key lives 60 min after each keepalive: a failed one is retried well before it lapses
KEEPALIVE_RETRY_S = 60
ROTATE_S = 23 * 3600
CONNECT_S = 15.0
REST_S = 30.0
CLOSE_S = 5.0
WAKE_S = 5.0               # the session's and the watch's tick: keepalive, rotation, misses, alerts
BACKOFF_MIN_S, BACKOFF_MAX_S = 1.0, 60.0   # "IPs that are repeatedly disconnected may be banned" (market/streams.py)
STABLE_S = 60.0            # a connection that lasted this long resets the backoff
DOWN_ALERT_S = 600
EXPECT_S = 15.0            # a fill the poll found must have reached the stream within this
UP_MARGIN_MS = 2000        # ...if it filled at least this long after the stream came up (fill times are to the second)
MISSES_RECONNECT = 2
STATS_S = 600
STATS_KEY = "user_stream"
REMEMBER = 2000            # client ids and triggered-order ids kept for matching


async def open_socket(url: str):
    import truststore  # the OS trust store, like market/liquidations.py
    import websockets
    return await websockets.connect(url, ssl=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), ping_interval=60,
                                    max_size=2 ** 20, open_timeout=CONNECT_S)


def _remember(d: OrderedDict, k: str, v: Any) -> None:
    d[k] = v
    d.move_to_end(k)
    while len(d) > REMEMBER:
        d.popitem(last=False)


class UserStream:
    def __init__(self, trader, notifier, on_hit: Callable[[ue.Hit, float], Any],
                 connect: Callable[[str], Awaitable[Any]] | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 extra_stats: Callable[[], dict] | None = None):
        self.tr, self.n, self.j, self.on_hit = trader, notifier, trader.j, on_hit
        self.mode = "live" if trader.ex.settings.is_live else "demo"
        self.connect = connect or (lambda url: open_socket(url))
        self.clock, self.sleep, self.extra_stats = clock, sleep, extra_stats or dict
        self.key: str | None = None
        self.key_ok = self.connected = self.alerted = False
        self.connected_at = 0.0
        self.up_since_ms: int | None = None
        self.down_since: float = clock()
        self.last_error: str | None = None
        self.next_keepalive = float("inf")
        self.reconnect_why: str | None = None
        self.actual: OrderedDict[str, str] = OrderedDict()   # triggered order id -> the stop's kind
        self.traded: OrderedDict[str, float] = OrderedDict()  # our client id -> when a TRADE for it arrived
        self.partial: dict[str, int] = {}                     # entry client id -> its first PARTIALLY_FILLED E
        self.expected: dict[str, float] = {}                  # client id -> when the poll found it filled
        self.last_foreign: dict[str, dict] = {}               # symbol -> the last order event not of ours
        self.fails = self.misses_in_row = 0
        self.stats: dict[str, Any] = {
            "since": datetime.now(UTC).isoformat(timespec="seconds"), "events": Counter(), "kinds": Counter(),
            "triggers": Counter(), "reconnects": Counter(), "connects": 0, "errors": 0, "keepalives": 0,
            "keepalive_failures": 0, "hits": 0, "misses": 0, "lag_ms": {"n": 0, "sum": 0, "max": None},
            "partial_to_full_ms": []}

    @property
    def up(self) -> bool:
        return self.connected and self.key_ok

    def url(self, key: str) -> str:
        return f"{HOSTS[self.mode]}/private/ws?listenKey={key}"

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tg:   # one dies (a bug): both stop, and `supervised` restarts the stream
            tg.create_task(self._sessions())
            tg.create_task(self._watch())

    # ------------------------------------------------------------ connection
    async def _sessions(self) -> None:
        """Planned reconnects (rotation, a new key, misses) are immediate; failures back off, BACKOFF_MIN_S doubling
        to BACKOFF_MAX_S, from the start again after a connection that held STABLE_S."""
        while True:
            try:
                why = await self._session()
            except Exception as e:  # noqa: BLE001 - a refused or dropped socket, a failed key: back off, reconnect
                self.stats["errors"] += 1
                self.last_error = redact(f"{type(e).__name__}: {e}")[:300]
                if self.connected_at and self.clock() - self.connected_at >= STABLE_S:
                    self.fails = 0
                delay = min(BACKOFF_MAX_S, BACKOFF_MIN_S * 2 ** self.fails)
                self.fails, self.connected_at = self.fails + 1, 0.0
                log.warning("user stream down (%s); reconnecting in %gs", self.last_error, delay)
                await self.sleep(delay)
            else:
                self.stats["reconnects"][why] += 1
                self.fails = 0
                log.info("user stream: reconnecting (%s)", why)

    async def _session(self) -> str:
        """One connection, until a planned reconnect (its reason returned) or a failure (raised)."""
        key = await asyncio.wait_for(self.tr.listen_key(), REST_S)
        ws = await asyncio.wait_for(self.connect(self.url(key)), CONNECT_S)
        try:
            self.key, self.key_ok, self.connected, self.reconnect_why = key, True, True, None
            self.connected_at, self.up_since_ms = self.clock(), int(time.time() * 1000)   # vs exchange fill times
            self.next_keepalive = self.connected_at + KEEPALIVE_S
            self.stats["connects"] += 1
            self.stats["last_connect_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            self._persist()   # posted: never waits for the journal
            log.info("user stream connected (%s, %s)", self.mode, HOSTS[self.mode])
            while True:
                if self.reconnect_why:
                    return self.reconnect_why
                if self.clock() - self.connected_at >= ROTATE_S:
                    return "24 h cut"
                try:
                    frame = await asyncio.wait_for(ws.recv(), WAKE_S)   # websockets: cancelling recv() loses nothing
                except TimeoutError:
                    continue
                self._frame(frame)
        finally:
            self.connected = False
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), CLOSE_S)

    async def _keepalive(self) -> None:
        """PUT; if it fails, POST (a new key if the old one is gone). Another key than ours: reconnect on it."""
        try:
            key = await asyncio.wait_for(self.tr.keep_listen_key(), REST_S)
        except Exception as e:  # noqa: BLE001 - the key may be gone (-1125): ask for the account's key instead
            log.warning("user stream keepalive failed (%s); asking for the account's key",
                        redact(f"{type(e).__name__}: {e}"))
            try:
                key = await asyncio.wait_for(self.tr.listen_key(), REST_S)
            except Exception as e2:  # noqa: BLE001 - down until a retry works; the polls cover it meanwhile
                self.key_ok = False
                self.stats["keepalive_failures"] += 1
                self.last_error = redact(f"keepalive: {type(e2).__name__}: {e2}")[:300]
                self.next_keepalive = self.clock() + KEEPALIVE_RETRY_S
                log.warning("user stream key unconfirmed (%s); retrying in %gs", self.last_error, KEEPALIVE_RETRY_S)
                return
        self.next_keepalive = self.clock() + KEEPALIVE_S
        if key != self.key:
            self.key_ok, self.reconnect_why = False, "listenKey changed"
            return
        self.key_ok = True
        self.stats["keepalives"] += 1

    # ------------------------------------------------------------ events
    def _frame(self, frame: str | bytes) -> None:
        rx, rx_ms = self.clock(), int(time.time() * 1000)
        try:
            ev = json.loads(frame)
        except ValueError:
            self.stats["events"]["unparsable"] += 1
            return
        if not isinstance(ev, dict):
            self.stats["events"]["unparsable"] += 1
            return
        hit = ue.classify(ev, self.actual)
        self._count(hit, rx_ms)
        if hit.kind == "expired":
            self.key_ok, self.reconnect_why = False, "listenKeyExpired"
            return
        if hit.actual_id:
            _remember(self.actual, hit.actual_id, hit.kind)
            seen = self.last_foreign.get(str(hit.symbol)) or {}
            if hit.kind != "foreign" and seen.get("order_id") == hit.actual_id:
                del self.last_foreign[str(hit.symbol)]   # it was our stop's fill, seen before its ALGO_UPDATE
        if hit.exec_type == "TRADE" and hit.cid:
            _remember(self.traded, str(hit.cid), rx)
        if hit.kind == "entry":
            self._partial(hit)
        if hit.type in ("ORDER_TRADE_UPDATE", "ALGO_UPDATE"):
            self._log(hit)
        if hit.job:
            self.stats["triggers"][hit.job] += 1
            try:
                self.on_hit(hit, rx)
            except Exception:  # noqa: BLE001 - a handler bug never stops the stream; the polls still run
                log.exception("user stream handler failed for %s", hit)

    def _count(self, hit: ue.Hit, rx_ms: int) -> None:
        self.stats["events"][hit.type] += 1
        self.stats["kinds"][hit.kind] += 1
        if hit.event_ms:
            lag, s = rx_ms - hit.event_ms, self.stats["lag_ms"]
            s["n"], s["sum"], s["max"] = s["n"] + 1, s["sum"] + lag, lag if s["max"] is None else max(s["max"], lag)

    def _partial(self, hit: ue.Hit) -> None:
        cid = str(hit.cid)
        if hit.status == "PARTIALLY_FILLED" and hit.event_ms:
            self.partial.setdefault(cid, hit.event_ms)
        elif hit.status in ue.ENTRY_DONE and cid in self.partial:
            first = self.partial.pop(cid)
            if hit.status == "FILLED" and hit.event_ms:
                took = hit.event_ms - first
                self.stats["partial_to_full_ms"] = [*self.stats["partial_to_full_ms"][-49:], took]
                log.info("user stream: entry %s filled %d ms after its first partial fill", cid, took)

    def _log(self, hit: ue.Hit) -> None:
        if hit.kind != "foreign":
            log.info("user stream %s %s %s %s %s/%s -> %s", hit.type, hit.kind, hit.symbol, hit.cid, hit.exec_type,
                     hit.status, hit.job or "-")
            return
        self.last_foreign[str(hit.symbol)] = {
            "cid": hit.cid, "status": hit.status, "side": hit.side, "qty": hit.qty, "order_id": hit.order_id,
            "at": datetime.fromtimestamp((hit.event_ms or 0) / 1000, UTC).isoformat(timespec="seconds")}
        log.info("user stream: order %s on %s (%s %s %s) is not the desk's: no action; the untracked check quotes it",
                 hit.cid, hit.symbol, hit.side, hit.qty, hit.status)

    # ------------------------------------------------------------ health
    def expect(self, cid: str | None, fill_ms: int | None) -> None:
        """The poll found this order filled: the stream should have said so. Only a fill made while the stream was up
        is judged (in `_judge`)."""
        if (cid and fill_ms is not None and self.up and self.up_since_ms is not None
                and fill_ms >= self.up_since_ms + UP_MARGIN_MS):
            self.expected.setdefault(cid, self.clock())

    def _judge(self, now: float) -> None:
        for cid, at in list(self.expected.items()):
            if cid in self.traded:
                self.stats["hits"] += 1
                self.misses_in_row = 0
            elif not self.up:
                pass   # no verdict on a stream that went down meanwhile
            elif now - at >= EXPECT_S:
                self.stats["misses"] += 1
                self.misses_in_row += 1
                log.warning("user stream missed the fill of %s (the poll found it)", cid)
            else:
                continue
            del self.expected[cid]
        if self.misses_in_row >= MISSES_RECONNECT:
            self.misses_in_row, self.key_ok, self.reconnect_why = 0, False, "missed fills"

    async def _watch(self) -> None:
        stats_at = self.clock()
        while True:
            await asyncio.sleep(WAKE_S)
            now = self.clock()
            if self.connected and now >= self.next_keepalive:
                await self._keepalive()
            self._judge(now)
            await self._health(now)
            if now - stats_at >= STATS_S:
                stats_at = now
                self._persist()

    async def _health(self, now: float) -> None:
        mins = (now - self.down_since) / 60
        if self.up:
            if self.alerted:
                await send_safe(self.n, "user_stream_up", f"user-data stream back after {mins:.0f} min: fills and "
                                "stop-outs are seen at once again", cooldown_s=0)
            self.down_since, self.alerted = now, False
            return
        if not self.alerted and now - self.down_since >= DOWN_ALERT_S:
            self.alerted = True
            await send_safe(self.n, "user_stream_down", f"user-data stream down for {mins:.0f} min (last: "
                            f"{self.last_error or 'no key confirmed'}). Fills and stop-outs are still caught "
                            "by the guardian's polls (fills within 5 s, stops and targets within 60 s); only the "
                            "instant reaction is lost.", cooldown_s=0)

    def _persist(self) -> None:
        snap = {**self.stats, "up": self.up, "lanes": self.extra_stats(),
                "at": datetime.now(UTC).isoformat(timespec="seconds")}
        self.j.post(lambda j: j.set(j.mode_key(STATS_KEY), json.dumps(snap, default=str)))   # stats only: posted
