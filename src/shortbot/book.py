"""Book-level risk rules for holding several shorts at once (learning #30, docs/RISK_GOVERNANCE.md).

Joint Kelly for N bets with pairwise correlation rho sizes each bet at single-bet Kelly / (1+(N-1)rho), and the book
only gains N_eff = N/(1+(N-1)rho) independent bets (capped at 1/rho). Alt shorts share one market factor and squeeze
together, so:
- slot_cap: a new trade gets at most (cluster heat cap / max_positions); more positions split the budget, never add
  to it.
- pending_book: resting entries of OTHER symbols count as heat (each group can fill): a group counts as its worst
  single order or whole split tranche, the same unit entries_place uses for notional exposure.
- diversity_mult: a trade sharing its setup AND sector with >= 2 open/pending trades is the same bet: half size.
- book_guard: with >= 3 open alt shorts during a market pause (BTC/ETH impulse, breadth squeeze) no new risk.
All pure except pending_book, which reads the entries table and tolerates its absence.
"""

from __future__ import annotations

import itertools
import math
import statistics
import time
from typing import Any

PENDING_STATUSES = ("pending", "placing", "unknown")
CORR_BARS = 168                 # 7 days of 1h returns for the book's measured correlation (informational)
CORR_TTL_S = 1800
_corr_cache: dict[tuple[str, ...], tuple[float, float | None]] = {}
DIVERSITY_MAX_SAME = 2          # this many open/pending trades with the same setup and sector -> half size
BOOK_GUARD_MIN_OPEN = 3         # open alt shorts that make a market pause a book-level block


def slot_cap(equity: float, limits, cluster: str) -> float:
    """Most risk one new trade may take: the cluster's heat cap split across max_positions slots."""
    pct = limits.max_cluster_heat_pct if cluster == "alts" else limits.max_portfolio_heat_pct
    return max(0.0, equity) * pct / 100 / max(1, int(limits.max_positions))


def pending_book(db, exclude_symbol: str, mode: str | None = None) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """({symbol: worst-case risk of its resting groups}, [{symbol, setup} per pending group]) for every OTHER
    symbol. Same-symbol rows are excluded: they are this trade's own legs or a plan it replaces. With `mode`
    (demo / live, #32) only that account's entries count; None counts every mode (errs safe)."""
    try:
        cols = {r[1] for r in db.execute("PRAGMA table_info(entries)")}
        if not cols:
            return {}, []
        tr, tid, fq = (c if c in cols else "NULL" for c in ("tranche", "trade_id", "filled_qty"))
        by_mode = bool(mode) and "mode" in cols
        q = (f"SELECT id, group_id, symbol, setup, amount, limit_price, stop_price, {tr}, status, {tid}, {fq}"
             f" FROM entries WHERE status IN ({','.join('?' * len(PENDING_STATUSES))}) AND symbol != ?"
             + (" AND mode = ?" if by_mode else ""))
        rows = db.execute(q, (*PENDING_STATUSES, exclude_symbol, *((mode,) if by_mode else ()))).fetchall()
    except Exception:  # noqa: BLE001 - no entries table yet
        return {}, []
    from .entries_partial import remaining
    groups: dict[str, dict[str, tuple[str, str, float, bool]]] = {}
    for rid, gid, sym, setup, amount, limit, stop, tranche, status, tid, filled in rows:
        # an order filled in part counts its remainder: what filled is its open trade's (already in the book)
        left = remaining({"status": status, "trade_id": tid, "amount": amount, "filled_qty": filled})
        risk = max(0.0, left * (float(stop or 0) - float(limit or 0)))
        unit = tranche or f"single{rid}"
        units = groups.setdefault(gid or f"row{rid}", {})
        s, st, r0, held = units.get(unit, (sym, setup, 0.0, False))
        units[unit] = (s, st, r0 + risk, held or tid is not None)
    risks: dict[str, float] = {}
    book: list[dict[str, Any]] = []
    for units in groups.values():
        sym, setup, risk, held = max(units.values(), key=lambda u: u[2])
        risks[sym] = risks.get(sym, 0.0) + risk
        if not held:   # one bet: its trade is in the book as open
            book.append({"symbol": sym, "setup": setup, "kind": "pending"})
    return risks, book


def diversity_mult(book: list[dict[str, Any]], symbol: str, setup: str | None,
                   sector: str | None) -> tuple[float, str]:
    """Half size when the new trade repeats a driver (same setup and sector) already held >= 2 times."""
    if not setup or not sector:
        return 1.0, ""
    same = [b for b in book if b.get("symbol") != symbol and b.get("setup") == setup and b.get("sector") == sector]
    if len(same) >= DIVERSITY_MAX_SAME:
        return 0.5, (f"driver overlap: {len(same)} open/pending {setup} trades in {sector} -> half size "
                     "(same bet, learning #30)")
    return 1.0, ""


def log_returns(closes: list[float]) -> list[float]:
    return [math.log(b / a) for a, b in itertools.pairwise(closes) if a > 0 and b > 0]


def avg_corr(returns: dict[str, list[float]], min_len: int = 48) -> float | None:
    """Mean pairwise Pearson correlation of aligned (tail-trimmed) return series; None with < 2 usable series."""
    series = [r for r in returns.values() if len(r) >= min_len]
    if len(series) < 2:
        return None
    n = min(len(s) for s in series)
    series = [s[-n:] for s in series]
    cs = []
    for i in range(len(series)):
        for j in range(i + 1, len(series)):
            try:
                cs.append(statistics.correlation(series[i], series[j]))
            except statistics.StatisticsError:   # a flat series
                continue
    return sum(cs) / len(cs) if cs else None


async def measured_corr(public, symbols: list[str]) -> float | None:
    """Mean pairwise correlation of the last 7 days of 1h returns of `symbols` (cached 30 min). Never raises."""
    key = tuple(sorted(set(symbols)))
    if len(key) < 2:
        return None
    hit = _corr_cache.get(key)
    if hit and time.time() - hit[0] < CORR_TTL_S:
        return hit[1]
    rets = {}
    for s in key:
        try:
            rets[s] = log_returns([c[4] for c in await public.fetch_ohlcv(s, "1h", limit=CORR_BARS + 1)])
        except Exception:  # noqa: BLE001 - a missing series only narrows the estimate
            continue
    rho = avg_corr(rets)
    _corr_cache[key] = (time.time(), rho)
    return rho


def n_eff(n: int, rho: float | None) -> float | None:
    """Effective number of independent bets N/(1+(N-1)rho) (Clarke-de Silva-Thorley; learning #30)."""
    if n <= 0 or rho is None:
        return None
    return n / (1 + (n - 1) * max(0.0, rho))


def book_guard(open_alts: int, pause: list[str]) -> str | None:
    if open_alts >= BOOK_GUARD_MIN_OPEN and pause:
        return f"book: {open_alts} open alt shorts during a market pause ({pause[0]}): no new risk"
    return None
