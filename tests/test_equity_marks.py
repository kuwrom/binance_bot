"""Day/week P&L from equity marks net of transfers; equity peak moved by transfers (equity_marks.py, task #16)."""

from datetime import UTC, datetime

from shortbot import equity_marks as em
from shortbot.journal import Journal

T0 = datetime(2026, 10, 1, 0, 5, tzinfo=UTC)          # 5 min after midnight (a Thursday)


def at(h, m=0):
    return datetime(2026, 10, 1, h, m, tzinfo=UTC)


def ms(dt):
    return int(dt.timestamp() * 1000)


def test_a_loss_carried_over_midnight_no_longer_counts_as_todays_loss():
    j = Journal(":memory:")
    r = em.period_pnl(j, 1000.0, [], old_day=-100.0, old_week=-100.0, now=T0)   # old def: -100 carried in
    assert r["day_pnl"] == 0 and not r["late"]["day"] and not r["degraded"]
    assert em.period_pnl(j, 950.0, [], old_day=-150.0, old_week=-150.0, now=at(9))["day_pnl"] == -50


def test_transfers_are_not_pnl():
    j = Journal(":memory:")
    em.period_pnl(j, 1000.0, [], 0, 0, now=T0)
    dep = [{"incomeType": "TRANSFER", "income": "100", "time": ms(at(1))},
           {"incomeType": "REALIZED_PNL", "income": "-5", "time": ms(at(2))}]
    assert em.period_pnl(j, 1095.0, dep, 0, 0, now=at(3))["day_pnl"] == -5


def test_a_late_mark_takes_the_worse_of_new_and_old():
    j = Journal(":memory:")
    r = em.period_pnl(j, 1000.0, [], old_day=-40.0, old_week=-40.0, now=at(3))    # guardian slept past midnight
    assert r["late"]["day"] and r["day_pnl"] == -40
    r = em.period_pnl(j, 1010.0, [], old_day=-30.0, old_week=-30.0, now=at(4))
    assert r["day_pnl"] == -30                                                     # min(+10, -30)


def test_the_peak_moves_with_transfers_and_can_be_reset():
    j = Journal(":memory:")
    j.set(em.KV_PEAK, "1000")
    j.set(em.KV_CURSOR, str(ms(T0)))
    wd = [{"incomeType": "TRANSFER", "income": "-120", "time": ms(at(1))}]
    assert em.adjusted_peak(j, 870.0, wd, ms(at(2))) == 880            # a withdrawal is not a drawdown
    assert em.adjusted_peak(j, 900.0, wd, ms(at(3))) == 900            # cursor moved: the withdrawal counts once
    out = em.reset_peak(j, 850.0, "owner reset after demo top-up")
    assert out["ok"] and out["old_peak"] == 900 and float(j.get(em.KV_PEAK)) == 850


def test_never_raises():
    class BadJournal:
        def get(self, k):
            raise RuntimeError("database is locked")

        def set(self, k, v):
            raise RuntimeError("database is locked")
    r = em.period_pnl(BadJournal(), 1000.0, [], -7.0, -9.0, now=T0)
    assert r == {"day_pnl": -7.0, "week_pnl": -9.0, "late": {}, "pending": {}, "degraded": True}


def test_overlapping_callers_count_each_transfer_once():
    j = Journal(":memory:")
    j.set(em.KV_PEAK, "1000")
    j.set(em.KV_CURSOR, str(ms(T0)))
    dep = [{"incomeType": "TRANSFER", "income": "100", "time": ms(at(1))}]
    a = em.adjusted_peak(j, 1000.0, dep, ms(at(2)))         # caller A advances the cursor over the deposit
    b = em.adjusted_peak(j, 1000.0, dep, ms(at(1, 30)))     # caller B started earlier: its window is behind
    assert a == 1100 and b == 1100 and int(j.get(em.KV_CURSOR)) == ms(at(2)) - em.CURSOR_LAG_MS


def test_a_late_reported_transfer_is_counted_on_the_next_pass():
    j = Journal(":memory:")
    j.set(em.KV_PEAK, "1000")
    j.set(em.KV_CURSOR, str(ms(T0)))
    now = ms(at(2))
    late = [{"incomeType": "TRANSFER", "income": "-50", "time": now - 30_000}]   # inside the lag window
    assert em.adjusted_peak(j, 900.0, late, now) == 1000                         # not counted yet
    assert em.adjusted_peak(j, 900.0, late, now + 120_000) == 950                # counted once, next pass
    assert em.adjusted_peak(j, 900.0, late, now + 240_000) == 950


def test_a_deposit_inside_the_lag_window_is_not_counted_twice_in_the_peak():
    j = Journal(":memory:")
    j.set(em.KV_PEAK, "10000")
    j.set(em.KV_CURSOR, str(ms(T0)))
    now = ms(at(2))
    dep = [{"incomeType": "TRANSFER", "income": "1000", "time": now - 10_000}]    # in the balance, inside the lag
    assert em.adjusted_peak(j, 11000.0, dep, now) == 10000                       # not yet in the high-water mark
    assert em.adjusted_peak(j, 11000.0, dep, now + 120_000) == 11000             # counted once: no phantom 12000


def test_a_withdrawal_in_the_balance_before_its_income_row_is_pending_not_a_loss():
    j = Journal(":memory:")
    em.period_pnl(j, 1000.0, [], 0, 0, now=T0, unrealized=0.0)                     # marks with unrealized 0
    r = em.period_pnl(j, 880.0, [], -999, -999, now=at(1), unrealized=0.0)        # -120 withdrawn, row not seen
    assert r["pending"]["day"] and r["day_pnl"] == 0                               # income view: no trading loss
    wd = [{"incomeType": "TRANSFER", "income": "-120", "time": ms(at(1))}]
    r = em.period_pnl(j, 880.0, wd, -999, -999, now=at(1, 1), unrealized=0.0)      # row arrived: views agree
    assert not r["pending"]["day"] and r["day_pnl"] == 0


def test_a_persistent_disagreement_takes_the_worse_view():
    j = Journal(":memory:")
    em.period_pnl(j, 1000.0, [], 0, 0, now=T0, unrealized=0.0)
    em.period_pnl(j, 950.0, [], 0, 0, now=at(1), unrealized=0.0)                   # first seen: pending
    r = em.period_pnl(j, 950.0, [], 0, 0, now=at(1, 5), unrealized=0.0)            # still unexplained after 5 min
    assert not r["pending"]["day"] and r["day_pnl"] == -50


def test_trading_income_counts_only_pnl_types():
    rows = [{"incomeType": "REALIZED_PNL", "income": "-10", "time": ms(at(1))},
            {"incomeType": "COMMISSION", "income": "-1", "time": ms(at(1))},
            {"incomeType": "TRANSFER", "income": "500", "time": ms(at(1))}]
    assert em.trading_income(rows, ms(T0)) == -11
