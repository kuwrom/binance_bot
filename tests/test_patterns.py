from shortbot import patterns as P
from shortbot.patterns_detect import detect

H = 3_600_000


def rows(n=200, price=1.0, qv=1000.0, oi=5e6):
    return [{"ts": i * H, "o": price, "h": price * 1.002, "l": price * 0.998, "c": price, "qv": qv, "n": 100,
             "tbq": qv / 2, "oi": oi, "prem": 0.0, "spot_c": None, "spot_qv": None} for i in range(n)]


def test_pump_kk_needs_volume_and_price_spike_and_pump_fail_follows():
    r = rows()
    r[100].update(qv=5000.0, h=1.08, c=1.06)            # 5x volume, high 8% over the 12h mean close
    for k in range(101, 200):
        r[k].update(o=1.06, h=1.061, l=1.059, c=1.06)
    r[102].update(o=1.06, c=1.05)                        # first red close after the pump
    ev = detect(r)
    assert ("pump_kk", 100) in ev and ("pump_fail", 102) in ev
    r2 = rows()
    r2[100].update(qv=5000.0)                            # volume alone is not a pump
    assert not [e for e in detect(r2) if e[0] == "pump_kk"]


def test_sweep_high_is_the_first_close_back_below_the_swept_high():
    r = rows()
    r[100].update(h=1.05, c=1.003)                       # wick >= 1.5 ATR over the 24h high (1.002), closes above
    r[101].update(c=0.999)                               # first close back below -> event at 101
    ev = [e for e in detect(r) if e[0] == "sweep_high"]
    assert ev == [("sweep_high", 101)]


def test_squeeze_up_needs_oi_down():
    r = rows()
    r[100].update(c=1.05, h=1.05, oi=4.5e6)              # +5% hour with OI -10%: shorts forced out
    assert ("squeeze_up", 100) in detect(r)
    r[100].update(oi=5.5e6)
    assert ("squeeze_up", 100) not in detect(r)


def test_funding_events_map_to_the_hour_that_just_closed_and_dedup():
    r = rows()
    ev = detect(r, [[100 * H + H, 0.001], [101 * H + H, 0.001], [150 * H, -0.001]])
    assert ("funding_hi", 100) in ev and ("funding_hi", 101) not in ev   # 24h dedup
    assert ("funding_neg", 149) in ev


def test_forward_is_a_short_return_and_needs_24h_of_future():
    r = rows(60)
    for k in range(11, 60):
        r[k].update(c=0.9, h=0.95, l=0.9)
    f = P.forward(r, 10)
    assert f["ret_24h"] == 10.0 and abs(f["mae_24h"] + 5.0) < 1e-9 and f["ret_72h"] is None
    assert P.forward(r, 40) is None


def test_summarize_counts_coins_and_squeezes():
    ev = [{"symbol": "A", "ret_24h": 5.0, "mae_24h": 20.0}, {"symbol": "A", "ret_24h": -1.0, "mae_24h": 2.0},
          {"symbol": "B", "ret_24h": 3.0, "mae_24h": 1.0}]
    s = P.summarize(ev)
    assert s["n"] == 3 and s["n_coins"] == 2 and s["squeeze_rate"] == 33.3 and s["coin_weighted_mean_24h"] == 2.5
    assert P.bucket(7.0, P.OI_BUCKETS_M) == "5-10M" and P.bucket(None, P.OI_BUCKETS_M) == "n/a"
