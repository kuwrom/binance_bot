"""Account and market gates on RESTING limit entries (#12). A resting entry is a trade that hasn't happened yet: it can
fill into a halt the governor called after it was placed, or into a market pause (BTC/ETH +0.8% in 15 min, a breadth
squeeze), when the alt catch-up fills several correlated shorts at once. New entries are refused in both; the
guardian's 5 s pass withdraws the resting ones too. Cancel, not freeze: Binance can't suspend an order, and an entry
re-proposed afterwards is re-checked and re-sized at the prices and budget of then.

- governor halt: the monitor lane's verdict (guardian check_governor, every 5 min) under the EFFECTIVE limits, the
  ones the session sizes and places under (as the kill-switch loss trips), so placing and withdrawing never fight.
  Ignored once older than MAX_AGE_S or past its `until`
- market pause: the cached market map, at most MAX_AGE_S old, so a stopped guardian never causes a cancel
- a row whose placement explicitly overrode that pause (veto_override while it was on: overrides.pause_overridden)
  rests on through it; nothing overrides the governor
- resize (#66): each entry is stamped with the budget multipliers it was sized under (STAMPED). A row is withdrawn
  when one is smaller now (the governor's m_gov halved by a drawdown tier or a loss streak; its setup's ladder went to
  HALF; a NULL stamp, from an older server, skips that comparison), when its setup's ladder is SHADOW, or when its
  risk (a split entry's resting legs together) exceeds the verdict's daily loss-cap room minus open risk. A source
  judges only rows placed before it was read: a newer row was sized on fresher numbers
Rows are withdrawn by killswitch_ops.cancel_resting: only an unfilled limit closes its row ("governor halt: ..." /
"market pause: ..." / "governor resized: ..." / "setup ladder: ..."), a fill is the poll's to adopt. The kill
switch's HALTED sweep is separate (guardian_positions).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

from .market.market_map import cached

log = logging.getLogger("shortbot.entries")
GOV_KEY = "governor_verdict"   # journal kv, per mode
MAX_AGE_S = 900
SWEEP_TIMEOUT_S = 60           # a hung cancel must not hold up the fill poll behind it
RESTING = "status IN ('pending','placing','unknown')"   # the rows killswitch_ops.cancel_resting withdraws
STAMPED = ("m_gov", "m_ladder")   # budget multipliers stamped on each entry (entries_place, a lowering amend)
LADDER_TTL_S = 60                 # the sweep re-reads a setup's ladder at most this often (it walks the closed trades)


def stamps(multipliers: dict[str, Any] | None) -> dict[str, float | None]:
    """The STAMPED multipliers of a risk budget; None (unknown) for any it lacks."""
    return {k: (multipliers or {}).get(k) for k in STAMPED}


def publish_governor(j, gov: dict[str, Any], limits: str, now: float | None = None) -> None:
    """The guardian's governor verdict for the protective lane; `limits` names the limits it was judged under."""
    j.set(j.mode_key(GOV_KEY), json.dumps({"ts": time.time() if now is None else now, "halt": bool(gov.get("halt")),
                                           "until": gov.get("until"), "reasons": list(gov.get("reasons") or []),
                                           "limits": limits, "m_gov": gov.get("m_gov"),
                                           "room_usd": (gov.get("daily") or {}).get("room_usd"),
                                           "open_risk_usd": gov.get("open_risk_usd")}))


def _verdict(j, now: float | None = None) -> dict[str, Any] | None:
    """The published verdict while fresh (at most MAX_AGE_S old); None when stale or unreadable."""
    try:
        v = json.loads(j.get(j.mode_key(GOV_KEY)) or "{}")
        return v if (time.time() if now is None else now) - float(v["ts"]) <= MAX_AGE_S else None
    except (ValueError, KeyError, TypeError):
        return None


def governor_halt(j, now: float | None = None) -> list[str]:
    """The governor's reasons while it halts new entries; [] when it doesn't, or its verdict is stale or ran out."""
    v = _verdict(j, now)
    if not v or not v.get("halt"):
        return []
    try:
        if v.get("until") and datetime.fromisoformat(v["until"]).timestamp() <= (time.time() if now is None else now):
            return []
    except (ValueError, TypeError):   # unreadable: no verdict
        return []
    return list(v.get("reasons") or ["halted"])


def market_pause(j) -> list[str]:
    """The market-wide pause vetoes of a fresh market map; [] when there are none or the map is stale."""
    return list(((cached(j, MAX_AGE_S) or {}).get("vetoes") or {}).get("pause") or [])


def overrode_pause(e: dict[str, Any]) -> bool:
    try:
        return bool(json.loads(e.get("overrides") or "{}").get("pause_overridden"))
    except (ValueError, AttributeError):
        return False


def _placed_s(e: dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(e["created_at"]).timestamp()
    except (ValueError, KeyError, TypeError):
        return float("inf")   # unknown: no source can tell it was placed before it


def _shrunk(e: dict[str, Any], key: str, now: float | None) -> bool:
    """Sized under a bigger `key` multiplier than now. A NULL stamp (an older server placed it) is unknown: never."""
    return e.get(key) is not None and now is not None and now < e[key] - 1e-9


def _risk(e: dict[str, Any]) -> float:
    return float(e.get("amount") or 0) * max(0.0, float(e.get("stop_price") or 0) - float(e.get("limit_price") or 0))


def resized(j, rows: list[dict[str, Any]], ladder_of, now: float | None = None) -> dict[int, str]:
    """{id: reason} for the resting rows whose size no longer fits (module doc, "resize"). `ladder_of(setup)` ->
    (read at, setup_ladder.status()); each source judges only the rows placed before it was read."""
    out: dict[int, str] = {}
    v = _verdict(j, now)
    old = [e for e in rows if v and _placed_s(e) + 1 <= float(v["ts"])]
    why = "; ".join((v or {}).get("reasons") or [])
    for e in old:
        if _shrunk(e, "m_gov", v.get("m_gov")):
            out[e["id"]] = f"governor resized: size x{v['m_gov']:g} now, placed at x{e['m_gov']:g} ({why})"
    if v and v.get("room_usd") is not None and v.get("open_risk_usd") is not None:
        room, held = float(v["room_usd"]), float(v["open_risk_usd"])
        units: dict[str, list[dict[str, Any]]] = {}   # a split entry's legs can all fill: one budget, one unit
        for e in old:
            units.setdefault(e.get("tranche") or f"single{e['id']}", []).append(e)
        for unit in units.values():
            if sum(_risk(e) for e in unit) > room - held + 1e-9:
                for e in unit:
                    out.setdefault(e["id"], f"governor resized: risk over the daily-cap room left (room {room:.2f} - "
                                            f"open risk {held:.2f} = {room - held:.2f} USDT)")
    for setup in sorted({e["setup"] for e in rows if e.get("setup") and e["id"] not in out}):
        at, lad = ladder_of(setup)
        state, mult = (lad or {}).get("state"), (lad or {}).get("mult")
        if state not in ("HALF", "SHADOW"):   # NORMAL can't be below a stamp; UNKNOWN never withdraws
            continue
        tail = "re-propose" if state == "HALF" else "no new entries for this setup until it is re-enabled"
        why = f"setup ladder: {state} ({'; '.join(lad.get('reasons') or [])}): {tail}"
        for e in rows:
            if e.get("setup") == setup and e["id"] not in out and _placed_s(e) + 1 <= at and (
                    state == "SHADOW" or _shrunk(e, "m_ladder", mult)):
                out[e["id"]] = why
    return out


def _ladders(desk):
    """ladder_of for `resized`: risk_budget._ladder (never raises), cached on the desk for LADDER_TTL_S."""
    from .risk_budget import _ladder
    seen = desk.__dict__.setdefault("_ladders", {})

    def ladder_of(setup: str) -> tuple[float, dict[str, Any] | None]:
        if setup not in seen or time.time() - seen[setup][0] > LADDER_TTL_S:
            seen[setup] = (time.time(), _ladder(desk.j, setup))
        return seen[setup]
    return ladder_of


async def sweep(desk) -> list[dict[str, Any]]:
    """Withdraw this desk's resting entries that must not fill as they are: all of them in a governor halt; else those
    a governor resize or their setup's ladder shrank, and in a market pause every one that didn't override it. One
    result per reason; [] when nothing is to go."""
    rows = desk._rows(RESTING)
    if not rows:   # nothing rests: don't parse the market map every 5 s
        return []
    gov, pause = governor_halt(desk.j), market_pause(desk.j)
    if gov:
        why = dict.fromkeys((e["id"] for e in rows), f"governor halt: {'; '.join(gov)}")
    else:
        why = resized(desk.j, rows, _ladders(desk))
        why.update({e["id"]: f"market pause: {'; '.join(pause)}" for e in rows if pause and not overrode_pause(e)})
    from .killswitch_ops import cancel_resting   # (it imports entries, which imports this module's users)
    out = []
    for reason in dict.fromkeys(why.values()):
        ids = {i for i, w in why.items() if w == reason}
        out.append({"reason": reason, **await cancel_resting(desk, reason, keep=lambda e, ids=ids: e["id"] not in ids)})
    return out


async def guardian_sweep(desk, n) -> None:
    """The guardian's hook (protective lane, every pass while not HALTED). Never fails the fill poll behind it."""
    from .guardian_loop import send_safe
    try:
        for r in await asyncio.wait_for(sweep(desk), SWEEP_TIMEOUT_S):
            if not r["cancelled"]:   # once per withdrawal (a cancel that keeps failing is retried, not re-alerted)
                continue
            then = "once it lifts" if r["reason"].startswith(("governor halt", "market pause")) else \
                "at the current budget, if it allows one"
            await send_safe(n, "entries_withdrawn", f"{r['reason']}: withdrew resting entries {r['cancelled']}" + (
                f"; left for the fill poll {r['left_for_fill_poll']}" if r["left_for_fill_poll"] else "")
                + f". Re-propose {then}.", cooldown_s=0)
    except Exception as e:  # noqa: BLE001 (incl. TimeoutError) - retried next pass
        log.exception("entry gate sweep failed")
        await send_safe(n, "guardian_error:entry_gates", f"resting-entry sweep: {type(e).__name__}: {e}",
                        urgent=True, cooldown_s=1800)
