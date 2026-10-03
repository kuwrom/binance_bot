"""Coin memory's measurements (coin_stats.py) on synthetic bars: swings, the 15m regime, a value against the coin's own
days, acceptance, each event rule and #81's break score (two or more checks = a real break, one alone = noise)."""

import pytest

from shortbot import coin_stats as C

M5, M15 = 300_000, 900_000
NOW = 1_790_000_000_000 // M15 * M15 + 7 * 60_000    # 7 min into a 15m slot


def bars_from(closes, now=NOW, step=M5, wick=0.0):
    """[ts, o, h, l, c, v] ending in the forming bar at `now`; o = the previous close."""
    t0 = now // step * step - (len(closes) - 1) * step
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append([t0 + i * step, prev, max(prev, c) * (1 + wick), min(prev, c) * (1 - wick), c, 10.0])
        prev = c
    return out


def saw(n, leg=6, amp=0.03, base=1.0):
    """A sawtooth: legs of `leg` bars, each moving `amp` (fraction) up then down."""
    out, px, up = [], base, True
    for i in range(n):
        if i and i % leg == 0:
            up = not up
        px *= (1 + amp / leg) if up else (1 - amp / leg)
        out.append(px)
    return out


# ------------------------------------------------------------ swings
def test_zigzag_confirms_an_extreme_once_price_reverses_the_threshold():
    piv = C.zigzag([100, 102, 104, 101, 98, 100, 103], 2.0)
    assert piv == [(0, 100), (2, 104), (4, 98), (6, 103)]   # the last one is the running extreme
    assert C.zigzag([100, 100.5, 100.2, 100.4], 2.0) == [] and C.zigzag([100], 1.0) == []


def test_swing_stats_measures_the_median_swing_and_the_current_leg():
    s = C.swing_stats(bars_from(saw(300)))
    assert s["n"] > 10 and s["median_pct"] == pytest.approx(3.0, abs=0.4) and s["median_min"] == 30
    assert s["leg_dir"] in ("up", "down") and s["leg_age_min"] <= 35 and s["thr_pct"] > 0
    assert C.swing_stats(bars_from([1.0, 1.0])) == {"n": 0}


# ------------------------------------------------------------ the 15m regime
def _regime(last_block):
    quiet = [1.0 + (0.001 if i % 2 else 0.0) for i in range(300)]          # alternating 0.1% moves
    closes = quiet + last_block
    while (NOW // M5 - (len(closes) - 1)) % 3:                              # the last block starts on the 15m grid
        closes.insert(0, 1.0)
    return C.regime_state(bars_from(closes + [closes[-1]], now=NOW // M15 * M15 + 60_000))


def test_regime_state_classifies_the_last_closed_block_against_the_coins_own_day():
    assert _regime([1.002, 1.004, 1.006])["state"] == "trend_up"
    assert _regime([0.998, 0.996, 0.994])["state"] == "trend_down"
    active = _regime([1.01, 0.995, 1.008])
    assert active["state"] == "active" and active["rel_vol"] > 1 and active["efficiency"] < 0.5
    quiet = _regime([1.0005, 1.0, 1.0004])
    assert quiet["state"] == "quiet" and quiet["rel_vol"] < 1
    assert C.regime_state(bars_from([1.0, 1.0, 1.0]))["state"] is None


def test_rel_is_a_z_score_and_a_change_against_the_series():
    r = C.rel(12.0, [10.0, 10.0, 11.0, 9.0, 10.0])
    assert r["z"] > 2 and r["chg_pct"] == 20.0 and r["n"] == 5
    assert C.rel(1.0, [1.0, 1.0]) is None and C.rel(None, [1.0] * 5) is None


def test_acceptance_needs_two_closes_beyond_the_level():
    flat = [[NOW - (40 - i) * M15, 1.0, 1.01, 0.99, 1.0, 1.0] for i in range(40)]
    up = flat + [[NOW, 1.0, 1.03, 1.0, 1.02, 1.0], [NOW + M15, 1.02, 1.04, 1.01, 1.03, 1.0]]
    assert C.accepted(up)["side"] == "up" and C.accepted(up)["level"] == pytest.approx(1.01)
    down = flat + [[NOW, 1.0, 1.0, 0.97, 0.98, 1.0], [NOW + M15, 0.98, 0.99, 0.96, 0.97, 1.0]]
    assert C.accepted(down) == {"side": "down", "level": 0.99, "level_type": "prior_low"}
    wick = flat + [[NOW, 1.0, 1.05, 1.0, 1.0, 1.0], [NOW + M15, 1.0, 1.0, 0.995, 1.0, 1.0]]
    assert C.accepted(wick) is None   # a wick through the level is not acceptance


# ------------------------------------------------------------ events
def _state(**kw):
    st = {"price": 1.0, "px_15m_pct": 0.0, "oi_15m_pct": 0.0, "oi_1h_pct": 0.0, "swing_5m": {"median_pct": 2.0},
          "series": [], "news": 0}
    return {**st, **kw}


def names(evs):
    return [e["name"] for e in evs]


def test_each_event_rule():
    assert names(C.events(_state(oi_15m_pct=-3.5, px_15m_pct=-1.2), None, {})) == ["flush"]
    assert names(C.events(_state(oi_15m_pct=-3.5, px_15m_pct=0.4), None, {})) == []   # OI out on a rise: no flush
    assert names(C.events(_state(), None, {"liq": ["liq_long_burst"]})) == ["flush"]
    assert names(C.events(_state(oi_1h_pct=5.2), None, {})) == ["oi_surge"]
    assert names(C.events(_state(), None, {"btc_15m_pct": -0.9})) == ["btc_impulse"]
    assert names(C.events(_state(), None, {"btc_15m_pct": 0.5})) == []
    acc = {"side": "up", "level": 1.01, "level_type": "prior_high"}
    assert names(C.events(_state(accepted=acc), None, {})) == ["level_accepted"]
    assert names(C.events(_state(), None, {"entries": [1.009]})) == ["entry_near"]     # 0.9% < half of 2%
    assert names(C.events(_state(), None, {"entries": [1.03, 0.99]})) == []          # too far; below the market
    assert names(C.events(_state(news=3), {"news": 1}, {})) == ["headline"]
    assert names(C.events(_state(news=3), None, {})) == []                            # no baseline yet
    assert names(C.events(_state(news=3), {"news": None}, {})) == []                  # the feed was down last pass


def test_retail_flip_is_the_global_long_short_rising_while_price_sags():
    t = NOW // M15 * M15
    series = [[t - 3 * M15, 1.00, 100, 1.10, 1.0], [t - 2 * M15, 0.99, 100, 1.12, 1.0], [t, 0.97, 99, 1.16, 1.0]]
    ev = C.events(_state(series=series), None, {})
    assert names(ev) == ["retail_flip"] and "1.1 -> 1.16" in ev[0]["detail"]
    up = [p[:1] + [1.0 + i / 100] + p[2:] for i, p in enumerate(series)]               # price rising: not a flip
    assert C.events(_state(series=up), None, {}) == []
    old = [[t - 6 * M15, 1.00, 100, 1.10, 1.0], [t, 0.97, 99, 1.16, 1.0]]               # outside the 60 min window
    assert C.events(_state(series=old), None, {}) == []


def test_an_event_is_new_only_when_the_previous_pass_did_not_have_it():
    first = C.events(_state(oi_1h_pct=6.0), None, {})
    again = C.events(_state(oi_1h_pct=6.5), {"events": first}, {})
    assert first[0]["new"] is True and again[0]["new"] is False


# ------------------------------------------------------------ the break score (#81)
def _short_state(**kw):
    t = NOW // M15 * M15
    b15 = [[t - (15 - i) * M15, 1.0, 1.01, 0.99, 1.0] for i in range(16)]
    return {**_state(fakeout_p90=1.5, blocks15=b15, price=1.0), **kw}


def test_one_check_alone_is_noise_two_are_a_real_break():
    trade = {"opened_ms": NOW - 3 * 3_600_000}
    assert C.break_score(trade, _short_state())["score"] == 0
    beyond = C.break_score(trade, _short_state(price=1.03))       # 4.0% up from the 0.99 low > the 2.0% band
    assert beyond["checks"]["beyond_noise"] and beyond["score"] == 1 and beyond["verdict"] == "noise"
    assert beyond["band_pct"] == 2.0 and beyond["move_pct"] == pytest.approx(4.04, abs=0.01)
    both = C.break_score(trade, _short_state(price=1.03, btc_15m_pct=0.9))
    assert both["score"] == 2 and both["verdict"] == "real break" and both["checks"]["source"]


def test_each_break_check():
    trade = {"opened_ms": NOW - 3 * 3_600_000}
    acc = C.break_score(trade, _short_state(accepted={"side": "up", "level": 1.01, "level_type": "prior_high"}))
    assert acc["checks"]["accepted"] and acc["level"] == 1.01
    st = _short_state()
    st["blocks15"][-3][4] = st["blocks15"][-2][4] = 1.006                       # 2 closes above a read's 1.005
    assert C.break_score(trade, st, level=1.005)["checks"]["accepted"]
    assert not C.break_score(trade, st, level=1.008)["checks"]["accepted"]
    part = _short_state(px_1h_pct=2.0, oi_1h_pct=1.5, ti_1h=0.2)
    assert C.break_score(trade, part)["checks"]["participation"]
    covering = _short_state(px_1h_pct=2.0, oi_1h_pct=-1.5, ti_1h=0.2)          # price up with OI down: covering
    assert not C.break_score(trade, covering)["checks"]["participation"]
    assert not C.break_score(trade, _short_state(btc_15m_pct=-1.2))["checks"]["source"]   # BTC down helps a short
    late = C.break_score({"opened_ms": NOW}, _short_state(price=1.03))          # only the forming block since entry
    assert late["move_pct"] == pytest.approx(4.04, abs=0.01)


# ------------------------------------------------------------ measure
def test_measure_builds_the_state_and_keeps_one_series_point_per_15m_slot():
    closes = saw(864, amp=0.02)
    oi = [{"ts": NOW - (12 - i) * M5, "oi": 100.0 + i} for i in range(13)]
    flow = [[NOW - (12 - i) * M5, 1, 1, 1, 1, 10.0, 7.0] for i in range(13)]
    ls = {"global_account_ls": 1.3, "top_trader_position_ls": 0.8}
    st = C.measure(bars_from(closes), bars_from(saw(480), step=3_600_000), oi, ls, flow, None, NOW,
                   funding_hist=[0.0001] * 9, funding=0.0004, btc_15m_pct=0.1)
    assert st["oi_1h_pct"] == 12.0 and st["oi_15m_pct"] == 2.75 and st["ti_1h"] == 0.4
    assert st["swing_5m"]["n"] > 10 and st["swing_1h"]["n"] > 5 and st["regime"]["state"]
    assert len(st["blocks15"]) == C.BLOCKS_KEPT and st["funding"] == 0.0004 and st["btc_15m_pct"] == 0.1
    assert len(st["series"]) == 1 and st["series"][0][3] == 1.3
    again = C.measure(bars_from(closes), [], oi, ls, flow, st, NOW + 60_000)            # same slot: replaced
    assert len(again["series"]) == 1
    later = C.measure(bars_from(closes), [], oi, ls, flow, again, NOW + M15)
    assert len(later["series"]) == 2
    old = {"series": [[NOW // M15 * M15 - i * M15, 1.0, 1.0, 1.0, 1.0] for i in range(1, 400)]}
    assert len(C.measure(bars_from(closes), [], oi, ls, flow, old, NOW)["series"]) == C.SERIES_MAX
    with pytest.raises(ValueError):
        C.measure(bars_from([1.0] * 5), [], oi, ls, flow, None, NOW)
