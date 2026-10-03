"""mtf: each column agrees with the existing single-bar indicators where they overlap, uses no future bars, and the
latest() view picks the last bar of every timeframe."""

import numpy as np

from shortbot import indicators as ind
from shortbot import mtf

N = 300


def bars(seed=1, flow=True, drift=0.0):
    rnd = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rnd.normal(drift, 0.01, N)))
    o = np.concatenate([[100.0], c[:-1]])
    h, lo = np.maximum(o, c) * 1.002, np.minimum(o, c) * 0.998
    v = rnd.lognormal(5, 0.3, N)
    rows = np.column_stack([np.arange(N) * 300_000, o, h, lo, c, v] + ([v * rnd.uniform(0.3, 0.7, N)] if flow else []))
    return rows


def test_columns_match_the_existing_indicators():
    b = bars()
    s = mtf.series(b, "1h")
    rows = b.tolist()
    assert bool(s["lower_high"][-1]) == ind.lower_high(rows, 4)
    assert abs(s["ema_gap"][-1] - (b[-1, 4] / ind.ema(list(b[:, 4]), 20)[-1] - 1)) < 1e-12
    assert abs(s["rsi"][-1] - ind.rsi(list(b[:, 4]), 14)) < 1.0               # Wilder seed differs early only
    assert abs(s["ret"][-1] - (b[-1, 4] / b[-1 - 24, 4] - 1)) < 1e-12
    v4 = b[-4:, 5].sum()
    assert abs(s["ti"][-1] - (2 * b[-4:, 6].sum() - v4) / v4) < 1e-12


def test_no_column_looks_ahead():
    b = bars(2)
    full = mtf.series(b, "15m")
    cut = mtf.series(b[:200], "15m")
    for k in mtf.COLUMNS:
        a, z = full[k][:200], cut[k]
        assert np.allclose(a[~np.isnan(z)], z[~np.isnan(z)], equal_nan=True), k


def test_move_z_and_flow_absent():
    b = bars(3, flow=False)
    b[-1, 4] = b[-2, 4] * 1.2                                # a 20% last bar
    s = mtf.series(b, "5m")
    assert s["move_z"][-1] > 3 and np.isnan(s["ti"][-1])


def test_latest_takes_each_timeframes_last_bar():
    got = mtf.latest({"5m": bars(4).tolist(), "1h": bars(5).tolist(), "4h": None, "2h": bars(6).tolist()})
    assert set(got) == {"5m", "1h"} and set(got["1h"]) == set(mtf.COLUMNS)
    assert got["5m"]["ret"] == round(float(mtf.series(bars(4), "5m")["ret"][-1]), 5)


def test_chain_study_aggregation_and_events():
    import pandas as pd

    from shortbot.backtest import chain_study as CH
    t = np.arange(120) * 60_000
    df = pd.DataFrame({"ts": t, "o": 1.0, "h": np.arange(120) + 2.0, "l": 0.5, "c": np.arange(120) + 1.0,
                       "v": 1.0, "tbv": 0.4})
    b15 = CH.aggregate(df, 900_000)
    assert len(b15) == 8 and b15[0].tolist() == [0, 1.0, 16.0, 0.5, 15.0, 15.0, 15 * 0.4]
    f = np.zeros(300, dtype=bool)
    f[[5, 6, 50, 101, 102, 250]] = True
    assert CH.events(f).tolist() == [5, 101, 250]                 # 24 h (96 steps) of silence after each
