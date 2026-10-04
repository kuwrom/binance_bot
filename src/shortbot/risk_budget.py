"""Dynamic risk budget (docs/POSITION_MANAGEMENT.md §a): how much to risk on the NEXT trade, given the
current situation, not a fixed number.

    risk_$ = equity × base × m_dd × m_vol × m_regime × m_corr, then capped by portfolio/cluster heat room

- m_dd:     clip(1 − DD/DD_max, 0.25, 1)          drawdown from peak equity (Grossman–Zhou)
- m_vol:    clip(σ_target/σ_BTC_now, 0.5, 1.0)    BTC 30d realized vol vs its 1y median (never > 1)
- m_regime: favorable 1.0 · caution 0.75 · hostile/unknown 0.5
- m_corr:   0.5 if a short in the same correlation cluster is already open
- tilt:     conviction x regime-cell, evidence-gated (tilts.py), capped at max_conviction_mult
- m_gov:    account breakers (governor.py): halts block, drawdown/loss-streak halve; the risk of a new trade is
            also capped at what is left under today's loss cap, so one stop-out can never breach it
- m_ladder: the setup's own drawdown ladder (setup_ladder.py, docs/PROPOSALS.md P2): NORMAL 1, HALF 0.5, SHADOW 0
Conviction "M" (moonshot) ignores the multipliers above: 25% of a ring-fenced bucket (moonshot_bucket_pct of
equity, minus this quarter's moonshot losses), still under the governor and the heat caps.
The pure part (`compute_budget`) is unit-tested; `gather_inputs` pulls live data.
"""

from __future__ import annotations

import itertools
import math
import statistics
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .book import book_guard, diversity_mult, measured_corr, n_eff, pending_book, slot_cap
from .config import RiskLimits
from .entry_intents import external_orders
from .governor import REGIME_MULT, GovInputs, govern
from .signals.core import MAJORS
from .tilts import cell_mult, conviction_mult, regime_cell

_vol_cache: tuple[float, float, float, list[float]] | None = None  # (ts, vol_now, vol_target, btc daily closes)
_dup_cache: dict[str, tuple[float, dict[str, float]]] = {}       # setup -> (ts, registry overlaps), 1h
MOON_SHARE = 0.25


def cluster_of(symbol: str) -> str:
    base = symbol.split("/")[0]
    return f"major:{base}" if base in MAJORS else "alts"


@dataclass
class BudgetInputs:
    equity: float
    peak_equity: float
    regime: str = "unknown"
    btc_vol_now: float | None = None
    btc_vol_target: float | None = None
    open_risks: dict[str, float] = field(default_factory=dict)  # symbol -> USDT lost if its stop hits
    gov: dict[str, Any] | None = None                            # governor.govern() output
    cell: str | None = None                                      # current regime cell (tilts.regime_cell)
    setup_rs: list[float] = field(default_factory=list)          # closed R multiples of this setup
    cell_rs: list[float] = field(default_factory=list)           # ... of this setup in the current cell
    moon_quarter_pnl: float = 0.0                                # net pnl of "M" trades this quarter
    pending_risks: dict[str, float] = field(default_factory=dict)  # other symbols' resting groups (book.py)
    book: list[dict[str, Any]] = field(default_factory=list)     # open + pending trades: symbol, setup, sector
    setup: str | None = None                                     # the new trade's setup and sector
    sector: str | None = None
    pause: list[str] = field(default_factory=list)               # market-map pause (BTC/ETH impulse, squeeze)
    book_rho: float | None = None                                # measured mean pairwise corr (informational)
    ladder: dict[str, Any] | None = None                         # setup_ladder.status() for the new trade's setup


def clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def room_left_today(inp: BudgetInputs) -> float:
    """Today's daily-loss room once every open stop hits (open risk would hit the same daily cap): what a new trade's
    budget is capped at, and what a voluntary widen must fit (#74)."""
    room = ((inp.gov or {}).get("daily") or {}).get("room_usd", float("inf"))   # no governor read: no daily cap
    return room - sum(inp.open_risks.values())


async def room_today(trader, regime: str) -> dict[str, Any]:
    """room_left_today from live inputs, with its parts and the governor's halt."""
    inp = await gather_inputs(trader, regime)
    open_risk = sum(inp.open_risks.values())
    left = room_left_today(inp)
    return {"room_usd": round(left, 2), "daily_room_usd": round(left + open_risk, 2),
            "open_risk_usd": round(open_risk, 2), "halt": bool((inp.gov or {}).get("halt"))}


def compute_budget(limits: RiskLimits, inp: BudgetInputs, symbol: str, conviction: str = "B") -> dict[str, Any]:
    """Risk (USDT and %) allowed for a new short in `symbol` right now, with the reasons."""
    eq = max(inp.equity, 0.0)
    dd = max(0.0, (inp.peak_equity - eq) / inp.peak_equity * 100) if inp.peak_equity > 0 else 0.0
    m_dd = clip(1 - dd / limits.max_drawdown_pct, 0.25, 1.0)
    m_vol = (clip(inp.btc_vol_target / inp.btc_vol_now, 0.5, 1.0)
             if inp.btc_vol_now and inp.btc_vol_target else 1.0)
    m_regime = REGIME_MULT.get(inp.regime, 0.5)
    cl = cluster_of(symbol)
    same_cluster = [s for s in inp.open_risks if cluster_of(s) == cl and s != symbol]
    m_corr = 0.5 if same_cluster else 1.0
    gov = inp.gov or {"halt": False, "m_gov": 1.0, "reasons": [], "daily": {"room_usd": float("inf")}}
    m_gov = gov["m_gov"]
    reasons: list[str] = list(gov["reasons"])
    tier = (conviction or "B").upper()
    if tier == "M":
        bucket = eq * limits.moonshot_bucket_pct / 100
        bucket_room = max(0.0, bucket + min(inp.moon_quarter_pnl, 0.0))
        risk_usd = min(bucket * MOON_SHARE, bucket_room) * m_gov
        tilt, eff = 1.0, "M"
        reasons.append(f"moonshot: {MOON_SHARE:.0%} of a {bucket:.2f} USDT bucket ({bucket_room:.2f} left this quarter)")
    else:
        m_conv, eff, note = conviction_mult(tier, inp.setup_rs)
        m_cell, cell_note = cell_mult(inp.cell_rs, inp.setup_rs)
        tilt = min(m_conv * m_cell, limits.max_conviction_mult)
        reasons += [n for n in (note, cell_note if m_cell != 1.0 else "") if n]
        risk_usd = eq * limits.risk_per_trade_pct * m_dd * m_vol * m_regime * m_corr * tilt * m_gov / 100

    lad = inp.ladder or {}
    m_ladder = float(lad.get("mult", 1.0))
    risk_usd *= m_ladder
    reasons += list(lad.get("reasons") or [])
    # book level (learning #30): resting entries of other symbols can fill, so they are heat too; a new trade gets
    # at most one slot of the cluster budget; a repeated driver is the same bet; no new risk into a market pause
    # while the book already holds several alt shorts
    m_div, div_note = diversity_mult(inp.book, symbol, inp.setup, inp.sector)
    risk_usd = min(risk_usd * m_div, slot_cap(eq, limits, cl)) if tier != "M" else risk_usd
    reasons += [div_note] if div_note else []
    every = {**inp.open_risks}
    for s, r in inp.pending_risks.items():
        every[s] = every.get(s, 0.0) + r
    heat = sum(every.values())
    cluster_heat = sum(r for s, r in every.items() if cluster_of(s) == cl and s != symbol)
    room = min(eq * limits.max_portfolio_heat_pct / 100 - heat, eq * limits.max_cluster_heat_pct / 100 - cluster_heat)
    day_room = room_left_today(inp)
    capped = max(0.0, min(risk_usd, room, day_room))
    guard = book_guard(sum(1 for s in inp.open_risks if cluster_of(s) == "alts"), inp.pause) if cl == "alts" else None
    if guard:
        reasons.append(guard)
        capped = 0.0
    if dd >= limits.max_drawdown_pct:
        reasons.append(f"drawdown {dd:.1f}% >= {limits.max_drawdown_pct}%: STOP trading and review")
        capped = 0.0
    if gov["halt"]:
        capped = 0.0
    elif capped < risk_usd:
        reasons.append(f"capped: heat room {max(room, 0):.2f}, daily-cap room {max(day_room, 0):.2f} USDT")
    return {
        "risk_usd": round(capped, 2), "risk_pct": round(capped / eq * 100, 3) if eq else 0.0,
        "base_pct": limits.risk_per_trade_pct, "drawdown_pct": round(dd, 2), "conviction": eff, "cell": inp.cell,
        "multipliers": {"m_dd": round(m_dd, 3), "m_vol": round(m_vol, 3), "m_regime": m_regime, "m_corr": m_corr,
                        "tilt": round(tilt, 3), "m_gov": m_gov, "m_ladder": m_ladder, "m_div": m_div},
        "slot_cap_usd": round(slot_cap(eq, limits, cl), 2),
        "heat_pct": round(heat / eq * 100, 3) if eq else None,
        "pending_heat_pct": round(sum(inp.pending_risks.values()) / eq * 100, 3) if eq else None,
        "book": _book_view(inp, symbol),
        "cluster": cl, "cluster_heat_pct": round(cluster_heat / eq * 100, 3) if eq else None,
        "halted_until": gov.get("until") if gov["halt"] else None,
        "blocked": capped <= 0, "reasons": reasons,
    }


def _book_view(inp: BudgetInputs, symbol: str) -> dict[str, Any]:
    """The book this trade would join: positions incl. the new one, measured correlation, effective independent
    bets. Information only (learning #30: measure rho from our own book before letting it size anything)."""
    n = len({b["symbol"] for b in inp.book} | {symbol})
    ne = n_eff(n, inp.book_rho)
    return {"n": n, "rho": round(inp.book_rho, 3) if inp.book_rho is not None else None,
            "n_eff": round(ne, 2) if ne is not None else None}


def realized_vol(closes: list[float]) -> float | None:
    """Annualised stdev of daily log returns."""
    if len(closes) < 3:
        return None
    rets = [math.log(b / a) for a, b in itertools.pairwise(closes) if a > 0 and b > 0]
    return statistics.stdev(rets) * math.sqrt(365) if len(rets) > 1 else None


def btc_vol_now_and_target(daily_closes: list[float]) -> tuple[float | None, float | None]:
    """(30d realized vol now, median of rolling 30d vol over the last year)."""
    if len(daily_closes) < 60:
        return realized_vol(daily_closes[-31:]), None
    window = [realized_vol(daily_closes[i - 30:i + 1]) for i in range(30, len(daily_closes))]
    window = [w for w in window[-365:] if w]
    return window[-1], statistics.median(window)


def track_day_peak(journal, day_pnl: float, now: datetime | None = None) -> float:
    """Highest day P&L seen today (the guardian calls this every minute; arms the give-back stop)."""
    key = journal.mode_key(f"day_peak:{(now or datetime.now(UTC)).date().isoformat()}")
    peak = max(float(journal.get(key) or 0.0), float(journal.pending(key) or 0.0), day_pnl)   # its own, queued (#82)
    journal.merge(key, str(peak), _raise_day_peak, key, peak)   # #79: the pass never waits; the unit keeps the highest
    return peak


def _raise_day_peak(journal, key: str, day_pnl: float) -> None:
    journal.set(key, str(max(float(journal.get(key) or 0.0), day_pnl)))


def quarter_start(now: datetime) -> str:
    return now.replace(month=3 * ((now.month - 1) // 3) + 1, day=1, hour=0, minute=0, second=0,
                       microsecond=0).isoformat()


async def gather_inputs(trader, regime: str, positions: list[dict] | None = None,
                        setup: str | None = None, symbol: str | None = None,
                        limits: RiskLimits | None = None) -> BudgetInputs:
    """Live inputs: equity + tracked peak, BTC vol + regime cell (cached 1h), open risk per position at its
    exchange stop (and per external resting order at the max stop distance), governor state (day/week P&L, loss
    streak, give-back) and this setup's track record. `limits` (default the trader's): the guardian passes the
    effective research limits, so its governor verdict and the heat it sees match the session's (#12)."""
    global _vol_cache
    lim = limits or trader.ex.limits
    now = datetime.now(UTC)
    pos = positions if positions is not None else await trader.positions()
    acct = await trader.account()
    eq = acct["equity"]
    if not _vol_cache or time.time() - _vol_cache[0] > 3600:
        try:
            candles = await trader.ex.public.fetch_ohlcv("BTC/USDT:USDT", "1d", limit=400)
            closes = [c[4] for c in candles]
            vnow, tgt = btc_vol_now_and_target(closes)
            _vol_cache = (time.time(), vnow or 0.0, tgt or 0.0, closes)
        except Exception:  # noqa: BLE001 - vol is a dampener; missing data = neutral
            _vol_cache = (time.time(), 0.0, 0.0, [])
    vnow, tgt = _vol_cache[1] or None, _vol_cache[2] or None
    cell = regime_cell(_vol_cache[3], vnow, tgt)
    risks = {}
    for p in pos:
        stop, mark, qty = p.get("exchange_stop"), p.get("mark") or 0, p.get("contracts") or 0
        worst = stop if stop else mark * (1 + lim.max_stop_distance_pct / 100)
        risks[p["symbol"]] = max(0.0, (worst - mark) * qty)
    for e in external_orders(trader.j):   # resting orders the desk didn't place: no known stop, the same worst case
        risks[e["symbol"]] = risks.get(e["symbol"], 0.0) + (
            e["amount"] * e["limit_price"] * lim.max_stop_distance_pct / 100)
    rows = trader.j.closed_results()
    # day/week P&L and the transfer-adjusted peak exactly as account() reports them to every other consumer (#16)
    peak, day_pnl, week_pnl = acct["equity_peak"], acct["day_pnl"], acct["week_pnl"]
    gin = GovInputs(eq, peak, day_pnl, track_day_peak(trader.j, day_pnl, now), week_pnl,
                    [(r["closed_at"], r["pnl"]) for r in rows], regime, vnow, tgt, now)
    s_rows = [r for r in rows if setup and r["setup"] == setup and r["r_multiple"] is not None]
    qs = quarter_start(now)
    from .market.market_map import cached
    m = cached(trader.j, 900) or {}
    sector_of = lambda s: ((m.get("coins") or {}).get(s) or {}).get("sector")  # noqa: E731
    pend, pbook = pending_book(trader.j.db, symbol or "", getattr(trader.j, "mode", None))
    book = [{"symbol": p["symbol"], "setup": p.get("setup"), "kind": "open"} for p in pos] + pbook
    for b in book:
        b["sector"] = sector_of(b["symbol"])
    syms = sorted({b["symbol"] for b in book} | ({symbol} if symbol else set()))
    rho = await measured_corr(trader.ex.public, syms) if len(syms) >= 2 else None
    ladder = _ladder(trader.j, setup)
    return BudgetInputs(eq, peak, regime, vnow, tgt, risks, govern(lim, gin), cell,
                        [r["r_multiple"] for r in s_rows],
                        [r["r_multiple"] for r in s_rows if cell and r["regime_cell"] == cell],
                        sum(r["pnl"] for r in rows if r["conviction"] == "M" and r["closed_at"] >= qs),
                        pend, book, setup, sector_of(symbol) if symbol else None,
                        list((m.get("vetoes") or {}).get("pause") or []), rho, ladder)


def _ladder(journal, setup: str | None) -> dict[str, Any] | None:
    """The setup's ladder, with its epoch = the setup's current registry version and the registry's >= 70% episode
    overlaps as roll-up candidates. A failure is reported, never silently treated as NORMAL."""
    if not setup:
        return None
    try:
        from . import registry as R
        from .setup_ladder import status
        p = R.latest(journal.db, f"setup.{setup}")
        hit = _dup_cache.get(setup)
        if not hit or time.time() - hit[0] > 3600:      # registry.duplicates scans every pattern's episodes
            dups = {d["id"].split(".", 1)[1]: d.get("overlap", 0.0) for d in R.duplicates(journal.db, f"setup.{setup}")
                    if str(d.get("id", "")).startswith("setup.")} if p else {}
            hit = _dup_cache[setup] = (time.time(), dups)
        dups = hit[1]
        return status(journal, setup, epoch=(p or {}).get("created_at"), overlaps=dups)
    except Exception as e:  # noqa: BLE001 - sizing must not crash; fail closed like the kill switch's unreadable row
        return _ladder_fallback(journal, setup, e)


def _ladder_fallback(journal, setup: str, err: Exception) -> dict[str, Any]:
    """The ladder could not be replayed: use the stop record frozen in kv. An unreleased stop -> 0 (SHADOW);
    otherwise 0.5, half size until the ladder reads again. Never full size on an unknown state."""
    try:
        from .setup_ladder import events
        ev = events(journal, setup)
        last_stop = max((x["at"] for x in ev.get("stops") or []), default=None)
        stopped = last_stop is not None and not any(r >= last_stop for r in ev.get("reenables") or [])
    except Exception:  # noqa: BLE001 - even the kv read failed: half size
        stopped = False
    mult = 0.0 if stopped else 0.5
    return {"state": "UNKNOWN", "mult": mult,
            "reasons": [f"setup ladder unavailable ({type(err).__name__}): {'stopped -> 0' if stopped else 'half size'}"]}


async def governor_status(trader, regime: str, limits: RiskLimits | None = None) -> dict[str, Any]:
    """Account breakers + the evidence behind the tilts (streak test over all closed R), and `ladders`: each setup the
    setup ladder sizes down (HALF, SHADOW, UNKNOWN) with its multiplier and reasons (setup_ladder_view, cached)."""
    from .tilts import streak_test
    inp = await gather_inputs(trader, regime, limits=limits)
    rs = [r["r_multiple"] for r in trader.j.closed_results() if r["r_multiple"] is not None]
    try:
        from .setup_ladder_view import ladders
        lad: dict[str, Any] = {"ladders": ladders(trader.j)}
    except Exception as e:  # noqa: BLE001 - visibility only: sizing reads each ladder itself and fails closed
        lad = {"ladders": None, "ladders_error": f"{type(e).__name__}: {e}"}
    return {**(inp.gov or {}), "regime": regime, "cell": inp.cell, "equity": inp.equity,
            "peak_equity": inp.peak_equity, "streak_test": streak_test(rs),
            "moonshot_quarter_pnl": round(inp.moon_quarter_pnl, 2),
            "open_risk_usd": round(sum(inp.open_risks.values()), 2), **lad}   # open risk: what comes off the daily room
