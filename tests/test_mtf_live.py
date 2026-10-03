"""mtf_live: a coin's view uses closed bars only, the job logs every streamed coin once per 15m close."""

import numpy as np

from shortbot import mtf
from shortbot import mtf_live as L
from shortbot.market import streams as S

M15 = S.TF_MS["15m"]
NOW = 1_790_000_000_000 // M15 * M15 + 60_000           # one minute after a 15m close


def store(tmp_path, n=160):
    st = S.StreamStore(tmp_path / "streams.db")
    rnd = np.random.default_rng(5)
    with st.db:
        for tf in ("5m", "15m", "1h", "4h", "1d"):
            step = S.TF_MS[tf]
            cur = NOW // step * step                         # the forming bar
            c = 100 * np.exp(np.cumsum(rnd.normal(0, 0.01, n)))
            for i in range(n):
                ts = cur - (n - 1 - i) * step
                st.db.execute(f"INSERT INTO candles ({S.COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              ("AAAUSDT", tf, ts, c[i], c[i] * 1.01, c[i] * 0.99, c[i], 10.0, 1000.0, 400.0,
                               int(i < n - 1), 4.0))
            st.db.execute("INSERT OR REPLACE INTO fresh VALUES ('AAAUSDT', ?, ?)", (f"kline_{tf}", NOW - 1000))
    return tmp_path / "streams.db"


def test_view_drops_the_forming_bar_and_covers_every_timeframe(tmp_path):
    p = store(tmp_path)
    v = L.view(p, "AAAUSDT", NOW)
    assert set(v) == set(mtf.TFS)
    closed = L._closed(p, "AAAUSDT", "15m", NOW)
    assert closed[-1][0] + M15 <= NOW and len(closed) == 150
    assert v["15m"]["ret"] == round(float(mtf.series(closed, "15m")["ret"][-1]), 5)
    assert v["5m"]["ti"] == round((2 * 4 * 4 - 40) / 40, 5)              # taker 4 of 10 per bar


def test_job_logs_once_per_close(tmp_path):
    p = store(tmp_path)
    out = tmp_path / "mtf.db"
    assert L.run(p, NOW, out)["logged"] == 1
    assert L.run(p, NOW + 30_000, out)["logged"] == 0                     # same close
    db = L.connect(out)
    (ts, sym), = db.execute("SELECT ts, symbol FROM views").fetchall()
    assert ts == NOW // M15 * M15 and sym == "AAAUSDT"
