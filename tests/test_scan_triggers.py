"""scan_triggers: each rule fires on its case and stays quiet on a calm tape; select ranks, caps and carries."""

import math
import random

from shortbot import scan_triggers as T

M15, H1 = T.M15, T.H1
NOW = 1_759_400_000_000 // H1 * H1 + 2 * M15   # 30 min past an hour: no hourly standing states


def tape(n=120, drift=0.0, vol=0.002, seed=1, volume=100.0, end=NOW):
    """n 15m bars ending with a forming bar at `end`; log returns ~ N(drift, vol)."""
    rnd, px, out = random.Random(seed), 100.0, []
    start = end - (n - 1) * M15
    for i in range(n):
        o = px
        px *= math.exp(rnd.gauss(drift, vol))
        out.append([start + i * M15, o, max(o, px) * 1.001, min(o, px) * 0.999, px, volume])
    return out


def rules(trigs, sym=None):
    return {t.rule for t in trigs if sym is None or t.symbol == sym}


def test_random_walk_false_positive_rate():
    """Gaussian walks cross |z| >= 2 ~4.6% of the time per horizon; flat volume never surges."""
    hits = [rules(T.evaluate({"C": tape(seed=i)}, {}, {}, {}, NOW)) for i in range(300)]
    assert 0.02 <= sum(bool(h) for h in hits) / 300 <= 0.15
    assert all(h <= {"move_1h", "move_4h"} for h in hits)


def test_move_and_volume_surge():
    bars = tape()
    for b in bars[-6:-1]:                      # the last closed hour: +1.5% a bar on 5x volume
        b[4] = b[1] * 1.015
        b[5] = 500.0
    for i in range(len(bars) - 5, len(bars)):
        bars[i][1] = bars[i - 1][4]
        bars[i][4] = bars[i][1] * (1.015 if i < len(bars) - 1 else 1.0)
    got = T.evaluate({"PUMPUSDT": bars}, {}, {}, {}, NOW)
    assert {"move_1h", "volume_surge"} <= rules(got)
    assert T.move_z(T.closed(bars, NOW))["z1h"] > 2


def test_new_lower_high_fires_once_after_a_pump():
    bars = tape(vol=0.0005)
    cb = T.closed(bars, NOW)
    for b in cb[-8:-4]:
        b[2] = 120.0                           # the pump's highs
    for b in cb[-4:]:
        b[2] = 110.0
    cb[-5][2] = 125.0                          # the previous window's peak; the newest bar turns it lower
    assert T.new_lower_high_after_pump(cb, 25.0)
    assert not T.new_lower_high_after_pump(cb, 3.0)          # no pump, no trigger


def test_funding_and_premium_extremes_need_the_decile_and_the_floor():
    mark = {f"C{i}USDT": {"funding": 0.0001, "premium": 0.0001} for i in range(30)}
    mark["HOTUSDT"] = {"funding": 0.002, "premium": 0.003}
    mark["COLDUSDT"] = {"funding": -0.001, "premium": -0.0008}
    got = T.cross_extremes(mark, {"COLDUSDT": 4})
    assert rules(got, "HOTUSDT") == {"funding_hi", "premium_hi"}
    assert rules(got, "COLDUSDT") == {"funding_lo", "premium_lo"}    # -0.1% per 4h = -0.2%/8h
    assert not rules(got, "C1USDT")                                    # in the top decile by ties, below the floor
    assert [t.strength for t in got if t.rule == "funding_hi"] == [4.0]


def test_liquidation_burst_uses_the_tape_rule():
    rows = [{"ts_ms": NOW - 60_000, "side": "long_liquidated", "notional": 400_000},
            {"ts_ms": NOW - 40 * 60_000, "side": "long_liquidated", "notional": 30_000},
            {"ts_ms": NOW - 60_000, "side": "short_liquidated", "notional": 20_000}]
    got = T.liq_bursts({"ARB/USDT:USDT": rows}, NOW)
    assert [(t.symbol, t.rule) for t in got] == [("ARBUSDT", "liq_long_burst")]
    assert got[0].strength == round(400_000 / T.LIQ_BURST_MIN_USD, 2)


def test_fast_events_premium_spike_and_btc_impulse():
    mark = {f"C{i}USDT": {"premium": 0.0001 * (i % 3)} for i in range(30)}
    mark["SPIKEUSDT"] = {"premium": 0.01}
    btc = tape(vol=0.0005)
    btc[-1][4] = btc[-2][4] * 1.01
    got = T.fast_events({"BTC/USDT:USDT": btc}, mark, {}, NOW)
    assert ("SPIKEUSDT", "premium_spike") in {(t.symbol, t.rule) for t in got}
    assert ("BTCUSDT", "btc_impulse") in {(t.symbol, t.rule) for t in got}


def test_standing_states_only_at_the_hour_and_events_always():
    at_hour = NOW - 2 * M15 + 60_000
    ages, ev = {"NEWUSDT": 5.0, "OLDUSDT": 300.0}, {"UNLUSDT": "unlock 4.1% in 6d"}
    assert rules(T.evaluate({}, {}, {}, {}, at_hour, ages, ev)) == {"new_listing", "event"}
    assert rules(T.evaluate({}, {}, {}, {}, NOW, ages, ev)) == {"event"}


def test_select_merges_ranks_caps_and_carries():
    trig = [T.Trigger("A", "move_1h", 1.2, ""), T.Trigger("A", "volume_surge", 1.5, ""),
            T.Trigger("B", "funding_hi", 2.0, ""), T.Trigger("C", "event", 1.0, "")]
    out = T.select(trig, carried=[{"symbol": "D", "strength": 3.0, "rules": ["x"], "details": []},
                                  {"symbol": "E", "strength": 1.5, "rules": ["x"], "details": []}], cap=2)
    assert [c["symbol"] for c in out["selected"]] == ["A", "B"] and out["selected"][0]["strength"] == 2.7
    assert [c["symbol"] for c in out["queued"]] == ["D", "C"]          # E halved below 1: dropped
    assert out["counts"] == {"move_1h": 1, "volume_surge": 1, "funding_hi": 1, "event": 1}


def test_select_skips_coins_with_no_new_rule():
    trig = [T.Trigger("A", "move_4h", 1.2, ""), T.Trigger("B", "move_4h", 1.1, ""), T.Trigger("B", "liq_long_burst", 2, "")]
    out = T.select(trig, recent={"A": {"move_4h"}, "B": {"move_4h"}})
    assert out["skipped"] == ["A"] and [c["symbol"] for c in out["selected"]] == ["B"]
    assert out["counts"]["move_4h"] == 2
