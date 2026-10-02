"""Synthetic replay cache for tests (task #29): 3 days of 1m bars around a scan moment T, with data AFTER T on purpose
(no-look-ahead tests poison it)."""

from __future__ import annotations

import math

from shortbot.backtest import replay_bars as RB
from shortbot.backtest.replay_store import Store, daily_from_inputs

MIN, HOUR, DAY = RB.MIN, RB.HOUR, RB.DAY
T0 = 1_790_208_000_000                                  # 2026-09-25T00:00Z
T = T0 + 2 * DAY + 14 * HOUR + 36 * MIN + 40_000          # 2026-09-27T14:36:40Z
END = T0 + 3 * DAY


def path(sym: str, i: int) -> float:
    x = i / 60
    if sym in ("AAAUSDT", "DEMUSDT"):                    # flat, +40% pump from h36 to h48, then fades
        base = 1.0 if x < 36 else 1.0 + 0.4 * min(x - 36, 12) / 12 - (0.01 * (x - 48) if x > 48 else 0)
    elif sym == "BBBUSDT":
        base = 2.0 * (1 - 0.25 * x / 72)
    elif sym == "BTCUSDT":
        base = 60_000.0
    else:
        base = 0.5
    return base * (1 + 0.002 * math.sin(i / 7))


def bar(sym: str, i: int, scale: float = 1.0) -> tuple:
    o, c = path(sym, i), path(sym, i + 1)
    v = (5_000.0 if sym != "BTCUSDT" else 2.0) * (3 if 36 * 60 <= i < 48 * 60 and sym == "AAAUSDT" else 1)
    px = (o + c) / 2 * scale
    return (T0 + i * MIN, o * scale, max(o, c) * scale * 1.001, min(o, c) * scale * 0.999, c * scale, v, v * px, 40,
            0.55 * v, 0.55 * v * px)


META = {
    "AAAUSDT": {"onboard_ms": T0 - 400 * DAY, "delivery_ms": None, "tags": ["AI"], "demo": True,
                "spot": ["AAAUSDT", 1.0]},
    "BBBUSDT": {"onboard_ms": T0 - 20 * DAY, "delivery_ms": T + 2 * DAY, "tags": [], "demo": True, "spot": None},
    "BTCUSDT": {"onboard_ms": T0 - 2000 * DAY, "delivery_ms": None, "tags": [], "demo": True, "spot": ["BTCUSDT", 1.0]},
    "NEWUSDT": {"onboard_ms": T + HOUR, "delivery_ms": None, "tags": [], "demo": True, "spot": None},
    "OLDUSDT": {"onboard_ms": T0 - 900 * DAY, "delivery_ms": T0 + DAY, "tags": [], "demo": True, "spot": None},
    "DEMUSDT": {"onboard_ms": T0 - 400 * DAY, "delivery_ms": None, "tags": [], "demo": False, "spot": None},
}


def build(p) -> Store:
    s = Store(p)
    meta = {}
    for sym, m in META.items():
        meta[sym] = {**m, "symbol": f"{sym[:-4]}/USDT:USDT", "min_notional": 5.0}
        lo = max(0, (m["onboard_ms"] - T0) // MIN)
        hi = (min(m["delivery_ms"], END) - T0) // MIN if m["delivery_ms"] else 3 * 1440
        s.put_bars("perp", sym, [bar(sym, i) for i in range(lo, hi)])
        if sym in ("AAAUSDT", "BBBUSDT", "BTCUSDT"):
            s.put_bars("mark", sym, [bar(sym, i, 1.0001) for i in range(lo, hi)])
            s.put_bars("prem", sym, [(T0 + i * MIN, 0, 0, 0, 0.0002, 0, 0, 0, 0, 0) for i in range(lo, hi)])
            s.put_funding(sym, [(T0 - 10 * DAY + k * 8 * HOUR, 0.0001) for k in range(40)])
            s.put_metrics(sym, [{"ts": T0 + k * 5 * MIN, "oi": 1e6 * (1 + k / 2000), "oi_val": 3e7 * (1 + k / 2000),
                                 "ls_top": 1.2, "ls_top_acct": 1.1, "ls_global": 1.5, "taker": 1.0}
                                for k in range(3 * 288)])
        if m["spot"]:
            s.put_bars("spot", m["spot"][0], [bar(sym, i, 0.999) for i in range(lo, hi)])
    s.put_meta(meta)
    s.derive_hourly("perp")
    daily_from_inputs(s, list(META), None, None, T0)
    return s


def poison_after(s: Store, t_ms: int) -> None:
    """Overwrite everything at or after t with absurd values: a replay that reads it has look-ahead."""
    for table in ("bars", "bars_h"):
        s.db.execute(f"UPDATE {table} SET o=o*50, h=h*60, l=l*40, c=c*55, v=v*9, qv=qv*9, tbv=0 WHERE ts>=?",
                     (t_ms - (HOUR if table == 'bars_h' else MIN) + 1,))   # every bar not closed by t
    s.db.execute("UPDATE bars_d SET c=c*55, h=h*60 WHERE ts + 86400000 > ?", (t_ms,))
    s.db.execute("UPDATE metrics SET oi=oi*50, oi_val=oi_val*50, ls_top=9 WHERE ts>?", (t_ms,))
    s.db.execute("UPDATE funding SET rate=0.05 WHERE ts>?", (t_ms,))
    s.db.commit()
