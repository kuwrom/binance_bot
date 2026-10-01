"""Round-3 variants (docs/FAILURES.md "Round 3"): are the desk's live VETOES right? Every rule below was written
down before the out-of-sample run; every variant is one trial in the ledger (`ledger()` = rounds 0-2's 76 + these).

Each veto is tested as a PARTITION of a base book's signals: "blocked" = the base signals the veto fires on (what
inverting the veto would trade), "allowed" = the base with the veto on (what the desk trades). Bases: pump_fade
(1 ATR, 12h), pump_fade+discount, level_fade (round, market, 24h). A veto is right if its blocked trades do worse.
All checks are as-of the close of the signal bar i (bar i complete); thresholds are the LIVE code's.

V1 premium / perp-led  (TAPE #7 'perp-led = fade' vs SCRIPTS: premium > +0.3% at entry was the worst fade)
   premium buckets on the perp premium index close of bar i: <= -1%, -1..0, 0..+0.3%, > +0.3%.
   blocked = premium > +0.3%.  perp-led (signals/tape.perp_vs_spot points): 6h perp return - 6h spot return
   > 0.3 pp and the premium widened > 10 bp over those 6h (needs spot).
V2 negative funding    (signals/core.funding): last funding print < 0, or the symbol is on a 1h interval.
V3 spot-led rally      (signals/tape.perp_vs_spot / tape.spot_led_rally): 6h perp return > 0, 6h spot return >=
   it, 6h spot taker imbalance > 0.10 and premium <= 0 (needs spot).
V4 liquidation flush   (TAPE #9 proxy, no WS data): a bar with 1h return <= -FLUSH_ATR x ATR% and OI contracts
   (OI USD / close) down >= 3% in the hour. Veto: a flush in bars i-2..i. Event studies (all tradable coins):
   flush_short   short the close of a flush bar (continuation vs the 'don't chase' bounce)
   squeeze_short short the close of a squeeze bar (1h return >= +FLUSH_ATR x ATR%, OI contracts down >= 3%)
   plan = the random baseline's: stop 2 ATR above the close, half at 1R / rest at 2R, breakeven 1R, 24h.
V5 taker-buy burst     (signals/tape.taker_imbalance): bar i's taker imbalance z-score > 2 vs the prior 23 hours'
   (population sd, as tape_math.ti_zscore) with price up over the hour (close > open)."""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable
from typing import Any

from .features import Coin, Market
from .sim import H, Plan
from .strategies import WARMUP, Trial, atr_abs, plan, tradable
from .strategies_v2 import BASE, discount, funding_interval_h, variant
from .strategies_v3 import ledger as ledger_v3

Check = Callable[[Market, Coin, int], bool]

PREM_EDGES = (-0.01, 0.0, 0.003)            # fraction of price: -1%, 0, +0.3%
PREM_BUCKETS = ("le-1%", "-1..0%", "0..+0.3%", "gt+0.3%")
PERP_LED_GAP_PP, BASIS_WIDEN = 0.3, 0.0010  # signals/tape.py PERP_LED_GAP_PP, BASIS_WIDEN_BP (10 bp)
SPOT_TI_BUY = 0.10                          # signals/tape.py SPOT_TI_BUY
FLOW_BARS = 6                               # the live 7-candle window = 6 closed hours + the forming one
FUND_INTERVAL_1H = 1.5
FLUSH_ATR, FLUSH_OI = 2.0, -0.03            # 1h move >= 2 ATR%, OI contracts <= -3% in the hour
FLUSH_LOOK = 3                              # veto: a flush in the last 3 bars (i-2..i)
TI_Z, TI_HIST = 2.0, 23                     # live: z > 2 of the last hour's TI vs the prior 23 hourly windows
EVENT_STOP_ATR, EVENT_TIME, EVENT_COOLDOWN = 2.0, 24, 6
BASES = {"pump_fade": ("pump_fade", ()), "pump_fade+discount": ("pump_fade", (discount,)),
         "level_fade": ("level_fade", ())}


# ---------------------------------------------------------------- as-of features
def premium(coin: Coin, i: int) -> float | None:
    return coin.rows[i].get("prem")


def prem_bucket(coin: Coin, i: int) -> str | None:
    p = premium(coin, i)
    if p is None:
        return None
    return PREM_BUCKETS[sum(p > e for e in PREM_EDGES)]


def spot_flow(coin: Coin, i: int, n: int = FLOW_BARS) -> dict[str, float] | None:
    """6h perp / spot returns (close of bar i vs close of bar i-n), spot taker imbalance over bars i-n+1..i and the
    premium change; None without spot data for the whole window."""
    r = coin.rows
    if i < n:
        return None
    sp0, sp1 = r[i - n].get("spot_c"), r[i].get("spot_c")
    if not sp0 or not sp1:
        return None
    tb = coin.extra.get("spot_tbq") or {}
    q = b = 0.0
    for k in range(i - n + 1, i + 1):
        sq, st = r[k].get("spot_qv"), tb.get(r[k]["ts"])
        if sq is None or st is None:
            return None
        q, b = q + sq, b + st
    p0, p1 = r[i - n].get("prem"), r[i].get("prem")
    return {"perp_ret": (r[i]["c"] / r[i - n]["c"] - 1) * 100, "spot_ret": (sp1 / sp0 - 1) * 100,
            "spot_ti": (2 * b - q) / q if q else None, "prem": p1,
            "prem_chg": p1 - p0 if p0 is not None and p1 is not None else None}


def perp_led(m: Market, coin: Coin, i: int) -> bool:
    f = spot_flow(coin, i)
    return bool(f and f["perp_ret"] - f["spot_ret"] > PERP_LED_GAP_PP and f["prem_chg"] is not None
                and f["prem_chg"] > BASIS_WIDEN)


def spot_led(m: Market, coin: Coin, i: int) -> bool:
    f = spot_flow(coin, i)
    return bool(f and f["perp_ret"] > 0 and f["spot_ret"] >= f["perp_ret"] and f["spot_ti"] is not None
                and f["spot_ti"] > SPOT_TI_BUY and f["prem"] is not None and f["prem"] <= 0)


def last_funding(coin: Coin, i: int) -> float | None:
    t_close = coin.rows[i]["ts"] + H
    return next((rate for t, rate in reversed(coin.funding) if t <= t_close), None)


def neg_funding(m: Market, coin: Coin, i: int) -> bool:
    f = last_funding(coin, i)
    fi = funding_interval_h(coin.funding, coin.rows[i]["ts"] + H)
    return (f is not None and f < 0) or (fi is not None and fi <= FUND_INTERVAL_1H)


def oi_contract_chg(coin: Coin, k: int) -> float | None:
    """OI contracts change over bar k: OI USD (end of hour) / close."""
    if k < 1:
        return None
    a, b = coin.rows[k - 1].get("oi"), coin.rows[k].get("oi")
    if not a or not b:
        return None
    return (b / coin.rows[k]["c"]) / (a / coin.rows[k - 1]["c"]) - 1


def oi_move(coin: Coin, k: int, sign: int) -> bool:
    """sign -1: long flush (price down >= FLUSH_ATR ATR%, OI down >= 3%); +1: short squeeze (price up)."""
    if k < 2:
        return False
    ret, atr = coin.ret(k, 1), coin.atr[k - 1]
    d = oi_contract_chg(coin, k)
    return (ret is not None and d is not None and atr > 0 and sign * ret >= FLUSH_ATR * atr
            and d <= FLUSH_OI)


def flush_recent(m: Market, coin: Coin, i: int) -> bool:
    return any(oi_move(coin, k, -1) for k in range(max(1, i - FLUSH_LOOK + 1), i + 1))


def ti_z(coin: Coin, i: int, hist: int = TI_HIST) -> float | None:
    if i < hist:
        return None
    tis = []
    for k in range(i - hist, i + 1):
        q = coin.rows[k]["qv"]
        if not q:
            return None
        tis.append((2 * (coin.rows[k].get("tbq") or 0.0) - q) / q)
    now, h = tis[-1], tis[:-1]
    sd = statistics.pstdev(h)
    return (now - statistics.fmean(h)) / sd if sd else None


def taker_burst(m: Market, coin: Coin, i: int) -> bool:
    z = ti_z(coin, i)
    return z is not None and z > TI_Z and coin.rows[i]["c"] > coin.rows[i]["o"]


def negate(c: Check) -> Check:
    def f(m: Market, coin: Coin, i: int) -> bool:
        return not c(m, coin, i)
    f.__name__ = f"not_{c.__name__}"
    return f


def in_bucket(name: str) -> Check:
    def f(m: Market, coin: Coin, i: int) -> bool:
        return prem_bucket(coin, i) == name
    f.__name__ = f"prem_{name}"
    return f


def prem_ok(m: Market, coin: Coin, i: int) -> bool:
    """premium <= +0.3% (a missing premium is allowed: the live code never vetoes on missing tape data)."""
    return prem_bucket(coin, i) != PREM_BUCKETS[-1]


# ---------------------------------------------------------------- event studies (V4)
def oi_event_short(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
    last = -10 ** 9
    for i in range(WARMUP, len(coin.rows)):
        if i - last < EVENT_COOLDOWN or not oi_move(coin, i, p["sign"]) or not tradable(coin, i):
            continue
        pl = plan(coin, i, coin.rows[i]["c"] + EVENT_STOP_ATR * atr_abs(coin, i), EVENT_TIME)
        if pl:
            last = i
            yield i, pl


# ---------------------------------------------------------------- data attach (spot taker buy volume)
def attach_spot(m: Market, raw_coins: list[dict[str, Any]]) -> int:
    """features.Coin keeps only spot close / volume; the spot TI needs the spot taker-buy volume."""
    n = 0
    for rc in raw_coins:
        c = m.coins.get(rc["symbol"])
        if c and rc.get("spot"):
            c.extra["spot_tbq"] = {b["ts"]: float(b["tbq"]) for b in rc["spot"] if b.get("tbq") is not None}
            n += 1
    return n


# ---------------------------------------------------------------- the round-3 ledger
# veto: (checks for BLOCKED, checks for ALLOWED)
VETOES: dict[str, tuple[tuple[Check, ...], tuple[Check, ...]]] = {
    "prem_hi": ((in_bucket(PREM_BUCKETS[-1]),), (prem_ok,)),
    "perp_led": ((perp_led,), (negate(perp_led),)),        # a points bonus, not a veto: "blocked" = perp-led
    "neg_funding": ((neg_funding,), (negate(neg_funding),)),
    "spot_led": ((spot_led,), (negate(spot_led),)),
    "flush": ((flush_recent,), (negate(flush_recent),)),
    "taker_burst": ((taker_burst,), (negate(taker_burst),)),
}


def _trial(key: str, tag: str, checks: tuple[Check, ...]) -> Trial:
    base, pre = BASES[key]
    return Trial(f"{key}|{tag}", dict(BASE[base][1]), variant(base, pre + checks), notes=f"round 3 {tag}")


def trials_v4() -> list[Trial]:
    out = []
    for key in BASES:
        out += [_trial(key, f"prem:{b}", (in_bucket(b),)) for b in PREM_BUCKETS[:-1]]
        for v, (blocked, allowed) in VETOES.items():
            out.append(_trial(key, f"{v}:blocked", blocked))
            out.append(_trial(key, f"{v}:allowed", allowed))
    for name, sign in (("flush_short", -1), ("squeeze_short", 1)):
        out.append(Trial(name, {"sign": sign, "time": EVENT_TIME}, oi_event_short, notes="round 3 V4 event"))
    return out


def ledger() -> list[Trial]:
    """Rounds 0-2 (76 trials) + round 3: the DSR counts every one of them."""
    return ledger_v3() + trials_v4()


def base_name(key: str) -> str:
    """The ledger trial that already runs the unsplit base book."""
    return {"pump_fade": "pump_fade[buf_atr=1.0,time=12]", "pump_fade+discount": "pump_fade+discount[buf_atr=1.0,time=12]",
            "level_fade": "level_fade[level=round,entry=market,time=24]"}[key]
