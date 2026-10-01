import random

from shortbot.analogs import Analogs, validate, vector


def trade(i, x, r, ts):
    return {"symbol": f"C{i}", "entry_ts": ts, "r": r,
            "f": {"ret_24h": x, "atr_pct": 2.0, "hour": i % 24, "weekend": False, "vol24_m": 10.0}}


def test_vector_handles_missing_and_cyclic_hour():
    v = vector({"hour": 6, "vol24_m": 9.0})
    assert len(v) == 22 + 3 + 3 + 2 and abs(v[-2] - 1.0) < 1e-9     # sin(2*pi*6/24) = 1


def test_knn_finds_signal_and_validation_flags_it_useful():
    rng = random.Random(3)
    ts = [trade(i, x := rng.uniform(-10, 10), (1.0 if x > 0 else -1.0) + rng.gauss(0, 0.3), i) for i in range(400)]
    q = Analogs(ts[:200]).query({"ret_24h": 8.0, "atr_pct": 2.0})
    assert q["pred_r"] > 0.5 and q["k"] == 25
    v = validate(ts, 200)
    assert v["ok"] and v["useful"] and v["spread_r"] > 1.0
    noise = [trade(i, rng.uniform(-10, 10), rng.gauss(0, 1), i) for i in range(400)]
    assert not validate(noise, 200)["useful"]
