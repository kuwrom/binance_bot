"""The probes that hold a minimum short (#55; runner and rules: probes.py). Each opens through the desk's _market_sell
with a closePosition safety stop at once (Probe.open_short), measures one thing, and leaves the cleanup to flatten.
Each returns {"observed", "verdict", "matches_fake"}: matches_fake compares Binance's answer with what tests/fakes.py
assumes (the assumption is quoted in probes.CASES)."""

from __future__ import annotations

import math
from typing import Any

from . import errors
from . import probe_kit as kit
from .probe_kit import TRIG, Probe, algo_status, outcome, say

TRIGGER_WAIT_S = 24     # probe 3: how long the near stop gets to trigger (polled every 2 s)
ADL_POLLS, ADL_EVERY_S = 7, 5.0   # probe 13: adlQuantile "values update every 30 s" (its doc notes)


def _n_close(listed: list[dict] | None) -> int | None:
    return None if listed is None else sum(1 for o in listed if str((o.get("info") or {}).get(
        "closePosition", "")).lower() == "true")


def _refused(ok: bool, res: Any, code: int) -> bool:
    return not ok and errors.code(res) == code


async def second_close_stop(p: Probe) -> dict[str, Any]:
    """1: a second closePosition STOP_MARKET on the same symbol and side, flat and with a position: -4130 or accepted?"""
    await p.setup()
    await p.prepare()
    oka, a, _ = await p.stop("flat: closePosition stop A +10%", p.px(10))
    okb, b, _ = await p.stop("flat: closePosition stop B +11%", p.px(11))
    flat_listed = await p.algo_open("flat: open conditional orders")
    for ok, o, tag in ((oka, a, "A"), (okb, b, "B")):
        if ok:
            await p.cancel(f"flat: cancel stop {tag}", str(o["id"]))
    await p.open_short()   # its safety stop is the first closePosition stop
    okc, c, _ = await p.stop("position: closePosition stop B +11%", p.px(11))
    held_listed = await p.algo_open("position: open conditional orders")
    obs = {"flat": {"first": outcome(oka, a), "second": outcome(okb, b), "closePosition_listed": _n_close(flat_listed)},
           "position": {"second": outcome(okc, c), "closePosition_listed": _n_close(held_listed)}}
    match = _refused(oka, a, -4509) and _refused(okb, b, -4509) and _refused(okc, c, -4130)
    return {"observed": obs, "matches_fake": match,
            "verdict": f"a second closePosition stop, flat: {say(okb, b)}; with a position: {say(okc, c)}"}


async def close_stop_at_flat(p: Probe) -> dict[str, Any]:
    """2: does a closePosition stop leave when its position goes flat (a reduce-only market close)?"""
    await p.setup()
    size = await p.open_short()
    if p.safety is None:
        raise kit.ProbeError("the safety stop's id is unknown: nothing to watch")
    await p.need("close: reduce-only market BUY (desk _market_close)", p.tr._market_close, p.sym, size)
    st = (await p.watch_algo({"closePosition": p.safety}))["closePosition"]
    gone = st["gone_after_s"] is not None
    when = f"left the open list within {st['gone_after_s']:g} s" if gone else "still rests 15.5 s"
    return {"observed": st, "matches_fake": gone and st["status"] == "EXPIRED",
            "verdict": f"the closePosition stop {when} after flat; algoStatus {st['status'] or 'unknown'}"}


async def reduce_only_stop_at_flat(p: Probe) -> dict[str, Any]:
    """12b: does a reduce-only STOP_MARKET (a pre-arm's shape, not closePosition) get cancelled at flat?"""
    await p.setup()
    size = await p.open_short()
    ok, ro, _ = await p.stop("reduce-only stop for the position +9% (pre-arm shape)", p.px(9), close=False, qty=size)
    if not ok:
        raise kit.ProbeError(f"the reduce-only stop was refused: {say(ok, ro)}")
    await p.need("close: reduce-only market BUY (desk _market_close)", p.tr._market_close, p.sym, size)
    ids = {"reduce_only": str(ro["id"]), **({"closePosition": p.safety} if p.safety else {})}
    w = await p.watch_algo(ids)
    r = w["reduce_only"]
    stays = r["gone_after_s"] is None
    when = "still rests 15.5 s" if stays else f"left the open list within {r['gone_after_s']:g} s"
    cp = w.get("closePosition") or {}
    return {"observed": w, "matches_fake": stays and r["status"] == "NEW",
            "verdict": f"the reduce-only stop {when} after flat (algoStatus {r['status'] or 'unknown'}); the "
                       f"closePosition stop: {cp.get('status') or 'n/a'}"}


async def _near_stop(p: Probe, size: float) -> str | None:
    """A reduce-only stop for the position 1 (then 2, 4) ticks above the mark: it triggers on the next uptick."""
    for k in (1, 2, 4):
        mark = await p.mark()
        px = float(p.t.price_to_precision(p.sym, math.ceil(round(mark / p.tick, 6)) * p.tick + k * p.tick))
        ok, o, _ = await p.stop(f"near reduce-only stop at mark + {k} tick(s)", px, close=False, qty=size)
        if ok:
            return str(o["id"])
        if errors.classify(o) is not errors.WouldTrigger:
            return None
    return None


async def cancel_finished(p: Probe) -> dict[str, Any]:
    """3: cancelling a conditional order that finished (triggered), expired (a closePosition stop at flat) or was
    cancelled already: -2011, or something else?"""
    await p.setup()
    size = await p.open_short()
    near, status, cancels = await _near_stop(p, size), "", {}
    waited = 0
    while near and waited < TRIGGER_WAIT_S:
        await kit._sleep(2)
        waited += 2
        ok, q = await p.call(f"query the near stop ({waited} s)", p.t.fetch_order, near, p.sym, TRIG)
        status = algo_status(q) if ok else ""
        if status and status not in ("NEW", "TRIGGERING"):
            break
    triggered = status in ("TRIGGERED", "FINISHED")
    if triggered:
        cancels["finished"] = {**await p.cancel(f"cancel the {status} near stop", near), "status_before": status}
    elif near:
        cancels["canceled_first"] = await p.cancel("cancel the resting near stop", near)
        cancels["canceled_repeat"] = {**await p.cancel("cancel it again", near), "status_before": "CANCELED"}
    if pos := await p.position("position after the near stop"):
        await p.need("close: reduce-only market BUY (desk _market_close)", p.tr._market_close, p.sym,
                     float(pos["contracts"]))
    if p.safety:
        st = (await p.watch_algo({"closePosition": p.safety}, delays=(0.5, 1, 2, 2)))["closePosition"]["status"]
        cancels["after_flat"] = {**await p.cancel(f"cancel the closePosition stop after flat ({st})", p.safety),
                                 "status_before": st}
        cancels["after_flat_repeat"] = {**await p.cancel("cancel it again", p.safety), "status_before": "after a cancel"}
    dead = [v for k, v in cancels.items() if k != "canceled_first" and v.get("status_before") not in ("NEW", "")]
    match = bool(dead) and all(not v["ok"] and v["code"] == -2011 for v in dead)
    words = "; ".join(f"{k} ({v.get('status_before', 'NEW')}): {'cancelled' if v['ok'] else v['error']} "
                      f"{v.get('code') or ''}".strip() for k, v in cancels.items())
    return {"observed": {"triggered": triggered, "near_status": status or None, "cancels": cancels},
            "matches_fake": match, "verdict": f"near stop {'triggered' if triggered else 'did not trigger'}; {words}"}


async def reduce_only_oversize(p: Probe) -> dict[str, Any]:
    """4: a reduce-only order for twice the position, as a resting LIMIT and as a MARKET order: capped or rejected?"""
    await p.setup()
    size = await p.open_short()
    big = float(p.t.amount_to_precision(p.sym, 2 * size))
    oka, a, _ = await p.limit("reduce-only LIMIT BUY 2x the position at -2%", "buy", p.px(-2), big, reduce=True)
    lim = outcome(oka, a)
    if oka:
        okq, q = await p.call("query the reduce-only limit", p.t.fetch_order, str(a["id"]), p.sym)
        lim.update(amount=q.get("amount") if okq else None, status=q.get("status") if okq else None)
        await p.cancel("cancel the reduce-only limit", str(a["id"]), algo=False)
    okm, m, _ = await p.order("reduce-only MARKET BUY 2x the position", "market", "buy", big, None, {"reduceOnly": True})
    pos = await p.position("position after the market order")
    left = float(pos["contracts"]) if pos else 0.0
    mkt = {**outcome(okm, m), "filled": m.get("filled") if okm else None, "position_after": left}
    capped = okm and left == 0
    lim_capped = oka and lim.get("amount") is not None and abs(float(lim["amount"]) - size) <= 1e-9
    return {"observed": {"size": size, "asked": big, "limit": lim, "market": mkt},
            "matches_fake": bool(lim_capped and capped),
            "verdict": f"reduce-only LIMIT 2x: {say(oka, a)}" + (f" (rests for {lim.get('amount')})" if oka else "")
                       + "; reduce-only MARKET 2x: " + ("capped, closed the position" if capped else
                                                        f"{say(okm, m)}, position {left:g} left")}


async def adl_quantile(p: Probe) -> dict[str, Any]:
    """13: GET /fapi/v1/adlQuantile with a probe position open, with and without `symbol` (raw JSON)."""
    await p.setup()
    await p.open_short()
    one = every = None
    ok1 = listed = False
    polls = 0
    for i in range(ADL_POLLS):
        if i:
            await kit._sleep(ADL_EVERY_S)
        polls = i + 1
        ok1, one = await p.call("adlQuantile ?symbol=", p.t.fapiPrivateGetAdlQuantile, {"symbol": p.raw})
        ok2, every = await p.call("adlQuantile (every symbol)", p.t.fapiPrivateGetAdlQuantile, {})
        listed = ok2 and isinstance(every, list) and any(isinstance(r, dict) and r.get("symbol") == p.raw for r in every)
        if listed:
            break
    pos = await p.position("positionRisk v3 (its adl field)")
    shape = ok1 and isinstance(one, dict) and isinstance(one.get("adlQuantile"), dict)
    obs = {"with_symbol": kit.raw(one) if ok1 else outcome(False, one), "every_symbol": kit.raw(every),
           "listed_after_polls": polls if listed else None, "positionRisk_adl": ((pos or {}).get("info") or {}).get("adl")}
    return {"observed": obs, "matches_fake": bool(shape and listed),
            "verdict": f"?symbol= answers {'one object with adlQuantile' if shape else type(one).__name__}; the "
                       f"all-symbols list {'shows' if listed else 'never showed'} {p.raw}"
                       + (f" (poll {polls})" if listed else f" in {ADL_POLLS} polls")}
