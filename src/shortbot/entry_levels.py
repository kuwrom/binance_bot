"""Level picker (owner decision 2026-10-05): ONE full-size limit short at the best level the market is likely to reach,
instead of split legs or a sell resting exactly at the touch. BEAMX 2026-10-04 (entry #43) rested at the touch price
0.00262: price touched it, 3,827 of 33,791 filled (11%) and the trade ran at ~5% of its planned size.

Per candidate level ({price, level_type, level_reason}, 1-4 of them, usually 2-4) the probability engine
(probability/engine.py, a NO-EDGE BASELINE: geometry and the coin's own volatility, no drift) gives:
- p_touch: the chance price touches the level within the fill window (`fill_prob`, an upper bound for a fill)
- p_fill: p_touch x haircut (HAIRCUT), for queue position and partial fills at an exact touch
- the odds if filled: closed-form and Monte Carlo P(first target before the stop) within the hold, its no-edge EV
- rr: reward to the first target over the risk to the stop, at the order's own price
The pick is the HIGHEST level (the best short entry) whose p_fill >= the threshold (THRESHOLD); when none clears it,
the likeliest fill, with a note. The sell rests INSIDE_TICKS ticks below the level (`inside`), so a touch of the
level trades through it.

HAIRCUT and THRESHOLD are hypotheses, not evidence (32 labelled entry_fill predictions by 2026-10-05: mean predicted
0.64, touched 0.56). Placement logs every candidate's touch as an `entry_level` prediction (log_candidates), labelled
by probability/labels.py like `entry_fill`, so the calibration report can settle them.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal
from typing import Any, cast

HAIRCUT = 0.85     # p_fill = p_touch x this: queue position and partial fills at an exact touch
THRESHOLD = 0.70   # the fill-adjusted probability a level must clear to be picked over a nearer one
INSIDE_TICKS = 2   # the sell rests this many ticks below the level (one if two would reach the market)
MAX_CANDIDATES = 4
SOURCE = "entry_level"   # the predictions table's source for each candidate's touch
LABEL = "NO-EDGE BASELINE (drift = 0): touch odds are an upper bound for a fill; fills are adversely selected"


class _OneContext:
    """A DataHub whose contexts are one per symbol: every candidate's odds read the same candles once."""

    def __init__(self, hub: Any) -> None:
        self.hub = hub
        self.ctx: dict[str, Any] = {}

    def context(self, symbol: str, ticker: dict | None = None) -> Any:
        if symbol not in self.ctx:
            self.ctx[symbol] = self.hub.context(symbol, ticker)
        return self.ctx[symbol]


def unit_interval(x: Any, name: str) -> float:
    v = float(x)
    if not 0 < v <= 1:   # NaN fails too
        raise ValueError(f"{name} must be in (0, 1], got {x}")
    return v


def inside(level: float, tick: float, last: float, ticks: int = INSIDE_TICKS) -> float | None:
    """The sell's price: `ticks` ticks below the level, floored to the tick grid, and still above the market (one tick
    if two would reach it). None when even one tick inside is at or below the market: the level is already reached."""
    lv, tk = Decimal(str(level)), Decimal(str(tick))
    for n in range(ticks, 0, -1):
        px = float(((lv - n * tk) / tk).to_integral_value(ROUND_FLOOR) * tk)
        if px > last:
            return px
    return None


def candidates_of(raw: Any) -> list[dict[str, Any]]:
    """Normalized candidates: [{price, level_type, level_reason}] from dicts or bare prices, highest first."""
    if not isinstance(raw, list | tuple) or not 1 <= len(raw) <= MAX_CANDIDATES:
        raise ValueError(f"candidates: a list of 1-{MAX_CANDIDATES} levels ({{price, level_type, level_reason}})")
    out = []
    for c in raw:
        c = c if isinstance(c, dict) else {"price": c}
        if c.get("price") is None:
            raise ValueError(f"candidate {c} has no price")
        out.append({"price": float(c["price"]), "level_type": c.get("level_type") or "unspecified",
                    "level_reason": c.get("level_reason") or ""})
    if len({c["price"] for c in out}) != len(out):
        raise ValueError("candidates must be distinct levels")
    return sorted(out, key=lambda c: -c["price"])


def pick(rows: list[dict[str, Any]], threshold: float) -> tuple[dict[str, Any] | None, str]:
    """(the chosen row, why): the highest usable level whose p_fill clears `threshold`, else the likeliest fill."""
    ok = [r for r in rows if r.get("ok")]
    if not ok:
        return None, "no usable candidate: " + "; ".join(f"{r['price']:g} {r.get('why')}" for r in rows)
    clear = [r for r in ok if r["p_fill"] >= threshold]
    if clear:
        best = max(clear, key=lambda r: r["price"])
        return best, f"the highest level whose fill-adjusted probability clears {threshold:g} ({best['p_fill']:g})"
    best = max(ok, key=lambda r: (r["p_fill"], r["price"]))
    return best, (f"no level clears {threshold:g}: the likeliest fill ({best['p_fill']:g}) was chosen; a nearer level, "
                  "a market entry on the trigger, or no entry may be better")


def split_hint(rows: list[dict[str, Any]], threshold: float) -> str | None:
    """A split entry is worth a look only when two levels clear the threshold and the far one is a sweep zone."""
    clear = sorted([r for r in rows if r.get("ok") and r["p_fill"] >= threshold], key=lambda r: -r["price"])
    if len(clear) >= 2 and clear[0]["level_type"] == "sweep":
        return (f"{clear[1]['price']:g} and {clear[0]['price']:g} (sweep) both clear {threshold:g}: a split entry "
                "(tranches) is an option; one full-size order at the pick is the default")
    return None


async def assess(engine: Any, symbol: str, stop: float, targets: list[float], candidates: Any, fill_hours: float,
                 hold_hours: float, last: float, tick: float, threshold: float = THRESHOLD, haircut: float = HAIRCUT,
                 ticks: int = INSIDE_TICKS, mc: bool = True) -> dict[str, Any]:
    """Score every candidate with `engine` (ProbabilityEngine.short_bracket's shape) and pick one (module doc).
    `last` is the market price, `tick` the symbol's price step. Nothing is logged here (log_candidates)."""
    if not fill_hours > 0 or not hold_hours > 0:
        raise ValueError("fill and hold windows must be positive hours")
    tg = sorted({float(t) for t in targets}, reverse=True)
    if not tg:
        raise ValueError("targets: at least one (R:R and the odds are to the first target)")
    rows = []
    for c in candidates_of(candidates):
        px = inside(c["price"], tick, last, ticks)
        row: dict[str, Any] = {**c, "order_price": px, "ok": False}
        rows.append(row)
        if px is None:
            row["why"] = f"at or below the market {last:g}: already reached (enter on the trigger instead)"
        elif c["price"] >= stop:
            row["why"] = f"at or above the stop {stop:g}"
        elif tg[0] >= px:
            row["why"] = f"first target {tg[0]:g} is not below the order price {px:g}"
        else:
            pe = await engine.short_bracket(symbol, stop, tg, hold_hours, entry=c["price"], fill_hours=fill_hours,
                                            mc=mc)
            if not pe.get("ok") or pe.get("fill_prob") is None:
                row["why"] = pe.get("error") or "no touch probability (engine)"
                continue
            first = pe["targets"][0]
            row.update(ok=True, p_touch=pe["fill_prob"], p_fill=round(pe["fill_prob"] * haircut, 4),
                       rr=round((px - tg[0]) / (stop - px), 3), p_target_if_filled=first.get("p_target"),
                       mc_p_target_if_filled=first.get("mc_p_target"), mc_ev_R=first.get("mc_ev_R"),
                       sigma_h=(pe.get("sigma_hourly") or {}).get("used"))
    chosen, why = pick(rows, threshold)
    return {"ok": chosen is not None, "symbol": symbol, "price_now": last, "stop": stop, "targets": tg,
            "fill_hours": round(fill_hours, 3), "hold_hours": hold_hours, "threshold": threshold, "haircut": haircut,
            "inside_ticks": ticks, "tick": tick, "candidates": rows, "chosen": chosen, "note": why,
            **({"split_hint": h} if (h := split_hint(rows, threshold)) else {}), "label": LABEL}


async def assess_live(ex: Any, t: Any, symbol: str, stop: float, targets: list[float], candidates: Any,
                      fill_hours: float, hold_hours: float, threshold: float = THRESHOLD,
                      haircut: float = HAIRCUT) -> dict[str, Any]:
    """assess() on the market now: the price and every candidate's odds from one shared market context, the tick
    from the trade client's market (what entries_place rounds limit prices to)."""
    from .market import DataHub
    from .probability import ProbabilityEngine
    hub = _OneContext(DataHub(ex))
    last = await hub.context(symbol).last()
    if not last:
        raise ValueError(f"no market price for {symbol}")
    tick = float(t.market(symbol)["precision"]["price"])
    engine = ProbabilityEngine(ex, None, cast(DataHub, hub))   # it calls only hub.context(): one per symbol here
    return await assess(engine, symbol, stop, targets, candidates, fill_hours, hold_hours, last, tick, threshold,
                        haircut)


async def for_proposal(tr: Any, p: dict[str, Any], fill_hours: float) -> dict[str, Any]:
    """A propose_entries proposal with `candidates` instead of `limit_price` / `tranches` -> the one full-size proposal
    at the picked level's inside price, carrying the pick (`level_pick`: record). The odds use the plan's targets and
    time stop; the fill window is the group's TTL."""
    from .exchange import to_symbol
    from .plans import build_plan
    from .setups import resolve
    if p.get("limit_price") is not None or p.get("tranches"):
        raise ValueError("candidates replace limit_price and tranches: pass one of them")
    sym = to_symbol(p["symbol"])
    if sym not in tr.t.markets:
        raise ValueError(f"{sym} not listed on the connected account")
    plan = build_plan(resolve(p["setup"]).default_plan, p.get("plan"))
    if not plan.targets:
        raise ValueError("a proposal with candidates needs plan targets: each level is scored on its R:R and odds to "
                         "the first target")
    res = await assess_live(tr.ex, tr.t, sym, float(p["stop_price"]), [t.price for t in plan.targets],
                            p["candidates"], fill_hours, float(plan.time_stop_hours or 24),
                            unit_interval(p.get("fill_threshold", THRESHOLD), "fill_threshold"),
                            unit_interval(p.get("fill_haircut", HAIRCUT), "fill_haircut"))
    ch = res["chosen"]
    if ch is None:
        raise ValueError(f"level picker: {res['note']}")
    out = {k: v for k, v in p.items() if k not in ("candidates", "fill_threshold", "fill_haircut")}
    return {**out, "limit_price": ch["order_price"], "level_type": ch["level_type"],
            "level_reason": ch["level_reason"], "level_pick": record(res)}


def record(res: dict[str, Any]) -> dict[str, Any]:
    """What an entry row keeps of a pick (entries.level_pick, and entry_features.level_pick on its trade): the chosen
    level, every candidate's probabilities, the threshold and haircut. Auditable and calibratable."""
    keep = ("price", "level_type", "order_price", "ok", "why", "p_touch", "p_fill", "rr", "p_target_if_filled",
            "mc_p_target_if_filled", "mc_ev_R", "sigma_h")
    ch = res["chosen"] or {}
    return {"chosen": ch.get("price"), "order_price": ch.get("order_price"), "threshold": res["threshold"],
            "haircut": res["haircut"], "fill_hours": res["fill_hours"], "price_now": res["price_now"],
            "note": res["note"], "candidates": [{k: r[k] for k in keep if r.get(k) is not None}
                                                for r in res["candidates"]]}


def log_candidates(j: Any, symbol: str, pick: dict[str, Any], entry_id: int, context: dict | None = None) -> list[int]:
    """Each usable candidate of a pick (record's shape) as an `entry_level` touch prediction (entry = the price then,
    target = the level, within the fill window), linked to the entry placed (ref_id): labelled like `entry_fill`."""
    ids = []
    for r in pick["candidates"]:
        if r.get("ok"):
            ids.append(j.log_prediction(
                SOURCE, symbol, pick["price_now"], None, r["price"], pick["fill_hours"], "touch", r["p_touch"], None,
                None, r.get("sigma_h"), entry_id, {**(context or {}), "level_type": r["level_type"],
                                                   "chosen": r["price"] == pick["chosen"], "p_fill": r["p_fill"],
                                                   "haircut": pick["haircut"], "threshold": pick["threshold"],
                                                   "order_price": r["order_price"]}))
    return ids
