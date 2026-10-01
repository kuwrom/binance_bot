"""Round-1 fixes (docs/FAILURES.md): pre-registered variants of the round-0 strategies, one change each, plus one
stack. Every threshold below was fixed from the IN-SAMPLE diagnosis (entries before the window midpoint) BEFORE any
out-of-sample run, and every variant is one trial in the ledger (`ledger()` = round 0's 36 + these).

Base param sets are round 0's IS picks: pump_fade(1.0 ATR, 12h), post_pump_decay(2 ATR, 12h),
level_fade(round, market, 24h), bounce_fade(-15%, 24h).

F1 tide veto      no entry while the market squeezes shorts: BTC 24h >= +2% or BTC 4h >= +1% or the coin's sector
                  (median of the other members) 4h >= +1%.
F2 stop floor     the stop sits at least 3 ATR above the entry (p90 of the adverse move IS winners took before T1 in
                  pump_fade); target prices stay where the base plan put them.
F3 taker veto     no entry into aggressive buying: hourly taker buy/sell ratio >= 1.1 or 4h taker imbalance >= +0.05.
F4 discount only  enter only when the perp traded <= -1% under its index in the last 24h, or the last funding print
                  was <= -0.1%, or the symbol is on a 1h funding interval (docs/SCRIPTS.md discount_squeeze).
F5 weekend only   enter only on Saturday/Sunday UTC (learning #6, SCRIPTS.md headline 4).
stack             F1 + F2 + F3 + a $5M 24h-volume floor (round 0's cost finding) on the pump family; F1 + F3 + $5M
                  on level_fade."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime, timezone

from .features import Coin, Market
from .sim import H, Plan
from .strategies import Trial, atr_abs, bounce_fade, level_fade, post_pump_decay, pump_fade, trials

Check = Callable[[Market, Coin, int], bool]          # True = the entry may go ahead

BASE = {"pump_fade": (pump_fade, {"buf_atr": 1.0, "time": 12}),
        "post_pump_decay": (post_pump_decay, {"stop_atr": 2.0, "time": 12}),
        "level_fade": (level_fade, {"level": "round", "entry": "market", "time": 24}),
        "bounce_fade": (bounce_fade, {"drop": -15, "time": 24})}

TIDE_BTC24, TIDE_BTC4, TIDE_SEC4 = 2.0, 1.0, 1.0
TAKER_RATIO, TAKER_IMB = 1.1, 0.05
DISC_PREM, DISC_FUND, DISC_INTERVAL_H = -0.01, -0.001, 1.5
FLOOR_ATR = 3.0
MIN_VOL_STACK = 5e6


# ---------------------------------------------------------------- pure as-of checks (bar i is complete at its close)
def _ret(coin: Coin, ts: int, back: int) -> float | None:
    a, b = coin.close_at(ts - back * H), coin.close_at(ts)
    return (b / a - 1) * 100 if a and b else None


def tide_ok(m: Market, coin: Coin, i: int) -> bool:
    ts = coin.rows[i]["ts"]
    if m.btc and coin is not m.btc:
        b24, b4 = _ret(m.btc, ts, 24), _ret(m.btc, ts, 4)
        if (b24 is not None and b24 >= TIDE_BTC24) or (b4 is not None and b4 >= TIDE_BTC4):
            return False
    s4 = m.sector_ret(coin.sector, ts, 4, exclude=coin.symbol) if coin.sector != "other" else None
    return not (s4 is not None and s4 >= TIDE_SEC4)


def taker_imbalance(rows: list[dict], i: int, n: int = 4) -> float | None:
    """(buy - sell) / total taker quote volume over bars i-n+1..i; tbq = taker BUY quote volume."""
    q = sum(r["qv"] for r in rows[max(0, i - n + 1):i + 1])
    b = sum(r.get("tbq") or 0.0 for r in rows[max(0, i - n + 1):i + 1])
    return (2 * b - q) / q if q else None


def taker_ok(m: Market, coin: Coin, i: int) -> bool:
    t_close = coin.rows[i]["ts"] + H
    d = coin.extra.get("taker_ratio") or {}
    tr = next((d[t_close - k * H] for k in range(3) if (t_close - k * H) in d), None)
    ti = taker_imbalance(coin.rows, i)
    return not ((tr is not None and tr >= TAKER_RATIO) or (ti is not None and ti >= TAKER_IMB))


def funding_interval_h(funding: list, t_close: int) -> float | None:
    """Hours between the last two funding prints known at t_close (8h normally; 1h once Binance caps a symbol)."""
    ts = [t for t, _ in funding if t <= t_close][-2:]
    return (ts[1] - ts[0]) / H if len(ts) == 2 else None


def discount(m: Market, coin: Coin, i: int) -> bool:
    rows, t_close = coin.rows, coin.rows[i]["ts"] + H
    prem = [r["prem"] for r in rows[max(0, i - 23):i + 1] if r.get("prem") is not None]
    if prem and min(prem) <= DISC_PREM:
        return True
    last = next((rate for t, rate in reversed(coin.funding) if t <= t_close), None)
    if last is not None and last <= DISC_FUND:
        return True
    fi = funding_interval_h(coin.funding, t_close)
    return fi is not None and fi <= DISC_INTERVAL_H


def weekend(m: Market, coin: Coin, i: int) -> bool:
    return datetime.fromtimestamp((coin.rows[i]["ts"] + H) / 1000, timezone.utc).weekday() >= 5


def liquid(m: Market, coin: Coin, i: int) -> bool:
    return sum(r["qv"] for r in coin.rows[max(0, i - 23):i + 1]) >= MIN_VOL_STACK


def floor_stop(pl: Plan, coin: Coin, i: int, k_atr: float = FLOOR_ATR) -> Plan:
    """Widen the stop to >= k_atr ATR above the entry reference; targets keep their PRICES. meta is recomputed so
    the placebos inherit the wider stop."""
    ref = pl.limit_px if pl.entry == "limit" and pl.limit_px else coin.rows[i]["c"]
    a = atr_abs(coin, i)
    stop = max(pl.stop, ref + k_atr * a)
    if stop == pl.stop or not a:
        return pl
    meta = dict(pl.meta, stop_atr=(stop - ref) / a,
                targets_r=tuple(((ref - px) / (stop - ref), f) for px, f in pl.targets))
    return replace(pl, stop=stop, meta=meta)


# ---------------------------------------------------------------- variant builder
def variant(base: str, checks: tuple[Check, ...] = (), widen: bool = False) -> Callable[..., Iterable]:
    """The base strategy's signals, dropped when any check fails (a veto never searches for a later entry), with an
    optional stop floor."""
    fn, _ = BASE[base]

    def run(m: Market, coin: Coin, p: dict) -> Iterable[tuple[int, Plan]]:
        for i, pl in fn(m, coin, p):
            if all(c(m, coin, i) for c in checks):
                yield i, (floor_stop(pl, coin, i) if widen else pl)
    run.__name__ = f"{base}_v2"
    return run


FIXES: dict[str, tuple[tuple[Check, ...], bool, tuple[str, ...]]] = {
    # name: (checks, widen stop, strategies it is applied to - chosen from the IS diagnosis, see docs/FAILURES.md)
    "tide": ((tide_ok,), False, ("pump_fade", "post_pump_decay", "level_fade")),
    "floor3atr": ((), True, ("pump_fade", "post_pump_decay")),
    "taker": ((taker_ok,), False, ("pump_fade", "level_fade", "bounce_fade")),
    "discount": ((discount,), False, ("pump_fade", "post_pump_decay", "level_fade")),
    "weekend": ((weekend,), False, ("pump_fade", "post_pump_decay", "level_fade")),
    "stack": ((tide_ok, taker_ok, liquid), True, ("pump_fade", "post_pump_decay")),
    "stack_nofloor": ((tide_ok, taker_ok, liquid), False, ("level_fade",)),
}


def trials_v2() -> list[Trial]:
    out = []
    for fix, (checks, widen, bases) in FIXES.items():
        for b in bases:
            out.append(Trial(f"{b}+{fix}", dict(BASE[b][1]), variant(b, checks, widen), notes=f"round 1: {fix}"))
    return out


def ledger() -> list[Trial]:
    """Round 0's pre-registered 36 trials + round 1's variants: the DSR counts all of them."""
    return trials() + trials_v2()
