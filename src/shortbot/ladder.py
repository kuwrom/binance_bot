"""3-level ladder with per-level odds and a state-score exposure rule (docs/LADDER.md). SHADOW MODE: the guardian
logs what the rule would do; nothing trades from it until it beats hold-to-plan and a price-only placebo.

Key idea: exposure follows EDGE (odds above the no-edge baseline), and edge only comes from information that moves
at a fixed price distance: squeeze state, tape flips, market breadth/impulse, time decay. Levels are fixed at
entry; probabilities are recomputed. Pure functions are unit-tested; `build_ladder` does the I/O."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from .probability.baseline import short_geometry

LAMBDA = 0.5            # score -> drift scale (assumption; calibrate from shadow outcomes)
COST = 0.002            # round-trip fees + slippage
STEP, CONFIRM, MIN_GAP_MIN, MAX_CHANGES = 0.25, 2, 30, 2
POS_TAPE = {"failed_sweep", "post_pump_decay", "hype_fade"}
NEG_TAPE = {"accepted_breakout", "pump_in_progress", "attention_rising", "spot_led_rally"}
SCHEMA = """CREATE TABLE IF NOT EXISTS ladder_shadow (id INTEGER PRIMARY KEY, ts TEXT, trade_id INTEGER, symbol TEXT,
    price REAL, score REAL, parts TEXT, levels TEXT, x_star REAL, x_actual REAL, x_virtual REAL, action TEXT);
CREATE TABLE IF NOT EXISTS trial_ledger (variant TEXT PRIMARY KEY, family TEXT, params TEXT, created TEXT)"""


def ensure_schema(journal) -> None:
    """Once per journal: the tables (DDL, never inside a unit) and the variants' trial-ledger rows (posted, #79)."""
    if getattr(journal, "_ladder_ready", False):
        return
    db = journal.db
    db.executescript(SCHEMA)
    have = {r[1] for r in db.execute("PRAGMA table_info(ladder_shadow)")}
    for col, ddl in (("variant", "TEXT DEFAULT 'base'"), ("stop_v", "REAL"), ("entry", "REAL"), ("init_r", "REAL")):
        if col not in have:
            db.execute(f"ALTER TABLE ladder_shadow ADD COLUMN {col} {ddl}")
    rows = [(name, "ladder", json.dumps(prm)) for name, prm in VARIANTS.items()]
    journal.post(lambda j: j.db.executemany("INSERT OR IGNORE INTO trial_ledger VALUES (?,?,?,datetime('now'))", rows))
    journal._ladder_ready = True


def race_with_edge(a: float, b: float, k: float) -> float:
    """P(price falls b before rising a), log distances, favourable drift k (per unit variance, k = -2mu/sigma^2).
    k = 0 gives the martingale a/(a+b); k > 0 favours the short."""
    if abs(k) < 1e-9:
        return a / (a + b)
    num = math.expm1(k * a)
    den = math.exp(k * a) - math.exp(-k * b)
    return max(0.0, min(1.0, num / den)) if den else a / (a + b)


def state_score(f: dict[str, Any], ctx: dict[str, Any]) -> tuple[float, dict[str, float]]:
    """Rule-based edge score in [-2, +2] until the probability model passes its calibration gate."""
    p: dict[str, float] = {}
    if (ctx.get("oi_chg_1h_pct") or 0) > 1.0 and (ctx.get("ret_1h_pct") or 0) > 0:
        p["squeeze_oi_price_up"] = -1
    if (f.get("funding_8h") or 0) < 0:
        p["funding_negative"] = -1
    if (f.get("ti_z_1h") or 0) >= 2 and (ctx.get("ret_1h_pct") or 0) > 0:
        p["taker_buy_burst"] = -1
    game = ctx.get("game")
    if game in POS_TAPE:
        p["tape_supports"] = 1
    elif game in NEG_TAPE:
        p["tape_against"] = -1
    b_now, b_entry = f.get("breadth_pct"), ctx.get("breadth_at_entry")
    if b_now is not None and b_entry is not None and b_now - b_entry >= 15:
        p["breadth_thrust"] = -1
    if ctx.get("impulse_pause"):
        p["btc_eth_impulse"] = -1
    if b_now is not None and b_now < 20 and "breadth_thrust" not in p:
        p["deep_risk_off"] = 0.5
    if ctx.get("age_frac_of_time_stop", 0) > 0.5 and (ctx.get("mfe_r") or 0) < 0.5:
        p["time_decay"] = -0.5
    return max(-2.0, min(2.0, sum(p.values()))), p


def ladder_levels(price: float, stop: float, plan_targets: list[float], sigma_t: float) -> list[float]:
    """T1..T3 below price: plan targets still below price, filled with 1/2/3 x sigma*sqrt(T) (log) moves."""
    t = sorted((x for x in plan_targets if x < price), reverse=True)[:3]
    k = 1
    while len(t) < 3:
        cand = price * math.exp(-k * sigma_t)
        if not t or cand < t[-1] * 0.999:
            t.append(cand)
        k += 1
    return t


def evaluate_ladder(price: float, stop: float, targets: list[float], sigma_h: float, hours_left: float,
                    score: float) -> list[dict[str, Any]]:
    """Per level: p0 (no edge, time-limited), p_model (edge-adjusted), EV in R net of costs."""
    a = math.log(stop / price)
    risk_pct = stop / price - 1
    sigma_t = sigma_h * math.sqrt(max(hours_left, 0.25))
    k = score * LAMBDA / sigma_t if sigma_t else 0.0
    out = []
    for t in targets:
        b = math.log(price / t)
        g = short_geometry(price, stop, t, sigma_h, max(hours_left, 0.25))
        ratio = race_with_edge(a, b, k) / race_with_edge(a, b, 0.0)
        pm = min(0.99, g["p_target"] * ratio)
        ps = max(0.0, min(1.0 - pm, g["p_stop"] * (1 - race_with_edge(a, b, k)) / max(1 - a / (a + b), 1e-9)))
        reward_r = (price - t) / price / risk_pct if risk_pct > 0 else 0.0
        cost_r = COST / risk_pct if risk_pct > 0 else 0.0
        out.append({"target": t, "p0": round(g["p_target"], 3), "p_model": round(pm, 3), "p_stop_model": round(ps, 3),
                    "reward_R": round(reward_r, 2), "ev_R": round(pm * reward_r - ps - cost_r, 3)})
    return out


def exposure_target(score: float) -> float:
    return max(0.0, min(1.0, 0.5 + score / 4))


def live_exposure_target(score: float, cap: float) -> float:
    """ladder_live_v1: the normal entry IS full size (x = 1 at a neutral score); a sweet score may scale up to
    `cap` x (max_add_r_mult, risk at stop <= cap x 1R) and a sour one scales down to 0."""
    return max(0.0, min(cap, 1.0 + score / 2))


BASE = {"step": STEP, "confirm": CONFIRM, "gap_min": MIN_GAP_MIN, "max_changes": MAX_CHANGES, "lam": LAMBDA}
# Shadow variants scored side by side on the same trades (docs/LADDER.md C). Every variant ever tried goes in the
# trial ledger so the winner can be judged with a deflated Sharpe, not by luck of the many.
VARIANTS: dict[str, dict[str, Any]] = {
    "base": BASE,
    "fast": {**BASE, "confirm": 1, "gap_min": 15},
    "slow": {**BASE, "step": 0.5, "confirm": 3, "gap_min": 60},
    "placebo": {**BASE, "placebo": True},        # price-only score: must LOSE to base for the edge to be real
    "adaptive": {**BASE, "adaptive": True},      # event-driven stop re-leveling (tighten-only), fixed plan = ghost
}


def decide(history: list[dict[str, Any]], x_virtual: float, x_star: float, now: datetime,
           prm: dict[str, Any] | None = None) -> tuple[str, float]:
    """Hysteresis: act only when |x* - x| >= step for `confirm` consecutive checks, >= gap_min since the last
    action, and at most max_changes adds / reductions. Returns (action, new virtual exposure)."""
    prm = prm or BASE
    st, cf = prm["step"], prm["confirm"]
    gap = x_star - x_virtual
    if abs(gap) < st:
        return "hold", x_virtual
    recent = history[-(cf - 1):] if cf > 1 else []
    same_side = all((h["x_star"] - h["x_virtual"]) * gap > 0 and abs(h["x_star"] - h["x_virtual"]) >= st
                    for h in recent) and len(recent) == cf - 1
    last_act = next((h for h in reversed(history) if h["action"] not in ("hold", "wait")), None)
    if last_act and (now - datetime.fromisoformat(last_act["ts"])).total_seconds() < prm["gap_min"] * 60:
        return "wait", x_virtual
    kind = "add" if gap > 0 else "reduce"
    if sum(1 for h in history if h["action"] == kind) >= prm["max_changes"]:
        return f"{kind}_capped", x_virtual
    if not same_side:
        return "wait", x_virtual
    step = st if abs(gap) < 2 * st else round(abs(gap) / st) * st
    return kind, round(max(0.0, min(prm.get("x_max", 1.0), x_virtual + math.copysign(step, gap))), 3)


def placebo_score(price: float, entry: float, init_r: float) -> float:
    """Price-only 'edge': the further price moves against the short (in R), the lower the score."""
    if init_r <= 0:
        return 0.0
    return max(-2.0, min(2.0, -2 * (price - entry) / init_r))


def adaptive_stop(prev: float, c1h: list[list[float]], atr: float, price: float) -> tuple[float, str | None]:
    """Event-driven, tighten-only: after a CONFIRMED lower 1h swing high (a swing bar with a lower-high bar after
    it, both closed), the stop may drop to that swing high + 0.5 ATR, if that is still above price."""
    closed = c1h[:-1]
    if len(closed) < 3:
        return prev, None
    a, b, c = closed[-3], closed[-2], closed[-1]
    if b[2] >= a[2] and b[2] > c[2]:
        cand = b[2] + 0.5 * atr
        if price < cand < prev:
            return cand, f"confirmed swing high {b[2]:g} + 0.5 ATR"
    return prev, None


async def build_ladder(ex, journal, pos: dict[str, Any], log: bool = True) -> dict[str, Any]:
    """Current ladder for an open position (as returned by Trader.positions()), optionally logged to shadow."""
    from .market import DataHub
    from .probability.engine import sigma_hourly
    from .signals import evaluate
    from .tape import classify
    sym, trade = pos["symbol"], journal.active_trade(pos["symbol"]) or {}
    plan = trade.get("plan") or {}
    ctx = DataHub(ex).context(sym)
    ev = await evaluate(ctx)
    f = ev.features
    sig = (await sigma_hourly(ctx))["used"] or 0.01
    c1h = await ctx.candles("1h", 3)
    oi = await ctx.oi_history("1h", 3)
    now = datetime.now(timezone.utc)
    age_h = (now - datetime.fromisoformat(trade["opened_at"])).total_seconds() / 3600 if trade.get("opened_at") else 0
    tsh = plan.get("time_stop_hours") or 24
    entry, stop, price = pos["entry"], pos["exchange_stop"] or trade.get("stop_price"), pos["mark"]
    init_r = (trade.get("initial_stop") or stop) - entry
    sctx = {"game": classify(f)["state"], "ret_1h_pct": (c1h[-1][4] / c1h[-2][4] - 1) * 100 if len(c1h) > 1 else None,
            "oi_chg_1h_pct": (oi[-1]["oi"] / oi[-2]["oi"] - 1) * 100 if len(oi) > 1 and oi[-2]["oi"] else None,
            "breadth_at_entry": (trade.get("entry_features") or {}).get("breadth_pct"),
            "impulse_pause": any("in 15 min" in v for v in ev.vetoes),
            "age_frac_of_time_stop": age_h / tsh, "mfe_r": (entry * (pos.get("mfe_pct") or 0) / 100) / init_r
            if init_r > 0 else 0}
    score, parts = state_score(f, sctx)
    hours_left = max(tsh - age_h, 1.0)
    levels = evaluate_ladder(price, stop, ladder_levels(price, stop, [t["price"] for t in plan.get("targets") or []],
                                                        sig * math.sqrt(hours_left)), sig, hours_left, score)
    orig = plan.get("full_size") or trade.get("amount") or pos["contracts"]
    x_actual = round(pos["contracts"] / orig, 3) if orig else 1.0
    ensure_schema(journal)
    atr = (await ctx.candles("1h", 20))
    atr_v = sum(c[2] - c[3] for c in atr[-14:]) / max(len(atr[-14:]), 1) if atr else 0.0
    c1h4 = await ctx.candles("1h", 5)
    rows, logged = {}, []
    for name, prm in VARIANTS.items():
        hist = [dict(zip(("ts", "x_star", "x_virtual", "action", "stop_v"), r)) for r in journal.db.execute(
            "SELECT ts, x_star, x_virtual, action, stop_v FROM ladder_shadow WHERE trade_id=? AND"
            " COALESCE(variant,'base')=? ORDER BY id", (trade.get("id"), name))]
        live = name == "base" and plan.get("ladder_live")      # live trades: the base variant tracks REAL exposure
        x_v = x_actual if live or not hist else hist[-1]["x_virtual"]
        stop_v = (hist[-1]["stop_v"] if hist and hist[-1]["stop_v"] else stop)
        note = None
        if prm.get("adaptive"):
            stop_v, note = adaptive_stop(stop_v, c1h4, atr_v, price)
        sc = placebo_score(price, entry, init_r) if prm.get("placebo") else score
        if live:
            cap = max(1.0, ex.limits.max_add_r_mult)
            x_star, prm = live_exposure_target(sc, cap), {**prm, "x_max": cap}
        else:
            x_star = exposure_target(sc)
        if x_v > 0 and price >= stop_v:
            action, x_new = "stopped", 0.0
        else:
            action, x_new = decide(hist, x_v, x_star, now, prm)
        rows[name] = {"score": sc, "x_star": x_star, "x_virtual": x_new, "action": action, "stop_v": stop_v,
                      "relevel": note}
        if log and trade.get("id"):
            logged.append((now.isoformat(timespec="seconds"), trade["id"], sym, price, sc, json.dumps(parts),
                           json.dumps(levels), x_star, x_actual, x_new, action, name, stop_v, entry, init_r))
    if logged:   # one unit (#79)
        await journal.write(lambda j: j.db.executemany(
            "INSERT INTO ladder_shadow (ts, trade_id, symbol, price, score, parts, levels, x_star, x_actual,"
            " x_virtual, action, variant, stop_v, entry, init_r) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", logged))
    b = rows["base"]
    return {"ts": now.isoformat(timespec="seconds"), "trade_id": trade.get("id"), "symbol": sym, "price": price,
            "stop": stop, "score": score, "parts": parts, "levels": levels, "x_star": b["x_star"],
            "x_actual": x_actual, "x_virtual": b["x_virtual"], "action": b["action"], "sigma_h": round(sig, 5),
            "hours_left": round(hours_left, 1), "variants": rows}
