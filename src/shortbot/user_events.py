"""Binance USDⓈ-M user-data events -> the guardian job that reconciles them (#54; the stream: user_stream.py). Pure.

Formats: the official SDK's models (binance-connector-python, derivatives_trading_usds_futures websocket_streams/models:
order_trade_update_o.py, algo_update_o.py, account_update_a.py, listen_key_expired.py), ccxt 4.5.84 pro/binance.py
(handle_order_update, handle_acount_update docstrings) and demo's own payloads (probe_result 387, 2026-10-02):
- ORDER_TRADE_UPDATE {e, E event ms, T, o: {s, c client id, i order id, x execution type, X status, l, z, ...}}.
  x NEW, CANCELED, CALCULATED, EXPIRED, TRADE, AMENDMENT; X NEW, PARTIALLY_FILLED, FILLED, CANCELED, EXPIRED,
  EXPIRED_IN_MATCH. Client ids Binance gives its own closes: "autoclose-..." a liquidation, "adl_autoclose" an ADL.
- ALGO_UPDATE {e, E, T, o: {caid client algo id, aid, s, X, ai, cp, R, tp, rm, ia}}. X (Binance.Net
  AlgoOrderStatus.cs): NEW placed; CANCELED; TRIGGERING sent to the matching engine; TRIGGERED placed there; FINISHED
  filled or cancelled there; REJECTED (e.g. margin); EXPIRED cancelled by the system (a closePosition stop at flat).
  A triggered algo order fills under a new order, `ai` (actualOrderId, Query-Algo-Order doc). That order's own client
  id is undocumented, so it is matched by `ai`. Change log 2026-08-21: `ia` is live, so a trailing stop may push NEW
  twice; the desk places none, and NEW wakes nothing.
- ACCOUNT_UPDATE {e, E, T, a: {m reason, B, P}}: counted only; the order events wake the handlers.
- listenKeyExpired {e, E, listenKey}: the stream reconnects with a fresh key.

What wakes what (the handler is the job that already reconciles it on schedule, so a missed event costs latency only):
- entries (the fill poll) on our entry's FILLED, CANCELED or EXPIRED. Not on PARTIALLY_FILLED: the poll adopts a
  partial fill and withdraws the rest, so adopting it at the first print would cut positions short, a strategy change;
  partial->full times are measured instead (user_stream.py).
- positions (the stop/plan pass: stop-outs, target settles, restores) on a target's fill, our stops' trigger,
  rejection or expiry, the whole-position stop's cancel (the app may have removed it), and Binance's own closes.
- Nothing on the sessions' own market orders (they journal them), probes, or orders without our prefix (`foreign`:
  logged, quoted by the untracked-position check).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import clientid
from .exchange import to_symbol

ENTRIES, POSITIONS = "entries", "positions"
ENTRY_DONE = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}
STOP_DONE = {"TRIGGERED", "FINISHED", "REJECTED", "EXPIRED"}
STOPS = {"stop", "prearm", "bridge"}
EXCHANGE_CLOSES = {"liquidation", "adl"}
_KIND = {letter: kind for kind, letter in clientid.KINDS.items()}


@dataclass(frozen=True)
class Hit:
    type: str                     # the event type (`e`)
    kind: str                     # entry, target, stop, prearm, bridge, open, close, add, probe, liquidation, adl,
    #                               foreign, account, expired, other
    job: str | None = None        # the guardian job to run now, or None
    symbol: str | None = None
    cid: str | None = None
    status: str | None = None     # X
    exec_type: str | None = None  # x (ORDER_TRADE_UPDATE)
    event_ms: int | None = None   # E
    order_id: str | None = None   # i, or the algo id
    actual_id: str | None = None  # ALGO_UPDATE `ai`: the order a triggered algo order fills under
    side: str | None = None
    qty: str | None = None


def kind_of(cid: Any) -> str:
    c = str(cid or "")
    if clientid.ours(c):
        return _KIND.get(c[len(clientid.PREFIX) + 1:][:1], "ours")
    if c.startswith("autoclose-"):
        return "liquidation"
    if c.startswith("adl_autoclose"):
        return "adl"
    return "foreign"


def _symbol(raw: Any) -> str | None:
    return to_symbol(str(raw)) if raw else None


def _int(x: Any) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _order_job(kind: str, x: str | None, status: str | None) -> str | None:
    if kind == "entry":
        return ENTRIES if status in ENTRY_DONE else None
    if kind in EXCHANGE_CLOSES or (kind in STOPS | {"target"} and x == "TRADE"):
        return POSITIONS
    return None


def _algo_job(kind: str, status: str | None) -> str | None:
    if kind in STOPS and (status in STOP_DONE or (kind == "stop" and status == "CANCELED")):
        return POSITIONS
    return None


def classify(ev: dict, actual: dict[str, str] | None = None) -> Hit:
    """`actual`: order id -> kind, from earlier ALGO_UPDATEs' `ai` (a triggered stop's fill is that stop's)."""
    e, ms = str(ev.get("e") or "?"), _int(ev.get("E"))
    o = ev.get("o") if isinstance(ev.get("o"), dict) else {}
    if e == "ORDER_TRADE_UPDATE" and o:
        oid, cid, x, st = str(o.get("i") or ""), o.get("c"), o.get("x"), o.get("X")
        kind = (actual or {}).get(oid) or kind_of(cid)
        return Hit(e, kind, _order_job(kind, x, st), _symbol(o.get("s")), cid, st, x, ms, oid, None, o.get("S"),
                   o.get("z"))
    if e == "ALGO_UPDATE" and o:
        cid, st = o.get("caid"), o.get("X")
        kind = kind_of(cid)
        return Hit(e, kind, _algo_job(kind, st), _symbol(o.get("s")), cid, st, None, ms, str(o.get("aid") or ""),
                   str(o.get("ai") or "") or None, o.get("S"), o.get("q"))
    kind = {"ACCOUNT_UPDATE": "account", "listenKeyExpired": "expired"}.get(e, "other")
    return Hit(e, kind, event_ms=ms)
