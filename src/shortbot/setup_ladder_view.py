"""Which setups the ladder is holding back (#67; setup_ladder.py): `ladders` for governor_status and the brief,
`listing` and `clear_preview` for `shortbot-admin ladder-status` / `ladder-clear`.

Cheap, because the guardian reads governor_status every pass:
- only setups with a closed trade since their epoch, or a recorded stop not yet re-enabled, are replayed, each through
  risk_budget._ladder (the epoch, roll-up and fail-closed UNKNOWN that size trades, with its 1 h duplicates cache);
- the map is cached for TTL_S, keyed on the closed trades and this account's ladder rows, so a closed trade, a new
  stop record or a clear shows at once.
A read may record a stop seen for the first time (setup_ladder.status), exactly as sizing would: one record, one alert.
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import registry as R
from . import risk_budget
from . import setup_ladder as L

TTL_S = 3600
_cache: dict[tuple, tuple[float, tuple, dict[str, dict[str, Any]]]] = {}   # (journal, mode) -> (ts, fingerprint, map)
CLEAR = "shortbot-admin ladder-clear --setup {setup} --reason '<what you reviewed>'"


def _records(journal) -> dict[str, str]:
    """{setup: raw kv row} of this account's ladders (demo: bare keys; live: the '@live' ones)."""
    tail, out = journal.mode_key(""), {}
    upper = L.KV[:-1] + chr(ord(L.KV[-1]) + 1)   # a key range on the primary key, not a LIKE scan of every kv row
    for k, v in journal.db.execute("SELECT k, v FROM kv WHERE k >= ? AND k < ?", (L.KV, upper)):
        name = k[len(L.KV):] if k.startswith(L.KV) else None
        if name and tail and name.endswith(tail):
            out[name[:-len(tail)]] = v
        elif name and not tail and "@" not in name:
            out[name] = v
    return out


def _unreleased(ev: dict[str, Any]) -> dict[str, Any] | None:
    last = (ev.get("stops") or [None])[-1]
    return last if last is not None and not any(r >= last["at"] for r in ev.get("reenables") or []) else None


def setups(journal, rows: list[dict[str, Any]], recs: dict[str, str]) -> list[str]:
    """The setups worth replaying: a closed trade opened since the setup's epoch, or a stop not yet re-enabled."""
    last_open: dict[str, str] = {}
    for r in rows:
        if r.get("setup") and r.get("r_multiple") is not None:
            last_open[r["setup"]] = max(last_open.get(r["setup"], ""), r.get("opened_at") or "")
    out = {s for s, raw in recs.items() if _unreleased(json.loads(raw))}
    for s, opened in last_open.items():
        reg = (R.latest(journal.db, f"setup.{s}") or {}).get("created_at")
        own = json.loads(recs[s]).get("epoch") if s in recs else None
        if opened >= max(x for x in (reg, own, "") if x is not None):
            out.add(s)
    return sorted(out)


def _fingerprint(journal, rows: list[dict[str, Any]]) -> tuple:
    rs = [r["r_multiple"] for r in rows if r.get("r_multiple") is not None]
    return (len(rows), max((r.get("closed_at") or "" for r in rows), default=""), round(sum(rs), 6),
            tuple(sorted(_records(journal).items())))


def ladders(journal) -> dict[str, dict[str, Any]]:
    """{setup: {state, mult, dd_r, reasons}} for each setup the ladder sizes down: HALF, SHADOW or UNKNOWN."""
    rows = journal.closed_results()
    slot, fp = (id(journal), getattr(journal, "mode", None)), _fingerprint(journal, rows)
    hit = _cache.get(slot)
    if hit and hit[1] == fp and time.time() - hit[0] <= TTL_S:
        return {s: dict(v) for s, v in hit[2].items()}
    out = {}
    for s in setups(journal, rows, _records(journal)):
        st = risk_budget._ladder(journal, s) or {}
        if st.get("state") != "NORMAL":
            out[s] = {"state": st.get("state"), "mult": st.get("mult"), "dd_r": st.get("dd_r"),
                      "reasons": list(st.get("reasons") or [])}
    _cache[slot] = (time.time(), _fingerprint(journal, rows), out)   # after any stop this read recorded
    return {s: dict(v) for s, v in out.items()}


def brief_line(journal) -> str | None:
    """One brief line when any setup is sized down; None when every ladder is NORMAL."""
    lad = ladders(journal)
    if not lad:
        return None
    parts = [f"{s} {v['state']} x{float(v['mult'] or 0):g}" + (f" (DD {v['dd_r']}R)" if v.get("dd_r") is not None
                                                                else "") for s, v in sorted(lad.items())]
    return ("- ⚠️ setup ladder: " + ", ".join(parts) + ": new entries sized down, SHADOW takes none until a "
            "re-enable on the evidence frozen at its stop (the human checks `shortbot-admin ladder-status`)")


def _stop_check(journal, setup: str) -> tuple[dict | None, dict | None, list[dict[str, Any]]]:
    """(the unreleased stop, its frozen criteria's verdict on the shadow episodes since, those episodes)."""
    stop = _unreleased(L.events(journal, setup))
    if stop is None:
        return None, None, []
    eps = [e for e in L.shadow_episodes(journal, setup) if e["day"] >= stop["at"][:10]]
    return stop, L.reenable_check(stop["criteria"], eps), eps


def listing(journal) -> dict[str, dict[str, Any]]:
    """Every ladder worth replaying, fresh (no cache): state, size, drawdown, thresholds, epoch, family, the stop and,
    while a stop is unreleased, the verdict a clear would get now."""
    out = {}
    for s in setups(journal, journal.closed_results(), _records(journal)):
        st = risk_budget._ladder(journal, s) or {}
        item = {k: st.get(k) for k in ("state", "mult", "dd_r", "cum_r", "hwm_r", "thresholds", "epoch", "family",
                                       "stop", "reasons")}
        stop, chk, _ = _stop_check(journal, s)
        if stop:
            item["stop"], item["reenable"] = stop, {**chk, "clear": CLEAR.format(setup=s)}
        out[s] = item
    return out


def clear_preview(journal, setup: str) -> dict[str, Any]:
    """What a clear would evaluate now: the ladder (recording a stop seen for the first time), the kv row it would
    swap (`raw`) and the frozen criteria on the shadow episodes since the stop. ok False: nothing to clear, or the
    evidence fails (`error` says why)."""
    lad = risk_budget._ladder(journal, setup) or {}
    base = {"setup": setup, "state": lad.get("state"), "dd_r": lad.get("dd_r"),
            "raw": journal.get(L.key(journal, setup))}
    stop, chk, eps = _stop_check(journal, setup)
    if stop is None:
        return {**base, "ok": False, "error": f"{setup} is not stopped (ladder {lad.get('state')}): nothing to clear"}
    out = {**base, "stop": stop, "evidence": chk, "episodes": eps}
    if not chk["ok"]:
        return {**out, "ok": False, "error": "frozen re-enable criteria not met: " + "; ".join(chk["fails"])}
    return {**out, "ok": True}
