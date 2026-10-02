"""long_shadow: the forward test opens the backtest's signals, closes by time / stop / delisting, labels excess, and the
job runs against a fake exchange shaped like Binance's raw klines / 24h tickers (no orders anywhere)."""

import asyncio
from datetime import UTC, datetime

from shortbot import long_shadow as S
from shortbot.backtest import long_studies as L
from shortbot.backtest.carry_regime import DAY
from shortbot.backtest.xs_factors import Panel

# a Sunday close, 130 days after D0
DS = (1_759_000_000_000 // DAY + 7) // 7 * 7 * DAY
while datetime.fromtimestamp(DS / 1000, UTC).weekday() != 6:
    DS += DAY
D0 = DS - 130 * DAY


def paths(n=131):
    out = {L.BTC: [100 * 1.002 ** k for k in range(n)]}          # BTC trending up: gate G on
    for i in range(20):
        out[f"C{i}USDT"] = [50.0 + 0.01 * ((k * (i + 3)) % 7) for k in range(n)]
    out["LEADUSDT"] = [50.0] * 100 + [50.0 * 1.03 ** k for k in range(1, n - 99)]   # breaks out, leads by far
    return out


def panel(ps, drop: dict[str, int] | None = None) -> Panel:
    bars = {}
    for s, cl in ps.items():
        n = len(cl) - (drop or {}).get(s, 0)
        bars[s] = [(D0 + k * DAY, c, c * 1.01, c * 0.99, c, 50e6) for k, c in enumerate(cl[:n])]
    return Panel({"bars": bars, "funding": {}})


def test_signals_are_the_backtests_and_record_dedupes(tmp_path):
    st = L.Study(panel(paths()))
    sig = S.signals(st, DS)
    l1 = [t["symbol"] for t in sig if t["setup"] == "L1"]
    assert "LEADUSDT" in l1 and len(l1) == 5 and all(t["gate"] == 1 for t in sig)
    assert l1 == L.rs_picks(st, DS, 28, L.rs_names(st, DS))
    t = next(t for t in sig if t["setup"] == "L1")
    assert t["stop"] < t["entry"] and t["exit_day"] == DS + 7 * DAY
    db = S.connect(tmp_path / "s.db")
    assert S.record(db, sig) == len(sig) and S.record(db, sig) == 0


def test_l2_breakout_day_and_its_stop_floor():
    ps = paths()
    st = L.Study(panel(ps))
    day = next(d for d, s in L.breakout_days(st) if s == "LEADUSDT")
    t = next(t for t in S.signals(st, day) if t["setup"] == "L2" and t["symbol"] == "LEADUSDT")
    low20 = min(st.p.bars["LEADUSDT"][day - k * DAY][3] for k in range(1, 21))
    assert t["stop"] <= low20 and t["exit_day"] == day + 5 * DAY


def test_exits_time_stop_and_delisting_then_excess(tmp_path):
    db = S.connect(tmp_path / "s.db")
    entry = DS - 10 * DAY
    rows = [("L1", "C1USDT", entry, 50.0, 10.0, entry + 7 * DAY),      # time exit
            ("L1", "C2USDT", entry, 50.0, 49.9, entry + 7 * DAY),      # the first daily low (0.99 x) hits 49.9
            ("L2", "C3USDT", entry, 50.0, 10.0, entry + 5 * DAY)]      # delists after 2 days
    with db:
        db.executemany("INSERT INTO shadow (setup, symbol, entry_day, entry, stop, exit_day, gate, universe) "
                       "VALUES (?,?,?,?,?,?,1,'C1USDT,C2USDT')", rows)
    p = panel(paths(), drop={"C3USDT": 131 - 123})                      # C3 trades until entry + 2 days
    xs = {x["row"]["symbol"]: x for x in S.exits(db, p, DS)}
    assert xs["C1USDT"]["reason"] == "time" and xs["C1USDT"]["day"] == entry + 7 * DAY
    assert xs["C2USDT"]["reason"] == "stop" and xs["C2USDT"]["exit"] == 49.9
    assert xs["C3USDT"]["reason"] == "delisted" and xs["C3USDT"]["day"] == entry + 2 * DAY
    S.close(db, xs["C1USDT"], p, funding=0.001)
    r = db.execute("SELECT * FROM shadow WHERE symbol='C1USDT'").fetchone()
    assert r["status"] == "closed" and abs(r["excess"] - (r["ret"] - r["bench"] - L.COST - 0.001)) < 1e-12
    assert S.summary(db)["L1"] == {"closed": 1, "open": 1, "mean_excess": round(r["excess"], 4)}


class FakePublic:
    def __init__(self, ps):
        self.ps, self.calls = ps, []

    async def fapiPublicGetTicker24hr(self):
        return [{"symbol": s, "quoteVolume": str(1e8 - i)} for i, s in enumerate(self.ps)]

    async def fapiPublicGetKlines(self, params):
        self.calls.append(params)
        cl = self.ps[params["symbol"]]
        today = DS + DAY                                                  # the forming bar is dropped
        rows = [[D0 + k * DAY, c, c * 1.01, c * 0.99, c, 1.0, 0, 50e6] for k, c in enumerate(cl)]
        rows.append([today, cl[-1], cl[-1], cl[-1], cl[-1], 1.0, 0, 1.0])
        return rows[-params["limit"]:]

    async def fetch_funding_rate_history(self, symbol, since=None, limit=None):
        return [{"timestamp": since + 8 * 3_600_000, "fundingRate": 0.0001}]


class FakeEx:
    def __init__(self, ps):
        self.public = FakePublic(ps)
        self.ps = ps

    async def coin_perps(self):
        return {f"{s[:-4]}/USDT:USDT": {"id": s} for s in self.ps}


def test_job_runs_on_a_fake_exchange(tmp_path, monkeypatch):
    ps = paths()
    monkeypatch.setattr(S, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: datetime.fromtimestamp(
        (DS + DAY + 3600_000) / 1000, UTC)), "fromtimestamp": staticmethod(datetime.fromtimestamp)}))
    monkeypatch.setattr(S, "FORWARD_START", DS - 2 * DAY)                # catch up Friday..Sunday
    ex = FakeEx(ps)
    out = asyncio.run(S.run(ex, tmp_path / "s.db"))
    assert out["gate"] is True and out["opened"] >= 5 and ("L1", "LEADUSDT") in out["signals"]
    assert out["evaluated_from"] == datetime.fromtimestamp((DS - 2 * DAY) / 1000, UTC).date().isoformat()
    assert all(c["interval"] == "1d" and c["limit"] == S.HISTORY_D + 5 for c in ex.public.calls)   # 4 catch-up days + the forming bar
    again = asyncio.run(S.run(ex, tmp_path / "s.db"))
    assert again["opened"] == 0 and again["signals"] == []                # same day: nothing new
