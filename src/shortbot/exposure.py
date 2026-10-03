"""What can still become a position without another cap check (#74). The portfolio caps (max_positions, total
notional) count it beside the live positions in every risk-adding check: open_short, a limit entry's placement and an
add, through this one helper so they can't drift.

- resting limit entries (entry_intents.ON_BOOK: pending, placing, unknown, external): one OCO group fills at most one
  order, but a split entry's legs can ALL fill, so a group counts its biggest single order or its biggest whole tranche;
  each external order (not placed by shortbot) can fill on its own: one unit each;
- market entries of unknown outcome (trades still 'opening'): they may still fill, at their full intended size
  (entry_features.intended_notional, journalled before the send; a row from before it: the per-coin cap). Not when
  the symbol shows a live position: that position is the fill, already counted.
A resting order on a symbol that already holds a position adds notional, not another position.
Read under the account lock (symlock.py), so two sessions can't both count the same free slot.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from .entry_intents import ON_BOOK
from .risk import AccountState

if TYPE_CHECKING:
    from .journal import Journal

Unit = tuple[str, float]   # (symbol, USDT notional that may still become exposure)


def pending_units(j: Journal, live: Iterable[str], max_notional: float, group: str | None = None,
                  symbol: str | None = None, own_resting: bool = True) -> list[Unit]:
    """Exposure that may still appear, in this journal's mode. `group`: the group being placed (its own legs share one
    budget). `symbol`: the caller's own symbol; an entry of unknown outcome there is refused under its symbol lock (a
    second trade), so it isn't counted here, nor its resting entries with own_resting=False (open_short refuses those
    under the lock too)."""
    live = set(live)
    try:
        rows = [r for r in j._rows(f"SELECT * FROM entries WHERE {ON_BOOK} ORDER BY id")
                if (r.get("mode") or "demo") == j.mode]
    except sqlite3.OperationalError:   # no entries table yet: nothing rests
        rows = []
    by_group: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        g = f"external{r['id']}" if r["status"] == "external" else r["group_id"]
        if g != group and (own_resting or r["symbol"] != symbol):
            by_group.setdefault(g, []).append(r)
    out: list[Unit] = []
    for g_rows in by_group.values():
        units: dict[str, Unit] = {}
        for r in g_rows:
            key = r.get("tranche") or f"single{r['id']}"
            s, n = units.get(key, (r["symbol"], 0.0))
            units[key] = (s, n + float(r["amount"] or 0) * float(r["limit_price"] or 0))
        out.append(max(units.values(), key=lambda u: u[1]))
    for t in j.open_trades(include_opening=True):
        if t["status"] == "opening" and t["symbol"] not in live and t["symbol"] != symbol:
            intended = (t.get("entry_features") or {}).get("intended_notional")
            out.append((t["symbol"], float(intended) if intended else max_notional))
    return out


def with_pending(acct: AccountState, units: list[Unit]) -> AccountState:
    """The account as the caps must see it: live positions plus every unit that may still fill."""
    return AccountState(acct.equity, tuple(dict.fromkeys((*acct.open_symbols, *(s for s, _ in units)))),
                        acct.total_open_notional + sum(n for _, n in units), acct.day_pnl)


def counted(j: Journal, acct: AccountState, max_notional: float, group: str | None = None, symbol: str | None = None,
            own_resting: bool = True) -> AccountState:
    """`acct` with everything that may still fill (pending_units) counted in."""
    return with_pending(acct, pending_units(j, acct.open_symbols, max_notional, group, symbol, own_resting))
