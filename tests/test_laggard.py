from shortbot.laggard import accepted_breakdown, outcome, pick_laggards


def bar(c, lo=None, hi=None):
    return [0, c, hi or c * 1.002, lo or c * 0.998, c, 1.0]


def test_accepted_breakdown_needs_two_closes_below_4h_low():
    base = [bar(100)] * 17
    assert accepted_breakdown(base + [bar(99), bar(98.5), bar(98.4)])[0]        # 2 closed below 99.8, +1 open
    assert not accepted_breakdown(base + [bar(99), bar(100), bar(98)])[0]       # only one closed below


def test_pick_laggards_and_outcome():
    mem = [{"symbol": "L", "r4h": -6}, {"symbol": "A", "r4h": -1}, {"symbol": "B", "r4h": -5}, {"symbol": "C", "r4h": 0.5}]
    lag, rest = pick_laggards(mem, "L", -6)
    assert {m["symbol"] for m in lag} == {"A", "C"} and [m["symbol"] for m in rest] == ["B"]
    c = [bar(100 - i * 0.1) for i in range(48)]
    o = outcome(100, c, gap_pct=3)
    assert o["r1h"] > 0 and o["catchup_hit"] and o["catchup_minutes"] > 0
