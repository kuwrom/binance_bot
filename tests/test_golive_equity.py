"""The go-live gate's equity drawdown (golive_equity.py, #87): the day marks plus each day's intraday low and high (the
guardian's day P&L range, kv day_low / day_peak), net of transfers and demo balance resets. An equity move between two
marks that the P&L booked in between (each exit fill when it filled, the rest at the close) and the change in
unrealized P&L don't explain is a jump: it moves the peak, as equity_marks.adjusted_peak moves it for a transfer."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from shortbot import golive_equity as GE
from shortbot.journal import Journal
from shortbot.risk_budget import track_day_low

START = "2026-10-03T00:00:00+00:00"
D0 = datetime(2026, 10, 3, tzinfo=UTC)


def at(day: float, hours: float = 0.0) -> datetime:
    return D0 + timedelta(days=day, hours=hours)


def ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def mark(j, day, equity, unrealized: float | None = 0.0):
    """The guardian's first equity of the day, a minute after 00:00 UTC."""
    t = at(day, 1 / 60)
    j.set(j.mode_key(f"eq_mark:day:{t.date().isoformat()}"), json.dumps(
        {"equity": equity, "ts": ms(t), **({"unrealized": unrealized} if unrealized is not None else {})}))


def day_range(j, day, low=None, high=None):
    d = at(day).date().isoformat()
    if low is not None:
        j.set(j.mode_key(f"day_low:{d}"), str(low))
    if high is not None:
        j.set(j.mode_key(f"day_peak:{d}"), str(high))


def trade(j, closed: datetime | None, pnl: float | None, exits=(), setup="pump_fade", entry=1.0, amount=100.0):
    """A short; exits: (time, px, qty) exit fills (a partial, then the rest)."""
    tid = j.create_trade("C/USDT:USDT", setup, "t", 1.05, 2, {}, {})
    j.activate_trade(tid, entry, amount, entry * amount)
    if closed is not None:
        j.close_trade(tid, entry, pnl, "x")
        j.update_trade(tid, closed_at=closed.isoformat(timespec="seconds"))
    fills = [{"leg": "exit", "type": "market", "order": f"o{i}", "px": px, "qty": q, "ms": ms(t)}
             for i, (t, px, q) in enumerate(exits)]
    j.update_trade(tid, fill_refs=json.dumps({"v": 1, "fills": fills}))
    return tid


def dd(j):
    return GE.drawdown(j, START)


def test_the_marks_alone_give_the_old_figure():
    j = Journal(":memory:")
    trade(j, at(0, 12), 100.0)
    trade(j, at(1, 12), -100.0)
    for day, eq in enumerate((1000.0, 1100.0, 1000.0)):
        mark(j, day, eq)
    d = dd(j)
    assert d["max_dd_pct"] == pytest.approx(9.09, abs=0.01) and d["at"] == "2026-10-05" and d["kind"] == "mark"
    assert d["jumps"] == [] and d["marks"] == 3 and d["marks_only_pct"] == d["max_dd_pct"]


def test_an_intraday_low_counts():
    j = Journal(":memory:")
    mark(j, 0, 1000.0)
    mark(j, 1, 1000.0)
    day_range(j, 0, low=-90.0)                      # down 9% at some point on the 3rd, back by midnight
    d = dd(j)
    assert d["max_dd_pct"] == pytest.approx(9.0) and (d["at"], d["kind"]) == ("2026-10-03", "intraday low")
    assert d["marks_only_pct"] == 0.0 and d["ranges"] == 1


def test_a_drawdown_from_an_intraday_high_counts():
    j = Journal(":memory:")
    mark(j, 0, 1000.0)
    mark(j, 1, 1000.0)
    day_range(j, 0, high=100.0)                     # up 10% intraday, all given back by the next mark
    assert dd(j)["max_dd_pct"] == pytest.approx(9.09, abs=0.01)


def test_a_days_high_is_never_put_before_its_low():
    j = Journal(":memory:")
    trade(j, at(0, 20), 100.0)
    mark(j, 0, 1000.0)
    mark(j, 1, 1100.0)
    day_range(j, 0, low=-50.0, high=100.0)          # which came first is unknown: only the certain drawdown counts
    assert dd(j)["max_dd_pct"] == pytest.approx(5.0)


@pytest.mark.parametrize("move", [-400.0, 5000.0])
def test_a_transfer_or_a_demo_reset_is_netted(move):
    j = Journal(":memory:")
    mark(j, 0, 1000.0)
    mark(j, 1, 1000.0 + move)                       # nothing booked explains it
    trade(j, at(1, 12), -50.0)
    mark(j, 2, 950.0 + move)                        # then a real loss
    d = dd(j)
    assert d["max_dd_pct"] == pytest.approx(50 / (1000 + move) * 100, abs=0.01)   # vs the peak moved by the jump
    assert d["jumps"] == [{"from": "2026-10-03", "to": "2026-10-04", "move": move, "explained": 0.0, "jump": move}]
    if move < 0:
        assert d["marks_only_pct"] == pytest.approx(45.0)


def test_a_jump_days_intraday_range_is_not_used():
    j = Journal(":memory:")
    mark(j, 0, 1000.0)
    mark(j, 1, 600.0)
    day_range(j, 0, low=-400.0)                     # a demo reset: no TRANSFER row, so the day P&L saw it as a loss
    d = dd(j)
    assert d["max_dd_pct"] == 0.0 and d["ranges_skipped"] == ["2026-10-03"] and len(d["jumps"]) == 1


def test_a_partial_close_before_midnight_is_trading_not_a_jump():
    j = Journal(":memory:")
    trade(j, at(2, 6), 50.0, exits=[(at(0, 12), 0.7, 100.0), (at(2, 6), 0.8, 100.0)], amount=200.0)   # +30, then +20
    mark(j, 0, 1000.0)
    mark(j, 1, 1040.0, unrealized=10.0)             # the partial's 30 is in the wallet, the rest +10 unrealized
    mark(j, 2, 1045.0, unrealized=15.0)
    mark(j, 3, 1050.0)
    d = dd(j)
    assert d["jumps"] == [] and d["max_dd_pct"] == 0.0


def test_a_loss_in_an_open_position_and_a_drills_pnl_are_trading():
    j = Journal(":memory:")
    trade(j, at(1, 12), -50.0, setup="exp_drill_kill")   # drills trade the same account
    mark(j, 0, 1000.0)
    mark(j, 1, 900.0, unrealized=-100.0)
    mark(j, 2, 950.0)
    d = dd(j)
    assert d["jumps"] == [] and d["max_dd_pct"] == pytest.approx(10.0)


def test_a_mark_without_unrealized_is_unchecked_and_counts_as_trading():
    j = Journal(":memory:")
    mark(j, 0, 1000.0)
    mark(j, 1, 600.0, unrealized=None)
    d = dd(j)
    assert d["unchecked"] == 1 and d["jumps"] == [] and d["max_dd_pct"] == pytest.approx(40.0)


def test_another_modes_marks_and_trades_never_count(tmp_path):
    demo = Journal(tmp_path / "j.db")
    mark(demo, 0, 1000.0)
    mark(demo, 1, 1000.0)
    live = Journal(tmp_path / "j.db", "live")
    mark(live, 0, 50.0)
    trade(live, at(0, 12), -500.0)
    assert dd(demo)["max_dd_pct"] == 0.0 and dd(demo)["marks"] == 2


def test_marks_before_the_window_never_count():
    j = Journal(":memory:")
    mark(j, -3, 2000.0)
    mark(j, 0, 1000.0)
    assert dd(j)["max_dd_pct"] == 0.0 and dd(j)["marks"] == 1


def test_no_marks_no_figure():
    d = dd(Journal(":memory:"))
    assert d["max_dd_pct"] is None and d["marks"] == 0


def test_the_days_low_keeps_the_lowest_day_pnl():
    j, now = Journal(":memory:"), at(5, 9)
    assert track_day_low(j, 3.0, now) == 0.0                     # a gain: the low stays at the mark
    assert track_day_low(j, -5.0, now) == -5.0
    assert track_day_low(j, -2.0, now) == -5.0
    assert j.get("day_low:2026-10-08") == "-5.0"
