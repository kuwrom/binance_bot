"""Probe 7 (#55; runner: probes.py): do user-data events (ORDER_TRADE_UPDATE, ALGO_UPDATE) reach a DEMO account's
stream, and on which URL? It informed the guardian's user-data stream (user_stream.py, #54).

Where a demo account's user stream lives is unsettled:
- ccxt 4.5.84 pro (installed) puts demo futures on wss://fstream.binancefuture.com/ws (the testnet host), and a user
  stream at <that>/private/ws?listenKey=K (pro/binance.py get_private_ws_url)
- Binance's General Info page (developers.binance.com/docs/derivatives/usds-margined-futures/general-info) gives the
  demo WebSocket host as wss://demo-fstream.binance.com; the change log (2026-03-05) moved user-data events to the
  `/private` path. binance-connector-python's constants list no demo WebSocket host at all.
So one listenKey (POST /fapi/v1/listenKey on the demo host, weight 1) is tried on three URLs at once while the probe
places and cancels a far limit (ORDER_TRADE_UPDATE) and a far reduce-only stop (ALGO_UPDATE). The key is never
DELETEd: Binance keeps one per account ("If the account has an active listenKey, that listenKey will be returned"), so
a delete would close any other stream on the account. It is never journalled or printed either (masked).
"connected" alone proves nothing: a made-up key connects to demo-fstream's /private path too (measured 2026-10-02);
the events decide."""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from typing import Any

from . import probe_kit as kit
from .probe_kit import Probe, mask

DOC_DEMO = "wss://demo-fstream.binance.com"
CONNECT_S, EVENTS_KEPT = 15.0, 8   # measured 2026-10-02: a public connect + first frame took 3-8 s


def urls(key: str) -> dict[str, str]:
    import ccxt.pro as ccxtpro   # only for the URL ccxt would use: no connection, nothing to close
    pro = ccxtpro.binanceusdm()
    pro.enable_demo_trading(True)
    return {"ccxt_pro_demo": pro.get_private_ws_url("future", key),
            "doc_demo_private": f"{DOC_DEMO}/private/ws?listenKey={key}",
            "doc_demo_legacy": f"{DOC_DEMO}/ws/{key}"}


async def listen(urls: dict[str, str], done: asyncio.Event, settled: set[str] | None = None,
                 timeout_s: float = CONNECT_S) -> dict[str, dict]:
    """Each URL: connect (its name joins `settled` once the attempt ended either way), then keep every text frame (the
    raw event + `_at_ms`, when it arrived) until `done`. TLS through certifi's CA bundle, as ccxt's own sessions: this
    Python's default store can't verify Binance's certificate."""
    import aiohttp
    import certifi
    tls = ssl.create_default_context(cafile=certifi.where())
    settled = set() if settled is None else settled

    async def one(name: str, url: str) -> dict[str, Any]:
        got: dict[str, Any] = {"connected": False, "error": None, "events": []}
        try:
            async with aiohttp.ClientSession() as s:
                try:
                    ws = await asyncio.wait_for(s.ws_connect(url, ssl=tls), timeout_s)
                finally:
                    settled.add(name)
                got["connected"] = True
                async with ws:
                    while not done.is_set():
                        try:
                            msg = await ws.receive(timeout=0.5)
                        except TimeoutError:
                            continue
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            d = json.loads(msg.data)
                            got["events"].append({**(d if isinstance(d, dict) else {"frame": d}),
                                                  "_at_ms": int(time.time() * 1000)})
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            got["error"] = f"closed by the server: {msg.type.name} {ws.close_code}"
                            break
        except Exception as e:  # noqa: BLE001 - the failure is the finding
            got["error"] = f"{type(e).__name__}: {e}"[:300]
        settled.add(name)
        return got
    names = list(urls)
    return dict(zip(names, await asyncio.gather(*(one(k, urls[k]) for k in names))))


async def user_stream(p: Probe) -> dict[str, Any]:
    await p.setup()
    await p.prepare()
    lk = await p.need("POST /fapi/v1/listenKey (never deleted: one key per account)", p.t.fapiPrivatePostListenKey,
                      secret=("listenKey",))
    key = str(lk["listenKey"])
    done, sent, settled, where = asyncio.Event(), [], set(), urls(key)
    task = asyncio.create_task(listen(where, done, settled))
    try:
        waited = 0.0
        while len(settled) < len(where) and waited < CONNECT_S + 1:   # every socket connected or gave up
            await kit._sleep(0.5)
            waited += 0.5
        await kit._sleep(1)
        ok, o, cid = await p.limit("limit SELL +2% (ORDER_TRADE_UPDATE NEW)", "sell", p.px(2), p.qty)
        sent.append(cid)
        await kit._sleep(2)
        if ok:
            await p.cancel("cancel the limit (ORDER_TRADE_UPDATE CANCELED)", str(o["id"]), algo=False)
        await kit._sleep(2)
        ok, s, cid = await p.stop("reduce-only stop +10% (ALGO_UPDATE NEW)", p.px(10), close=False, qty=p.qty)
        sent.append(cid)
        await kit._sleep(2)
        if ok:
            await p.cancel("cancel the stop (ALGO_UPDATE CANCELED)", str(s["id"]))
        await kit._sleep(3)
    finally:
        done.set()
        got = await task
    obs = {}
    for name, g in got.items():
        evs = g.get("events") or []
        obs[name] = {"connected": g.get("connected"), "error": g.get("error"),
                     "event_types": sorted({str(e.get("e")) for e in evs if e.get("e")}),
                     "events_with_our_ids": sum(1 for e in evs if any(c in json.dumps(e) for c in sent)),
                     "events": evs[:EVENTS_KEPT]}
    obs = json.loads(json.dumps(obs, default=str).replace(key, mask(key)))   # an error may quote the URL
    on = {t: [n for n, v in obs.items() if t in v["event_types"]] for t in ("ORDER_TRADE_UPDATE", "ALGO_UPDATE")}
    return {"observed": obs, "matches_fake": None,
            "verdict": "; ".join(f"{t} on {', '.join(v) or 'no URL'}" for t, v in on.items())
                       + f" (connected: {', '.join(n for n, v in obs.items() if v['connected']) or 'none'})"}
