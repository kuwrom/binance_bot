import pytest

from shortbot.backtest import funding_events as F

DAY = F.DAY
D0 = 1_600_000_000_000 // DAY * DAY


def test_classify_majors_by_volume_then_pumped_alts():
    vol = {f"C{i}": float(100 - i) for i in range(15)}
    ret7 = {"C12": 0.35, "C13": 0.10, "C0": 0.50}
    cls = F.classify(D0, vol, ret7)
    assert cls["C0"] == "major" and cls["C9"] == "major"           # top 10 by volume, pumped or not
    assert cls["C12"] == "pumped_alt" and cls["C13"] == "other_alt" and cls["C14"] == "other_alt"


def test_breakdown_day_needs_a_close_below_the_prior_low():
    bars = {D0: (10, 11, 9, 10, 1e7), D0 + DAY: (10, 12, 9.5, 11, 1e7), D0 + 2 * DAY: (11, 11, 9, 9.4, 1e7)}
    assert F.breakdown_day(bars, D0) == D0 + 2 * DAY                # 9.4 < day-1 low 9.5
    assert F.breakdown_day({D0: bars[D0], D0 + DAY: bars[D0 + DAY]}, D0) is None


def _market(n_days: int = 80) -> dict:
    """25 coins; HOT has funding 0.2%/8h on day 50 (one event) and falls 10% the next day."""
    bars, funding = {}, {}
    for i in range(25):
        s = "HOTUSDT" if i == 24 else f"C{i}USDT"
        rows, px = [], 100.0
        for k in range(n_days):
            if s == "HOTUSDT" and k == 51:
                px *= 0.9
            rows.append((D0 + k * DAY, px, px, px, px, 1e7 * (50 - i)))
        bars[s] = rows
        funding[s] = [(D0 + k * DAY + j * DAY // 3, 0.002 if (s == "HOTUSDT" and k in (50, 52)) else 0.0)
                      for k in range(n_days) for j in range(3)]
    return {"bars": bars, "funding": funding}


def test_events_quiet_period_class_and_edge():
    trades = F.events(_market())
    hot = [t for t in trades if t["symbol"] == "HOTUSDT" and t["entry"] == "raw" and t["thr"] == 0.0005]
    assert {t["day"] for t in hot} == {D0 + 50 * DAY}                # day 52 is inside the 7-day quiet window
    one = next(t for t in hot if t["h"] == 1)
    assert one["cls"] == "other_alt"                                  # lowest volume coin
    assert one["ret"] == pytest.approx(0.10 - F.FEES)
    # control = same-day mean of all other_alts (HOT -10%, the rest flat) -> edge is HOT minus that mean
    assert 0 < one["edge"] < one["ret"]


def test_evaluate_verdicts():
    trades = [{"symbol": f"S{i % 7}", "day": D0 + i * DAY, "cls": "major", "thr": 0.0005, "entry": "raw", "h": 1,
               "ret": -0.02 + (i % 3) * 0.001, "edge": -0.02 + (i % 3) * 0.001} for i in range(40)]
    [row] = F.evaluate(trades)
    assert row["verdict"] == "avoid" and row["n"] == 40 and row["coins"] == 7
