"""Book allocator (learning #30): which scan candidates the desk should work next, given what it already holds.

It never places orders. It turns a scan + reads of the top candidates into a logged "book plan":
- free slots = max_positions - (open positions + resting entry groups);
- candidates whose read says short_setup are ranked by registry evidence (a live card > a shadow hypothesis >
  probation), then by scan score;
- slots 1-3 take any read-approved setup; slots 4-5 only EVENT playbooks (delisting, listing fade, unlock): more pump
  fades in the same regime are the same bet, independent events are not;
- a candidate repeating a setup+sector already held twice gets half size (book.diversity_mult);
- size_mult = the read's size cut x the diversity cut; the dollar risk still comes from the budget at placement.
Run: `python -m shortbot.allocator [--top 6] [--min-change 5] [--direction both]`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from .book import diversity_mult, pending_book

CORE_SLOTS = 3
EVENT_PLAYBOOKS = ("delisting-short", "listing-fade", "unlock-short")
PLAYBOOK_SETUP = {"pump-fade": "pump_fade", "delisting-short": "delisting", "listing-fade": "listing_fade",
                  "unlock-short": "unlock"}


def setup_of(playbook: str | None) -> str | None:
    if not playbook:
        return None
    return next((s for p, s in PLAYBOOK_SETUP.items() if playbook.startswith(p)), playbook)


def evidence_rank(read: dict[str, Any]) -> tuple[int, float]:
    notes = " ".join((read.get("verdict") or {}).get("notes") or [])
    level = 2 if "evidence-backed (live)" in notes else 0 if "PROBATION" in notes else 1
    return level, float(read.get("score") or 0)


def plan(reads: list[dict[str, Any]], book: list[dict[str, Any]], max_positions: int) -> dict[str, Any]:
    """Pure: reads = read_coin() outputs; book = open + pending trades [{symbol, setup, sector}]."""
    held = {b["symbol"] for b in book}
    used = len(held)
    adds: list[dict[str, Any]] = []
    skips: list[dict[str, Any]] = []
    for r in sorted(reads, key=evidence_rank, reverse=True):
        sym, v = r["symbol"], r.get("verdict") or {}
        pb = v.get("playbook")
        if sym in held:
            skips.append({"symbol": sym, "why": "already in the book (open or resting)"})
            continue
        if v.get("action") != "short_setup":
            skips.append({"symbol": sym, "why": f"read: {v.get('action')} ({'; '.join(v.get('why') or [])[:120]})"})
            continue
        slot = used + len(adds) + 1
        if slot > max_positions:
            skips.append({"symbol": sym, "why": f"no free slot ({max_positions} max)"})
            continue
        if slot > CORE_SLOTS and not (pb or "").startswith(EVENT_PLAYBOOKS):
            skips.append({"symbol": sym, "why": f"slot {slot}: slots 4-5 are for event setups (independent drivers),"
                                                 f" not another {pb}"})
            continue
        setup, sector = setup_of(pb), (r.get("numbers") or {}).get("sector")
        m_div, note = diversity_mult(book + [{"symbol": a["symbol"], "setup": a["setup"], "sector": a["sector"]}
                                             for a in adds], sym, setup, sector)
        adds.append({"symbol": sym, "slot": slot, "playbook": pb, "setup": setup, "sector": sector,
                     "score": r.get("score"), "evidence": ("live", "shadow", "probation")[2 - evidence_rank(r)[0]],
                     "size_mult": round(float(v.get("size_mult") or 1.0) * m_div, 3),
                     "why": [*(v.get("why") or []), *([note] if note else [])]})
    return {"slots_used": used, "max_positions": max_positions, "free_slots": max(0, max_positions - used),
            "add": adds, "skip": skips}


async def book_plan(app, top: int = 6, min_change: float = 5.0, direction: str = "both") -> dict[str, Any]:
    """Scan (not logged again), read the top non-vetoed candidates, and plan the book. Logged for research."""
    from .market.market_map import cached
    from .read import read_coin
    from .scanner import scan_shorts
    scan = await scan_shorts(app.ex, min_change=min_change, top_n=top, journal=None, direction=direction)
    positions = await app.trader.positions()
    _, pend = pending_book(app.journal.db, "")
    m = cached(app.journal, 900) or {}
    sector_of = lambda s: ((m.get("coins") or {}).get(s) or {}).get("sector")  # noqa: E731
    book = [{"symbol": p["symbol"], "setup": p.get("setup"), "sector": sector_of(p["symbol"])} for p in positions]
    book += [{**b, "sector": sector_of(b["symbol"])} for b in pend]
    reads = []
    for c in (scan.get("candidates") or [])[:top]:
        try:
            reads.append(await read_coin(app.ex, app.journal, c["symbol"]))
        except Exception as e:  # noqa: BLE001 - one unreadable coin never blocks the plan
            reads.append({"symbol": c["symbol"], "verdict": {"action": "error", "why": [f"{type(e).__name__}"]}})
    out = plan(reads, book, int(app.ex.limits.max_positions))
    out["book"] = book
    app.journal.log_decision("book_plan", f"{len(out['add'])} to work, {len(out['skip'])} skipped", None,
                             snapshot=out)
    return out


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m shortbot.allocator")
    p.add_argument("--top", type=int, default=6)
    p.add_argument("--min-change", type=float, default=5.0)
    p.add_argument("--direction", default="both", choices=("gainers", "losers", "both", "now"))
    a = p.parse_args()

    async def run() -> dict[str, Any]:
        from .app import close_app, get_app
        try:
            return await book_plan(get_app(), a.top, a.min_change, a.direction)
        finally:
            await close_app()
    print(json.dumps(asyncio.run(run()), indent=2, default=str))


if __name__ == "__main__":
    main()
