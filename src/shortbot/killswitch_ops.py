"""Kill-switch actions (state and gate: killswitch.py): halt = trip + withdraw the resting entries; flatten = HALTED +
market-close every short reduce-only. Neither ever cancels a protective stop on an open position. Also the
`shortbot kill-switch` / `shortbot flatten` subcommands, registered from cli.py.
"""

from __future__ import annotations

from typing import Any

from . import killswitch as ks
from .entries import EntryDesk
from .killswitch import HALTED
from .symlock import SymbolBusy

CLI_BY = "shortbot CLI (agent or human)"   # the allowlisted CLI can't tell who typed it; shortbot-admin can


async def cancel_resting(desk, reason: str, keep=None) -> dict[str, list[int]]:
    """HALTED: withdraw every resting limit entry (the guardian repeats this every cycle while HALTED; entry_gates
    reuses it for a governor halt or a market pause, keeping the rows `keep(row)` spares). Only an
    "unfilled" withdraw (limit dead, nothing filled, pre-armed stop gone with it) closes the row; "filled" and
    "unknown" keep the row and its stop, so the guardian's fill poll protects and journals the position.
    The desk's own rows only, incl. sends of unknown outcome (placing/unknown: found by client id, and 'rejected' once
    GRACE_S passed without it reaching the book: desk._cancel_entry, re-read and compare-and-set inside the symbol
    lock); never an 'external' order, which may be the owner's (the guardian alerts those instead)."""
    out: dict[str, list[int]] = {"cancelled": [], "left_for_fill_poll": []}
    for e in [e for e in desk._rows("status IN ('pending','placing','unknown')") if not (keep and keep(e))]:
        try:
            got = await desk._cancel_entry(e, "cancelled", reason)
        except SymbolBusy:   # a session is acting on the symbol: the guardian's HALTED sweep retries every pass
            out.setdefault("busy", []).append(e["id"])
            continue
        out["cancelled" if got == "unfilled" else "left_for_fill_poll"].append(e["id"])
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
    for p in await tr.raw_positions():
        sym = p["symbol"]
        if p.get("side") != "short":
            failed.append({"symbol": sym, "error": f"{p.get('side')} position: not a short, close it manually"})
            continue
        try:
            r = await tr.close(sym, why, lesson)
        except Exception as e:  # noqa: BLE001 - one failure must not stop the others
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        (closed if r.get("ok") else failed).append({**r, "symbol": sym})
    left = [p["symbol"] for p in await tr.raw_positions()]
    if failed or left:
        await ks.notify(tr.j, tr.ex.settings, "flatten_incomplete", f"still open: {left}; failures: {failed}. "
                        "Stops are untouched; close these by hand.", urgent=True)
    tr.j.log_decision("flatten_all", why, None, snapshot={"by": by, "closed": closed, "failed": failed,
                                                         "still_open": left, "resting": st.get("resting")})
    return {"ok": not failed and not left, "state": ks.read(tr.j)["state"], "closed": closed, "failed": failed,
            "still_open": left, "resting": st.get("resting")}


# ------------------------------------------------------------ CLI (cli.py registers these)
def add_cli(sub) -> None:
    f = sub.add_parser("flatten", help="HALTED + market-close every short reduce-only; the state stays HALTED")
    f.add_argument("--reason", required=True)
    f.add_argument("--lesson", default="")


async def run_cli(app, args) -> dict[str, Any]:
    return await flatten(app.trader, CLI_BY, args.reason, args.lesson)
