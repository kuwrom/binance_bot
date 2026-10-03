"""The probes that never hold a position (#55; runner and rules: probes.py; the user-data stream probe: probe_stream.py).
Orders here rest far from the market (a SELL limit 2% above, BUY stops 10% above) for seconds and are cancelled. Each
returns {"observed", "verdict", "matches_fake"} (the fake's assumption is quoted in probes.CASES)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import ccxt.async_support as ccxt

from . import errors
from . import probe_kit as kit
from .probe_kit import TRIG, Probe, algo_status, outcome, say

UNKNOWN_ALGO_ID = 9_999_999_999_999   # an algoId this account never had
COUNTDOWN_MS, COUNTDOWN_WAIT_S = 3000, 6.0   # the doc advises against "too precise or too small" countdowns


def _answer(ok: bool, res: Any) -> dict[str, Any]:
    return {**outcome(ok, res), **({"answer": kit.raw(res)} if ok else {})}


def _not_found(ok: bool, res: Any, code: int) -> bool:
    return not ok and isinstance(res, ccxt.OrderNotFound) and errors.code(res) == code


async def algo_unknown_cid(p: Probe) -> dict[str, Any]:
    """11: what Query-Algo-Order answers for a clientAlgoId (and an algoId) that never existed. Read only."""
    cid = p.cid()   # never sent
    ok1, r1 = await p.call("fetch_order trigger, unknown clientAlgoId", p.t.fetch_order, None, p.sym,
                           {"clientOrderId": cid, **TRIG})
    ok2, r2 = await p.call("raw GET /fapi/v1/algoOrder, unknown clientAlgoId", p.t.fapiPrivateGetAlgoOrder,
                           {"clientAlgoId": cid})
    ok3, r3 = await p.call("raw GET /fapi/v1/algoOrder, unknown algoId", p.t.fapiPrivateGetAlgoOrder,
                           {"algoId": UNKNOWN_ALGO_ID})
    return {"observed": {"fetch_order": _answer(ok1, r1), "raw_client_algo_id": _answer(ok2, r2),
                         "raw_algo_id": _answer(ok3, r3)},
            "matches_fake": _not_found(ok1, r1, -2013),
            "verdict": f"unknown clientAlgoId: {say(ok1, r1)} (raw: {say(ok2, r2)}); unknown algoId: {say(ok3, r3)}"}


async def trigger_listing_cid(p: Probe) -> dict[str, Any]:
    """8: does fetch_open_orders(sym, {'trigger': True}) carry our clientOrderId on a conditional order, and does the
    lookup by clientAlgoId find it (Trader.find_stop relies on both)?"""
    await p.setup()
    ok, o, cid = await p.stop("reduce-only stop +10% (pre-arm shape)", p.px(10), close=False, qty=p.qty)
    if not ok:
        return {"observed": {"stop": outcome(ok, o)}, "matches_fake": None, "verdict": f"stop refused: {say(ok, o)}"}
    oid = str(o["id"])
    mine = next((x for x in await p.algo_open("open conditional orders") or [] if str(x.get("id")) == oid), None)
    okq, q = await p.call("fetch_order trigger by clientAlgoId", p.t.fetch_order, None, p.sym,
                          {"clientOrderId": cid, **TRIG})
    await p.cancel("cancel the stop", oid)
    listed_cid = (mine or {}).get("clientOrderId")
    found = okq and str(q.get("id")) == oid and q.get("clientOrderId") == cid
    obs = {"listed": mine is not None, "listed_clientOrderId": listed_cid,
           "listed_info_clientAlgoId": ((mine or {}).get("info") or {}).get("clientAlgoId"), "lookup_by_cid": found,
           "lookup": _answer(okq, q)}
    return {"observed": obs, "matches_fake": listed_cid == cid and found,
            "verdict": f"the trigger listing carries our client id: {listed_cid == cid}; the lookup by clientAlgoId "
                       f"finds it: {found}"}


async def fetch_by_cid(p: Probe) -> dict[str, Any]:
    """9: does fetch_order(None, sym, {'clientOrderId': cid}) find a resting limit (then the cancelled one), and what
    does an unknown client id answer (clientid.lookup's None)?"""
    await p.setup()
    await p.prepare()
    ok, o, cid = await p.limit("limit SELL +2%", "sell", p.px(2), p.qty)
    if not ok:
        return {"observed": {"limit": outcome(ok, o)}, "matches_fake": None, "verdict": f"limit refused: {say(ok, o)}"}
    oid = str(o["id"])
    ok1, f1 = await p.call("fetch_order by clientOrderId (resting)", p.t.fetch_order, None, p.sym, {"clientOrderId": cid})
    await p.cancel("cancel the limit", oid, algo=False)
    ok2, f2 = await p.call("fetch_order by clientOrderId (cancelled)", p.t.fetch_order, None, p.sym,
                           {"clientOrderId": cid})
    ok3, f3 = await p.call("fetch_order by an unknown clientOrderId", p.t.fetch_order, None, p.sym,
                           {"clientOrderId": p.cid()})
    resting = ok1 and str(f1.get("id")) == oid and f1.get("status") == "open"
    cancelled = ok2 and str(f2.get("id")) == oid and f2.get("status") == "canceled"
    obs = {"resting": _answer(ok1, f1), "cancelled": _answer(ok2, f2), "unknown": _answer(ok3, f3)}
    return {"observed": obs, "matches_fake": resting and cancelled and _not_found(ok3, f3, -2013),
            "verdict": f"found resting: {resting}; found cancelled: {cancelled}; an unknown client id: {say(ok3, f3)}"}


async def countdown_cancel_all(p: Probe) -> dict[str, Any]:
    """6: does POST /fapi/v1/countdownCancelAll cancel conditional (algo) orders too, or regular ones only? Flat: a
    SELL limit, a closePosition stop and a reduce-only stop rest; the countdown runs out; the timer is then turned off
    (the cleanup turns it off again if this didn't)."""
    await p.setup()
    await p.prepare()
    okl, lim, _ = await p.limit("limit SELL +2%", "sell", p.px(2), p.qty)
    okc, cps, _ = await p.stop("closePosition stop +10% (flat)", p.px(10))
    okr, ro, _ = await p.stop("reduce-only stop +10%", p.px(10), close=False, qty=p.qty)
    p.countdown = True
    await p.need(f"countdownCancelAll {COUNTDOWN_MS} ms", p.t.fapiPrivatePostCountdownCancelAll,
                 {"symbol": p.raw, "countdownTime": COUNTDOWN_MS})
    await kit._sleep(COUNTDOWN_WAIT_S)
    okg, regular = await p.call("open orders after the countdown", p.t.fetch_open_orders, p.sym)
    algo = await p.algo_open("open conditional orders after the countdown")
    ok0, _ = await p.call("countdownCancelAll 0 (timer off)", p.t.fapiPrivatePostCountdownCancelAll,
                          {"symbol": p.raw, "countdownTime": 0})
    p.countdown = not ok0
    left_reg = {str(o.get("id")) for o in regular} if okg else None
    left_algo = {str(o.get("id")) for o in algo} if algo is not None else None

    def state(ok: bool, o: Any, left: set[str] | None) -> str | None:
        return None if not ok or left is None else "still rests" if str(o["id"]) in left else "cancelled"
    obs = {"limit": state(okl, lim, left_reg), "closePosition_stop": state(okc, cps, left_algo),
           "reduce_only_stop": state(okr, ro, left_algo)}
    for k, ok, o in (("closePosition_stop", okc, cps), ("reduce_only_stop", okr, ro)):
        if ok:
            okq, q = await p.call(f"query the {k}", p.t.fetch_order, str(o["id"]), p.sym, TRIG)
            obs[f"{k}_status"] = algo_status(q) if okq else say(okq, q)
    algos = [v for k, v in obs.items() if k in ("closePosition_stop", "reduce_only_stop") and v is not None]
    match = None if obs["limit"] is None or not algos else obs["limit"] == "cancelled" and all(
        v == "cancelled" for v in algos)
    return {"observed": obs, "matches_fake": match,
            "verdict": f"the countdown cancelled the limit: {obs['limit']}; closePosition stop: "
                       f"{obs['closePosition_stop']}; reduce-only stop: {obs['reduce_only_stop']}"}


def _since_ms(p: Probe) -> int:
    """From 5 minutes before the oldest probe row on this symbol in the last 24 h (a row is written when its probe
    ends), else the last hour."""
    day = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    rows = p.tr.j._rows("SELECT ts FROM decisions WHERE action='probe_result' AND symbol=? AND ts >= ? ORDER BY ts "
                        "LIMIT 1", (p.sym, day))
    at = datetime.fromisoformat(rows[0]["ts"]) - timedelta(minutes=5) if rows else \
        datetime.now(timezone.utc) - timedelta(hours=1)
    return int(at.timestamp() * 1000)


async def income_timestamps(p: Probe) -> dict[str, Any]:
    """10: what time do COMMISSION / REALIZED_PNL income rows carry against their trade's (matched by tradeId)? Read
    only: the trades are the earlier probes' (or any on the symbol in the window)."""
    since = _since_ms(p)
    trades = await p.need("userTrades since the probes", p.t.fetch_my_trades, p.sym, since)
    inc = await p.need("income since the probes", p.t.fapiPrivateGetIncome,
                       {"symbol": p.raw, "startTime": since, "limit": 1000})
    if not trades:
        return {"observed": {"since_ms": since, "trades": 0}, "matches_fake": None,
                "verdict": f"no trades on {p.sym} since {since}: run a trading probe first"}
    times = {str(t["id"]): int(t["timestamp"]) for t in trades}
    rows = [r for r in inc if r.get("incomeType") in ("COMMISSION", "REALIZED_PNL")]
    pairs = [{"type": r["incomeType"], "tradeId": str(r.get("tradeId")), "income_time": int(r["time"]),
              "trade_time": times[str(r.get("tradeId"))], "delta_ms": int(r["time"]) - times[str(r.get("tradeId"))]}
             for r in rows if str(r.get("tradeId")) in times]
    unmatched = [r for r in rows if str(r.get("tradeId")) not in times]
    deltas = [x["delta_ms"] for x in pairs]
    obs = {"since_ms": since, "trades": len(trades), "income_rows": len(rows), "pairs": pairs,
           "unmatched": [kit.raw(r) for r in unmatched[:10]]}
    spread = f"delta ms min {min(deltas)} max {max(deltas)}" if deltas else "no deltas"
    floored = all(-1000 < x["delta_ms"] <= 0 and x["income_time"] % 1000 == 0 for x in pairs)
    return {"observed": obs, "matches_fake": bool(pairs) and not unmatched and floored,
            "verdict": f"{len(pairs)} of {len(rows)} income rows matched their trade by tradeId: {spread}"}
