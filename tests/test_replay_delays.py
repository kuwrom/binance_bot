"""Entry-delay runs of the replayed setups (backtest/replay_delays.py, task #73)."""

import math

import pytest

from shortbot.backtest import replay_delays as RD

T = 1_790_208_000_000 + 10 * 3_600_000          # a scan at 10:00
FIVE = 300_000


def rows(path):
    return [{"ts": T - 24 * 12 * FIVE + k * FIVE, "o": p, "h": p * 1.001, "l": p * 0.999, "c": p, "qv": 5e6}
            for k, p in enumerate(path)]


def cand(sym="AAA/USDT:USDT", t=T, setups=("pump_fade",), tradable=1, price=100.0, sigma=0.01):
    return {"symbol": sym, "ts_ms": t, "tradable": tradable, "setups": list(setups), "price": price,
            "features": {"last": price, "sigma_h": sigma, "quote_volume": 1e8}}


def test_plan_is_the_scans_bracket_with_the_setups_hold():
    p = RD.plan_for("pump_fade", 100.0, 0.01, 100.0)
    move = 0.01 * 24 ** 0.5
    assert p.stop == pytest.approx(100 * math.exp(move)) and p.targets[0][0] == pytest.approx(100 * math.exp(-2 * move))
    assert p.time_stop == 12 * 12 and p.be_r == pytest.approx(5 / ((p.stop - 100) / 100 * 100))
    assert RD.plan_for("pump_fade", 100.0, 0.01, 113.0) is None          # price already through the stop
    assert RD.plan_for("listing_fade", 100.0, 0.01, 100.0).time_stop == 72 * 12   # capped at the label horizon


def test_delays_enter_later_bars_and_censored_exits_are_dropped():
    path = [100.0] * 288 + [100.0 - 0.05 * k for k in range(400)]     # flat day, then a slow decline
    r = RD.simulate_delays(cand(), "pump_fade", rows(path), funding=[])
    assert set(r) == {0, 5, 15, 30} and all(x["filled"] for x in r.values())
    assert r[0]["entry_ts"] == T - FIVE and r[30]["entry_ts"] == T + 25 * 60_000
    assert r[0]["r"] > 0 and r[30]["entry_px"] < r[0]["entry_px"]          # later entry, lower short price
    short = RD.simulate_delays(cand(), "pump_fade", rows(path[:300]), funding=[])
    assert all(v is None for v in short.values())                          # the 12h hold runs past the data


def test_episodes_and_table():
    cs = [cand(t=T), cand(t=T + 1800_000), cand(t=T + 13 * 3_600_000), cand(sym="B", t=T, tradable=0),
          cand(sym="C", t=T, setups=("breakdown", "pump_fade"))]
    ep = RD.setup_episodes(cs)
    assert [c["ts_ms"] for c in ep["pump_fade"] if c["symbol"] == "AAA/USDT:USDT"] == [T, T + 13 * 3_600_000]
    assert [c["symbol"] for c in ep["breakdown"]] == ["C"]
    res = {"pump_fade": [(c, {0: {"filled": True, "r": 0.5 + k / 10, "reason": "target"}, 5: None})
                         for k, c in enumerate(ep["pump_fade"])]}
    tab = RD.table(res, delays=(0, 5))
    t0 = tab[0]
    assert (t0["setup"], t0["delay_min"], t0["n"], t0["episodes"]) == ("pump_fade", 0, 3, 3)
    assert t0["mean_r"] == pytest.approx(0.6) and t0["coins"] == 2 and tab[1]["n"] == 0
    reg = RD.register_rows(tab, 30, "both/5/25")
    assert len(reg) == 1 and reg[0]["family"] == "replay.pump_fade" and reg[0]["params"]["delay_min"] == 0
