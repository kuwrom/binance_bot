"""Round-2 variants (docs/FAILURES.md "Round 2"). Every rule below was written down before the out-of-sample run;
every variant is one trial in the ledger (`ledger()` = rounds 0-1's 53 + these).

Q1 ENTRY TYPE in breakdowns. One common "armed" signal, three ways in, identical stop / targets / exits:
   armed at the close of bar i: 24h return <= drop, lower high (bounce_fade's rule), close below EMA20 for 2 bars,
   bar i's high still below EMA20 (no bounce yet), tradable; at most one arm per coin per BD_COOLDOWN bars.
   stop    = max(EMA20_i, high of bars i-2..i) + 0.5 ATR, fixed at the arm
   targets = 1.5R (half) / 3R (rest) of each entry's own risk; breakeven 1R, trail 2R/1R, 24h time stop
             (bounce_fade's exits)
   (a) limit  resting sell at EMA20_i, fills when a later bar trades through it (the live bounce-sell), 12-bar TTL
   (b) stop   sell-stop 0.1% under the 12h breakdown low (bars i-11..i), live only after a bounce attempt (a bar
              trading >= close_i + 0.5 ATR); cancelled if the stop trades first; 12-bar TTL; 2x slippage (stop-market)
   (c) market at the arm bar's close
   The same-hour placebo enters at the FILL bar (notes "placebo@fill").

Q2 EXIT MANAGEMENT on identical signals: (i) fixed stop + targets + time stop; (ii) + breakeven at +1R;
   (iii) + trailing at the LIVE guardian's parameters (setups.py: pump_fade 10% activation / 4% trail for the
   pump family; breakdown 5% / 3% for bounce_fade), converted to R per trade; (iv) + both.

Q3 re-entry after a stop-out: once a trade is stopped at its original stop S, re-short at the close of the first
   of the next 3 bars that closes below S; stop = the highest high since the stop bar + the base ATR buffer;
   the original target prices; the base time stop and breakeven. One re-entry per trade.
   funding budget: exit at a close once funding PAID >= 0.5R while the open price gain is < 0.5R."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace

from .features import Coin, Market
from .sim import Plan, simulate
from .strategies import WARMUP, Trial, atr_abs, plan, r_targets, tradable
from .strategies_v2 import BASE, discount
from .strategies_v2 import ledger as ledger_v2
from .strategies_v2 import variant

# ---------------------------------------------------------------- Q1: breakdown entry types
BD_DROPS = (-15, -8)                  # round 0's bounce_fade grid; -8 is the live `breakdown` detector
BD_TTL, BD_COOLDOWN = 12, 12
BD_STOP_ATR, BD_BOUNCE_ATR, BD_LOW_BARS, BD_TRIG_BELOW = 0.5, 0.5, 12, 0.001
BD_TARGETS = ((1.5, 0.5), (3.0, 0.5))
BD_TIME, BD_BE, BD_TRAIL = 24, 1.0, (2.0, 1.0)


def breakdown_arms(coin: Coin, drop: float) -> Iterable[int]:
    r, e, last = coin.rows, coin.ema20, -10 ** 9
    for i in range(max(WARMUP, 36), len(r)):
        if i - last < BD_COOLDOWN:
            continue
        r24 = coin.ret(i, 24)
        if r24 is None or r24 > drop or not tradable(coin, i):
            continue
        lower_high = coin.high(i - 11, i + 1) < coin.high(i - 36, i - 12)
        if lower_high and r[i]["h"] < e[i] and r[i]["c"] < e[i] and r[i - 1]["c"] < e[i - 1]:
            last = i
            yield i


def breakdown_plan(coin: Coin, i: int, kind: str) -> Plan | None:
    """The shared stop and exits, entered by `kind` in (limit, stop, market)."""
    r, a, c = coin.rows, atr_abs(coin, i), coin.rows[i]["c"]
    stop = max(coin.ema20[i], coin.high(i - 2, i + 1)) + BD_STOP_ATR * a
    kw: dict = {}
    if kind == "limit":
        ref = coin.ema20[i]
        kw = {"entry": "limit", "limit_px": ref}
    elif kind == "stop":
        ref = min(x["l"] for x in r[i - BD_LOW_BARS + 1:i + 1]) * (1 - BD_TRIG_BELOW)
        kw = {"entry": "stop", "limit_px": ref, "arm_px": c + BD_BOUNCE_ATR * a}
    else:
        ref = c
    if not a or stop <= ref:
        return None
    tg = r_targets(ref, stop, BD_TARGETS)
    meta = {"stop_atr": (stop - ref) / a, "targets_r": BD_TARGETS}
    return Plan(stop=stop, targets=tuple((px, f) for px, f in tg if px > 0), time_stop=BD_TIME, limit_ttl=BD_TTL,
                be_r=BD_BE, trail_act_r=BD_TRAIL[0], trail_dist_r=BD_TRAIL[1], meta=meta, **kw)


def breakdown_entry(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    for i in breakdown_arms(coin, p["drop"]):
        pl = breakdown_plan(coin, i, p["entry"])
        if pl:
            yield i, pl


# ---------------------------------------------------------------- Q2: exit management on identical signals
LIVE_TRAIL = {"pump": (10.0, 4.0), "breakdown": (5.0, 3.0)}   # setups.py (activation %, trail %)
EXIT_MODES = ("fixed", "be", "trail", "both")
PUMP_FAMILY = {"pump_fade": ("pump_fade", (), "pump"), "pump_fade+discount": ("pump_fade", (discount,), "pump"),
               "post_pump_decay": ("post_pump_decay", (), "pump"), "bounce_fade": ("bounce_fade", (), "breakdown")}


def with_exits(pl: Plan, ref: float, mode: str, live: tuple[float, float]) -> Plan:
    """Replace the plan's breakeven / trailing: be at +1R; trail at the live guardian's % params in R units."""
    risk = (pl.stop - ref) / ref
    be = 1.0 if mode in ("be", "both") else None
    if mode in ("trail", "both") and risk > 0:
        return replace(pl, be_r=be, trail_act_r=live[0] / 100 / risk, trail_dist_r=live[1] / 100 / risk)
    return replace(pl, be_r=be, trail_act_r=None)


def exit_variant(key: str, mode: str) -> Callable[..., Iterable]:
    base, checks, fam = PUMP_FAMILY[key]
    fn = variant(base, checks)

    def run(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
        for i, pl in fn(m, coin, p):
            ref = pl.limit_px if pl.entry == "limit" and pl.limit_px else coin.rows[i]["c"]
            yield i, with_exits(pl, ref, mode, LIVE_TRAIL[fam])
    run.__name__ = f"{key}_{mode}"
    return run


# ---------------------------------------------------------------- Q3: re-entry and funding budget
REENTRY_BARS = 3
FUND_BUDGET_R, FUND_FOLLOW_R = 0.5, 0.5
STOP_REASONS = ("stop", "stop_gap")


def censored(coin: Coin, i: int, pl: Plan) -> bool:
    """The runner's censoring rule (run.Runner.execute)."""
    return i + pl.time_stop + (pl.limit_ttl if pl.entry != "market" else 0) >= len(coin.rows)


def reentry_plan(coin: Coin, res: dict, pl: Plan, buf_atr: float) -> tuple[int, Plan] | None:
    """After a stop-out at the original stop S: the first of the next REENTRY_BARS closes below S re-shorts."""
    r, e, s = coin.rows, res["exit_i"], res["stop0"]
    for k in range(e + 1, min(len(r), e + 1 + REENTRY_BARS)):
        if r[k]["c"] < s:
            if not tradable(coin, k):
                return None
            stop = coin.high(e, k + 1) + buf_atr * atr_abs(coin, k)
            tg = tuple((px, f) for px, f in pl.targets if px < r[k]["c"])
            if not tg:
                return None
            p2 = plan(coin, k, stop, pl.time_stop, targets=tg, be_r=pl.be_r)
            return (k, replace(p2, meta=dict(p2.meta, reentry=True))) if p2 else None
    return None


def reentry_variant(base: str, checks: tuple = ()) -> Callable[..., Iterable]:
    """The base signals plus re-entries. The runner's one-trade-per-coin bookkeeping is replayed here so a re-entry
    only follows a trade the runner will actually take (stop hits do not depend on costs)."""
    fn = variant(base, checks)

    def run(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
        busy = -1
        for i, pl in fn(m, coin, p):
            if i <= busy:
                continue
            yield i, pl
            if censored(coin, i, pl):
                continue
            res = simulate(coin.rows, i, pl, fidx=coin.fidx)
            if not res or not res["filled"]:
                continue
            busy = res["exit_i"]
            if res["reason"] not in STOP_REASONS:
                continue
            re = reentry_plan(coin, res, pl, p["buf_atr"])
            if not re or censored(coin, *re):
                continue
            res2 = simulate(coin.rows, re[0], re[1], fidx=coin.fidx)
            if res2 and res2["filled"]:
                yield re
                busy = res2["exit_i"]
    run.__name__ = f"{base}_reentry"
    return run


def budget_variant(base: str, checks: tuple = ()) -> Callable[..., Iterable]:
    fn = variant(base, checks)

    def run(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
        for i, pl in fn(m, coin, p):
            yield i, replace(pl, fund_budget_r=FUND_BUDGET_R, fund_follow_r=FUND_FOLLOW_R)
    run.__name__ = f"{base}_fundbudget"
    return run


# ---------------------------------------------------------------- the round-2 ledger
def trials_v3() -> list[Trial]:
    out = [Trial("bd_entry", {"drop": d, "entry": k, "time": BD_TIME}, breakdown_entry,
                 notes="round 2 Q1 placebo@fill") for d in BD_DROPS for k in ("limit", "stop", "market")]
    for key, (base, _, _) in PUMP_FAMILY.items():
        cur = "be" if base != "bounce_fade" else None       # pump family bases already run (ii); bounce_fade runs none
        for mode in EXIT_MODES:
            if mode != cur:
                out.append(Trial(f"{key}+exit_{mode}", dict(BASE[base][1]), exit_variant(key, mode),
                                 notes="round 2 Q2"))
    for key, checks in (("pump_fade", ()), ("pump_fade+discount", (discount,))):
        out.append(Trial(f"{key}+reentry", dict(BASE["pump_fade"][1]), reentry_variant("pump_fade", checks),
                         notes="round 2 Q3"))
        out.append(Trial(f"{key}+fundbudget", dict(BASE["pump_fade"][1]), budget_variant("pump_fade", checks),
                         notes="round 2 Q3"))
    return out


def ledger() -> list[Trial]:
    """Rounds 0-1 (53 trials) + round 2: the DSR counts every one of them."""
    return ledger_v2() + trials_v3()


REFERENCE = {   # round-2 comparison -> the ledger trial that already runs that exact configuration
    "pump_fade+exit_be": "pump_fade[buf_atr=1.0,time=12]",
    "post_pump_decay+exit_be": "post_pump_decay[stop_atr=2.0,time=12]",
    "pump_fade+discount+exit_be": "pump_fade+discount[buf_atr=1.0,time=12]",
    "bounce_fade+current": "bounce_fade[drop=-15,time=24]",
}
