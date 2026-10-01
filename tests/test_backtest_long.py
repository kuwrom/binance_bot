import random

import pytest

from shortbot.backtest.sim import COSTS, FREE, Plan, simulate

H = 3_600_000


def bar(k, o, h, l, c, qv=2e7):
    return {"ts": k * H, "o": o, "h": h, "l": l, "c": c, "qv": qv}


def flat(n=30, px=100.0):
    return [bar(k, px, px * 1.001, px * 0.999, px) for k in range(n)]


def mirror_rows(rows, c):
    return [{**r, "o": 2 * c - r["o"], "h": 2 * c - r["l"], "l": 2 * c - r["h"], "c": 2 * c - r["c"]} for r in rows]


def test_a_long_is_the_mirror_image_of_a_short_without_costs():
    """Reflect every price around the signal close: a long on P and a short on 2c - P must agree exactly (fills,
    exits, R) for market / limit / stop entries, targets, breakeven and time stops. Trailing is multiplicative
    (a % of the best price), so it is the one rule that is not mirror-exact and is left out here."""
    rnd = random.Random(3)
    checked = 0
    for _ in range(600):
        px, rows = 100.0, []
        for k in range(50):
            o = px
            c = o * (1 + rnd.gauss(0, 0.02))
            h, l = max(o, c) * (1 + abs(rnd.gauss(0, 0.01))), min(o, c) * (1 - abs(rnd.gauss(0, 0.01)))
            rows.append(bar(k, o, h, l, c))
            px = c
        i = rnd.randint(20, 40)
        c = rows[i]["c"]
        kind = rnd.choice(["market", "limit", "stop"])
        stop_d, tps = rnd.uniform(0.01, 0.06), sorted(rnd.uniform(0.01, 0.08) for _ in range(2))
        lim_d = rnd.uniform(0.001, 0.02)
        common = dict(time_stop=rnd.randint(1, 12), entry=kind, be_r=rnd.choice([None, 0.5, 1.0]))
        long_ = Plan(side="long", stop=c * (1 - stop_d), targets=tuple((c * (1 + t), 0.4) for t in tps),
                     limit_px=None if kind == "market" else c * (1 - lim_d) if kind == "limit" else c * (1 + lim_d),
                     **common)
        short = Plan(stop=2 * c - long_.stop, targets=tuple((2 * c - p, f) for p, f in long_.targets),
                     limit_px=None if long_.limit_px is None else 2 * c - long_.limit_px, **common)
        a = simulate(rows, i, long_, costs=FREE)
        b = simulate(mirror_rows(rows, c), i, short, costs=FREE)
        assert (a is None) == (b is None)
        if a is None or not a["filled"]:
            assert a == b or (a and b and a["filled"] == b["filled"])
            continue
        checked += 1
        for k in ("entry_i", "exit_i", "reason", "bars"):
            assert a[k] == b[k], k
        for k in ("r", "mfe_r", "mae_r"):                         # R = price move / stop distance: mirror-exact
            assert a[k] == pytest.approx(b[k], abs=1e-9), k
        if kind == "market":                                      # % figures divide by the entry price, which only
            for k in ("pnl_pct", "risk_pct"):                     # mirrors onto itself for a fill at the close
                assert a[k] == pytest.approx(b[k], abs=1e-9), k
    assert checked > 300


def test_long_market_entry_buys_above_the_close_and_pays_positive_funding():
    rows = flat()
    funding = [(k * H, 0.001) for k in range(0, 30, 8)]
    r = simulate(rows, 5, Plan(side="long", stop=95.0, time_stop=20), funding, COSTS)
    assert r["side"] == "long" and r["entry_px"] > 100.0          # bought at close + slippage
    assert r["funding_pct"] < 0                                   # a long PAYS positive funding
    assert r["reason"] == "time" and r["pnl_pct"] < 0


def test_long_stop_below_entry_gaps_and_targets_above_fill():
    rows = flat()
    rows[8] = bar(8, 94.0, 95.0, 93.0, 94.5)                      # opens below the 96 stop
    r = simulate(rows, 5, Plan(side="long", stop=96.0, time_stop=20), costs=FREE)
    assert r["reason"] == "stop_gap" and r["r"] == pytest.approx(-1.5)   # filled at the 94 open: -6% / 4% risk
    up = flat()
    up[7] = bar(7, 100.0, 106.0, 99.5, 105.0)
    t = simulate(up, 5, Plan(side="long", stop=96.0, targets=((104.0, 1.0),), time_stop=20), costs=FREE)
    assert t["reason"] == "target" and t["r"] == pytest.approx(1.0)


def test_long_trailing_stop_follows_the_high():
    rows = flat()
    rows[7] = bar(7, 100.0, 110.0, 99.8, 109.0)                   # MFE 10% = 2.5R on a 4% risk
    rows[8] = bar(8, 109.0, 109.5, 100.0, 101.0)                  # falls back through the trail
    r = simulate(rows, 5, Plan(side="long", stop=96.0, trail_act_r=1.0, trail_dist_r=1.0, time_stop=20),
                 costs=FREE)
    assert r["reason"] == "trail" and r["r"] == pytest.approx((110.0 * 0.96 - 100.0) / 4.0)


def test_invalid_long_plans_are_rejected():
    rows = flat()
    assert simulate(rows, 5, Plan(side="long", stop=101.0), costs=FREE) is None            # stop above entry
    assert simulate(rows, 5, Plan(side="long", stop=95.0, entry="limit", limit_px=101.0)) is None   # buy limit above
    assert simulate(rows, 5, Plan(side="sideways", stop=95.0)) is None
