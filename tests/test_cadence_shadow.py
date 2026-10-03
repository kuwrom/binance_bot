"""cadence_shadow on a synthetic stream store: it evaluates every closed minute since the last run, logs the minutes
a rule holds (the study's definitions), resumes where it stopped, and the report applies the study's comparison."""

import numpy as np

from shortbot import cadence_shadow as CSH
from shortbot.market import streams as S

MIN = 60_000
END = 1_790_000_000_000 // MIN * MIN


def store_with(tmp_path, surge_at=None, n=CSH.HISTORY + 40):
    st = S.StreamStore(tmp_path / "streams.db")
    rnd = np.random.default_rng(3)
    t0 = END - n * MIN
    rows = []
    for i in range(n):
        q = 1000.0 + rnd.normal(0, 30)
        if surge_at is not None and i >= n - surge_at:
            q *= 8                                          # the last `surge_at` minutes: an 8x volume hour
        rows.append((t0 + i * MIN, 100.0, 100.2, 99.8, 100.0, 10.0, q))
    with st.db:
        st.db.executemany(f"INSERT INTO candles ({S.COLS}) VALUES (?,?,?,?,?,?,?,?,?,NULL,1,NULL)",
                          [("AAAUSDT", "1m", ts, o, h, lo, c, v, q) for ts, o, h, lo, c, v, q in rows])
        st.db.execute("INSERT OR REPLACE INTO fresh VALUES ('AAAUSDT', 'kline_1m', ?)", (END,))
    return tmp_path / "streams.db"


def test_logs_a_volume_surge_minute_and_resumes(tmp_path):
    store = store_with(tmp_path, surge_at=60)
    shadow = tmp_path / "shadow.db"
    r = CSH.run(store, END + 1000, shadow)                 # the last closed minute is END // MIN - 1
    assert r["minutes"] == 1 and r["coins"] == 1 and r["flags"] == 1
    db = CSH.connect(shadow)
    (minute, rule, value), = db.execute("SELECT minute, rule, value FROM flags").fetchall()
    assert minute == END // MIN - 1 and rule == "volume_surge" and value > 10
    assert CSH.run(store, END + 1000, shadow)["minutes"] == 0      # nothing new to evaluate


def test_quiet_market_logs_nothing_and_short_history_is_skipped(tmp_path):
    store = store_with(tmp_path)
    assert CSH.run(store, END + 1000, tmp_path / "a.db")["flags"] == 0
    (tmp_path / "short").mkdir()
    short = store_with(tmp_path / "short", n=500)                  # 500 minutes: under the 30 h the rules need
    assert CSH.evaluate(short, "AAAUSDT", [END // MIN - 1]) == []


def test_report_compares_one_minute_with_the_fifteen_minute_grid(tmp_path):
    db = CSH.connect(tmp_path / "r.db")
    base = 1_000_000 // 15 * 15
    rows = [(base + k, "AAAUSDT", "volume_surge", 3.0) for k in range(7, 40)]       # true from minute +7
    rows += [(base + 500 + k, "AAAUSDT", "volume_surge", 3.0) for k in range(1, 4)]  # a spike the grid misses
    with db:
        db.executemany("INSERT INTO flags VALUES (?,?,?,?)", rows)
    rep = CSH.report(tmp_path / "r.db")["rules"]["volume_surge"]
    assert rep["episodes_1m"] == 2 and rep["episodes_15m"] == 1
    assert rep["lead_median"] == 8.0 and rep["transients"] == 0.5
