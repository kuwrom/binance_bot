import random

from shortbot.backtest.features import Market
from shortbot.backtest.strategies_flow import per8h, trials_flow, zscore

H = 3_600_000
T0 = 1_780_000_000_000 // H * H


def _market(n=420):
    """TRAP: falls 6% over 24h to bar 300 while OI rises 15% and funding stays positive (long trap).
    FUEL: same price/OI path but funding negative, then reclaims (squeeze fuel).
    CROWD: OI +50% over a week with funding 0.06%/8h, a 4h lower high, then breaks down at bar 380.
    RETAIL: retail account ratio spikes long at bar 250 while top traders cut and takers sell.
    RSHORT: the mirror at bar 260 (retail short, top traders adding, takers buying, price rising).
    QUIET: control."""
    out = []
    for sym in ("TRAP", "FUEL", "CROWD", "RETAIL", "RSHORT", "QUIET"):
        rng, px, perp, oi, fund = random.Random(sym), 100.0, [], {}, []
        ls_g, ls_t, tk = {}, {}, {}
        for k in range(n):
            o, step = px, rng.gauss(0, 0.002)
            if sym in ("TRAP", "FUEL") and 276 <= k < 300:
                step = -0.0026
            if sym == "FUEL" and 300 <= k < 312:
                step = 0.006
            if sym == "RSHORT" and 255 <= k <= 260:
                step = 0.003
            if sym == "CROWD" and 372 <= k < 376:
                step = 0.002
            if sym == "CROWD" and 376 <= k < 381:
                step = -0.004
            px = o * (1 + step)
            red = sym == "TRAP" and k == 299
            perp.append({"ts": T0 + k * H, "o": o * (1.001 if red else 1), "h": max(o, px) * 1.002,
                         "l": min(o, px) * 0.998, "c": px, "qv": 3e6, "n": 10, "tbq": 1.5e6})
            oi_lvl = 5e6
            if sym in ("TRAP", "FUEL") and k >= 276:
                oi_lvl *= 1.15
            if sym == "CROWD":
                oi_lvl *= 1.5 if k >= 300 else 1.0
            oi[T0 + (k + 1) * H] = oi_lvl
            if k % 8 == 0:
                rate = {"TRAP": 0.0002, "FUEL": -0.0002, "CROWD": 0.0006}.get(sym, 0.00005)
                fund.append([T0 + k * H, rate])
            g = 1.0 + rng.gauss(0, 0.02)
            if sym == "RETAIL" and k == 250:
                g = 1.6
            if sym == "RSHORT" and k == 260:
                g = 0.4
            ls_g[T0 + (k + 1) * H] = g
            ls_t[T0 + (k + 1) * H] = 1.2 - (0.2 if sym == "RETAIL" and k >= 240 else 0.0) \
                + (0.2 if sym == "RSHORT" and k >= 250 else 0.0)
            tk[T0 + (k + 1) * H] = (0.8 if sym == "RETAIL" and 248 <= k <= 250 else
                                    1.2 if sym == "RSHORT" and 258 <= k <= 260 else 1.0)
        out.append({"symbol": f"{sym}/USDT:USDT", "onboard_ms": 1, "tags": [], "perp": perp, "oi": oi,
                    "premium": {}, "funding": fund, "spot": [], "ls_top": ls_t, "ls_global": ls_g,
                    "taker_ratio": tk})
    return out


def _cut(coins, cut_ts):
    end = cut_ts + H
    keep = lambda d: {t: v for t, v in d.items() if t <= end}  # noqa: E731
    return [{**c, "perp": [b for b in c["perp"] if b["ts"] <= cut_ts], "oi": keep(c["oi"]),
             "funding": [f for f in c["funding"] if f[0] <= end], "ls_top": keep(c["ls_top"]),
             "ls_global": keep(c["ls_global"]), "taker_ratio": keep(c["taker_ratio"])} for c in coins]


def test_flow_strategies_fire_on_their_scenarios_without_look_ahead():
    coins = _market()
    full, fired = Market(coins), {}
    for t in trials_flow():
        for s, c in full.coins.items():
            for i, p in t.fn(full, c, t.params):
                side_ok = (p.stop < c.rows[i]["c"]) if p.side == "long" else (p.stop > c.rows[i]["c"])
                assert side_ok, (t.name, s)
                fired.setdefault(t.strategy, set()).add(s.split("/")[0])
    assert fired.get("long_trap_short") == {"TRAP"}
    assert fired.get("squeeze_fuel_long") == {"FUEL"}
    assert fired.get("crowded_unwind_short") == {"CROWD"}
    assert fired.get("ls_contrarian_short") == {"RETAIL"}
    assert fired.get("ls_contrarian_long") == {"RSHORT"}
    assert "QUIET" not in set().union(*fired.values())
    for cut_i in (255, 305, 385):
        cut = Market(_cut(coins, T0 + cut_i * H))
        for t in trials_flow():
            a = {(s, i, p.stop) for s, c in full.coins.items() for i, p in t.fn(full, c, t.params) if i <= cut_i}
            b = {(s, i, p.stop) for s, c in cut.coins.items() for i, p in t.fn(cut, c, t.params)}
            assert a == b, (t.name, cut_i)


def test_funding_is_normalised_to_8h_and_zscore_uses_only_the_past():
    m = Market(_market())
    c = m.coins["CROWD/USDT:USDT"]
    assert per8h(c, 300, 3) == [0.0006, 0.0006, 0.0006]
    c.funding = [[T0 + k * H, 0.0001] for k in range(0, 400)]           # hourly prints
    assert per8h(c, 300, 2) == [0.0008, 0.0008]
    xs = [1.0] * 200 + [5.0]
    assert zscore(xs, 200) is None                                        # zero variance in the past window
    xs = [1.0 + (k % 2) * 0.1 for k in range(200)] + [9.0, -50.0]
    assert zscore(xs, 200) > 10 and zscore(xs[:201], 200) == zscore(xs, 200)
