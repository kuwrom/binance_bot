"""Day/week P&L from equity marks net of transfers; equity peak moved by transfers (equity_marks.py, task #16)."""

from datetime import datetime, timezone

from shortbot import equity_marks as em
from shortbot.journal import Journal

T0 = datetime(2026, 10, 1, 0, 5, tzinfo=timezone.utc)          # 5 min after midnight (a Thursday)


def at(h, m=0):
    return datetime(2026, 10, 1, h, m, tzinfo=timezone.utc)


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
    assert r == {"day_pnl": -7.0, "week_pnl": -9.0, "late": {}, "degraded": True}


async def test_gather_falls_back_when_income_is_unreachable():
    class T:
        async def fapiPrivateGetIncome(self, req):
            raise RuntimeError("timeout")
    j = Journal(":memory:")
    j.set(em.KV_PEAK, "1200")
    r = await em.gather(T(), j, 1000.0, -3.0, -4.0)
    assert r["degraded"] and r["day_pnl"] == -3.0 and r["week_pnl"] == -4.0 and r["peak"] == 1200
