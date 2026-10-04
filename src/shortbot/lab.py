"""Code-mode research API: synchronous helpers for ad-hoc analysis scripted through Bash, so questions that
no MCP tool anticipated can still be answered without adding a tool, and raw data stays out of context
(print only the conclusion).

    .venv/bin/python - <<'PY'
    from shortbot import lab
    c = lab.ohlcv("QNT", "15m", 96)                      # [[ts, o, h, l, c, v], ...]
    print(lab.pct(c[0][4], c[-1][4]))
    rows = lab.candidates(labelled=True)                 # logged scans + real outcomes
    print(lab.group_mean(rows, lambda r: r["features"]["rsi_1h"] > 85, "short_ret_24h"))
    PY

Read-only on purpose: no order functions live here. Orders only go through shortbot's order paths (the MCP tools or
the `shortbot` CLI), where the risk checks, the kill switch and the audit trail apply.
"""

from __future__ import annotations

import asyncio
import statistics
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from . import fee_audit as _fee_audit
from . import indicators as ind
from . import research as _research
from . import scanner as _scanner
from . import weight
from .config import Settings
from .exchange import Exchanges, to_symbol
from .journal import Journal

pct = ind.pct_change
rsi, ema, atr, vwap_bands = ind.rsi, ind.ema, ind.atr, ind.vwap_bands


def _with_ex(fn: Callable[[Exchanges], Any]) -> Any:
    async def run():
        ex = Exchanges(Settings.from_env())
        ex.journal = journal()   # scans see the desk's limits (desk_limits.py)
        try:
            with weight.tagged(weight.RESEARCH, wait_s=weight.ENTRY_WAIT_S):   # yields to the desk and guardian (#70)
                return await fn(ex)
        finally:
            await ex.close()
    return asyncio.run(run())


def journal() -> Journal:
    s = Settings.from_env()
    return Journal(s.db_path, s.mode)   # stats and trades of this mode; all_modes=True for both


# ------------------------------------------------------------ market data (prod, public)
def ohlcv(symbol: str, timeframe: str = "1h", limit: int = 200, since: int | None = None) -> list[list[float]]:
    return _with_ex(lambda ex: ex.public.fetch_ohlcv(to_symbol(symbol), timeframe, since=since, limit=limit))


def oi_history(symbol: str, period: str = "1h", limit: int = 100) -> list[dict]:
    return _with_ex(lambda ex: ex.oi_history(symbol, period, limit))


def funding_history(symbol: str, limit: int = 100) -> list[dict]:
    return _with_ex(lambda ex: ex.public.fetch_funding_rate_history(to_symbol(symbol), limit=limit))


def taker_ratio(symbol: str, period: str = "1h", limit: int = 48) -> list[float]:
    return _with_ex(lambda ex: ex.taker_ratio(symbol, period, limit))


def tickers() -> dict[str, dict]:
    """24h tickers for tradable crypto perps only."""
    async def f(ex):
        perps = await ex.coin_perps()
        return {s: t for s, t in (await ex.public.fetch_tickers()).items() if s in perps}
    return _with_ex(f)


def snapshot(symbol: str) -> dict:
    return _with_ex(lambda ex: _scanner.coin_snapshot(ex, symbol))


def scan(min_change: float = 10.0, top_n: int = 25) -> dict:
    """Scan WITHOUT logging candidates (exploration). Use the MCP scan_shorts tool for logged scans."""
    return _with_ex(lambda ex: _scanner.scan_shorts(ex, min_change=min_change, top_n=top_n))


# ------------------------------------------------------------ research data
def candidates(labelled: bool = True, limit: int = 5000) -> list[dict]:
    return journal().candidates(labelled_only=labelled, limit=limit)


def trades(limit: int = 500, drills: bool = False) -> list[dict]:
    """Newest first; the plumbing drills' trades (#46) only with drills=True."""
    return journal().trades(limit, drills=drills)


def metrics(symbol: str, limit: int = 1000) -> list[dict]:
    """Guardian-recorded price/funding/OI history (outlives Binance's 30-day window)."""
    return journal().metric_history(to_symbol(symbol), limit)


def report(min_n: int = 5) -> dict:
    return _research.report(journal(), min_n)


def fee_audit(limit: int = 500) -> dict:
    """Closed trades' journalled fees / P&L against Binance income matched by tradeId (fee_audit.py, #75): before,
    after and diff per trade, how its position was found (`matched`: its journalled order ids, #76, or time), and the
    totals. Reads the account's trade history and income; writes nothing."""
    return _with_ex(lambda ex: _fee_audit.audit(SimpleNamespace(t=ex.require_trade(), j=journal()), limit))


def group_mean(rows: list[dict], key: Callable[[dict], Any], field: str = "short_ret_24h") -> dict[Any, dict]:
    """Group rows by key(row) and summarise `field`: {group: {n, mean, median, hit_rate}}."""
    groups: dict[Any, list[float]] = {}
    for r in rows:
        try:
            k = key(r)
        except (KeyError, TypeError):
            continue
        v = r.get(field)
        if v is not None:
            groups.setdefault(k, []).append(v)
    return {k: {"n": len(v), "mean": round(statistics.mean(v), 3), "median": round(statistics.median(v), 3),
                "hit_rate": round(sum(x > 0 for x in v) / len(v) * 100, 1)} for k, v in groups.items()}
