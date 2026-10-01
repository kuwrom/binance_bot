"""Kill-switch actions (state and gate: killswitch.py): halt = trip + withdraw the resting entries; flatten = HALTED +
market-close every short reduce-only. Neither ever cancels a protective stop on an open position. Also the
`shortbot kill-switch` / `shortbot flatten` subcommands, registered from cli.py.
"""

from __future__ import annotations

from typing import Any

from . import killswitch as ks
from .entries import EntryDesk, withdraw
from .killswitch import HALTED, REDUCING

CLI_BY = "shortbot CLI (agent or human)"   # the allowlisted CLI can't tell who typed it; shortbot-admin can


async def cancel_resting(desk, reason: str) -> dict[str, list[int]]:
    """HALTED: withdraw every resting limit entry (the guardian repeats this every cycle while HALTED). Only an
    "unfilled" withdraw (limit dead, nothing filled, pre-armed stop gone with it) closes the row; "filled" and
    "unknown" keep the row and its stop, so the guardian's fill poll protects and journals the position.
    The desk's own rows only, incl. sends of unknown outcome (placing/unknown: withdraw finds them by client id);
    never an 'external' order, which may be the owner's (the guardian alerts those instead)."""
    out: dict[str, list[int]] = {"cancelled": [], "left_for_fill_poll": []}
    rows = desk._rows("status IN ('pending','placing','unknown')")
    if not rows:
        return out
    t = desk.tr.t
    for e in rows:   # compare-and-set: a resolution the reconciler or the placing call made meanwhile wins
        if await withdraw(t, e) == "unfilled" and desk._settle(e["id"], e["status"], status="cancelled", reason=reason):
            out["cancelled"].append(e["id"])
        else:
            out["left_for_fill_poll"].append(e["id"])
    return out


async def halt(tr, state: str, by: str, reason: str, evidence: dict | None = None,
               cause: str = "manual") -> dict[str, Any]:
    """Trip to `state`; when the desk is (now) HALTED, withdraw the resting entries right away."""
    res = await ks.trip(tr.j, tr.ex.settings, state, by, reason, evidence, cause)
    if ks.read(tr.j)["state"] == HALTED:
        try:
            res["resting"] = await cancel_resting(EntryDesk(tr), f"kill switch HALTED: {reason}")
        except Exception as e:  # noqa: BLE001 - the guardian's sweep retries
            res["resting"] = {"error": f"{type(e).__name__}: {e}"}
            await ks.notify(tr.j, tr.ex.settings, "kill_switch_cancel_failed", f"HALTED, but resting entries were "
                            f"not withdrawn ({e}); the guardian retries every cycle", urgent=True)
    return res


async def flatten(tr, by: str, reason: str, lesson: str = "") -> dict[str, Any]:
    """Flatten all: HALTED first (nothing new opens meanwhile), resting entries withdrawn, then every short closed
    through Trader.close (reduce-only market buy, journal + exchange-truth PnL). The state stays HALTED."""
    if not (reason or "").strip():
        raise ValueError("flatten needs a reason")
    why = f"flatten all: {reason.strip()}"
    st = await halt(tr, HALTED, by, why, cause="flatten")
    closed, failed = [], []
    for p in await tr._raw_positions():
        sym = p["symbol"]
        if p.get("side") != "short":
            failed.append({"symbol": sym, "error": f"{p.get('side')} position: not a short, close it manually"})
            continue
        try:
            r = await tr.close(sym, why, lesson)
        except Exception as e:  # noqa: BLE001 - one failure must not stop the others
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        (closed if r.get("ok") else failed).append({**r, "symbol": sym})
    left = [p["symbol"] for p in await tr._raw_positions()]
    if failed or left:
        await ks.notify(tr.j, tr.ex.settings, "flatten_incomplete", f"still open: {left}; failures: {failed}. "
                        "Stops are untouched; close these by hand.", urgent=True)
    tr.j.log_decision("flatten_all", why, None, snapshot={"by": by, "closed": closed, "failed": failed,
                                                         "still_open": left, "resting": st.get("resting")})
    return {"ok": not failed and not left, "state": ks.read(tr.j)["state"], "closed": closed, "failed": failed,
            "still_open": left, "resting": st.get("resting")}


# ------------------------------------------------------------ CLI (cli.py registers these)
def add_cli(sub) -> None:
    k = sub.add_parser("kill-switch", help="show the kill switch, or trip it to REDUCING | HALTED (clearing is "
                                           "human-only: shortbot-admin)")
    k.add_argument("state", nargs="?", choices=(REDUCING, HALTED))
    k.add_argument("--reason", help="required to trip")
    f = sub.add_parser("flatten", help="HALTED + market-close every short reduce-only; the state stays HALTED")
    f.add_argument("--reason", required=True)
    f.add_argument("--lesson", default="")


async def run_cli(app, args) -> dict[str, Any]:
    if args.cmd == "flatten":
        return await flatten(app.trader, CLI_BY, args.reason, args.lesson)
    if not args.state:
        await ks.watch_guardian(app.trader)
        return ks.status(app.journal)
    if not (args.reason or "").strip():
        return {"ok": False, "error": "--reason is required to trip the kill switch"}
    return await halt(app.trader, args.state, CLI_BY, args.reason)
