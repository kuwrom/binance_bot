"""One day P&L everywhere (#16): Trader.account() reads income once and reports day/week P&L from the equity marks
(equity_marks.py), and every consumer reads that value on the start-of-day basis: risk.py's day check (open_short,
limit entries), the guardian's daily-loss trip and give-back peak, and the governor. A loss carried over midnight in an
open position is not today's; a deposit or a withdrawal moves none of them."""

import asyncio
import json
import time
from datetime import UTC, datetime

import pytest

from shortbot import equity_marks as em
from shortbot import guardian_positions as gp
from shortbot import killswitch as ks
from shortbot import reconcile
from shortbot.entries import EntryDesk
from shortbot.killswitch import ACTIVE, REDUCING
from shortbot.risk import RiskViolation, ShortRequest, check_open_short
from shortbot.risk_budget import gather_inputs
from tests.fakes import SYM, FakeCcxt
from tests.test_entries import favorable, prop
from tests.test_guardian import guardian_with, kinds


@pytest.fixture(autouse=True)
def neutral_vol(monkeypatch):
    """No BTC-vol dampener: the governor's daily cap is the plain daily_loss_limit_pct (f = 1 when favorable)."""
    monkeypatch.setattr("shortbot.risk_budget._vol_cache", (time.time(), 0.0, 0.0, []))


def desk(equity=10_000.0, mark=None, carried=0.0):
    """A guardian on a fake account whose day and week marks were taken just after each period began (not late), with
    an open short carrying `carried` unrealized P&L since before then; the transfer-adjusted peak last moved 10 min
    ago."""
    fake = FakeCcxt(equity=equity)
    g = guardian_with(fake)
    g.j.set(ks.HEARTBEAT_KEY, str(time.time()))
    for kind, (key, start) in em.period_starts(datetime.now(UTC)).items():
        g.j.set(f"eq_mark:{kind}:{key}", json.dumps({"equity": mark or equity, "ts": start + 60_000,
                                                     "unrealized": carried}))
    g.j.set(em.KV_PEAK, str(mark or equity))
    g.j.set(em.KV_CURSOR, str(int(time.time() * 1000) - 600_000))
    if carried:
        fake.sym[SYM].update(qty=4000.0, entry=1.0, price=1.0 - carried / 4000)
    return g, fake


def income(fake, kind, usdt, age_s=120):
    fake.income.append({"symbol": "" if kind == "TRANSFER" else "PUMPUSDT", "incomeType": kind, "income": str(usdt),
                        "asset": "USDT", "time": int(time.time() * 1000) - age_s * 1000})


async def consumers(g):
    """What each consumer concludes on the same account: (risk.py day check blocks a new short, kill-switch state,
    governor daily-cap halt, give-back stop halt, today's give-back peak)."""
    tr = g.app.trader
    await g.check_positions()
    try:
        check_open_short(ShortRequest("NEW/USDT:USDT", 10, 1.0, 1.05, 3, 1e9), await tr.account_state(),
                         tr.ex.limits)
        blocked = False
    except RiskViolation as e:
        blocked = any("daily loss" in r for r in e.reasons)
    gov = (await gather_inputs(tr, "favorable")).gov
    peak = float(g.j.get(f"day_peak:{datetime.now(UTC).date().isoformat()}") or 0)
    return (blocked, ks.read(g.j)["state"], any("daily loss cap" in r for r in gov["reasons"]),
            any("give-back" in r for r in gov["reasons"]), peak)


async def test_account_reports_the_mark_day_pnl_and_keeps_the_old_one_as_evidence():
    g, _ = desk(carried=-400)
    a = await g.app.trader.account()
    assert (a["day_pnl"], a["week_pnl"], a["old_day_pnl"], a["old_week_pnl"]) == (0, 0, -400, -400)
    assert a["late"] == a["pending"] == {"day": False, "week": False} and not a["degraded"]
    assert a["equity_peak"] == 10_000
    assert (a["realized_today"], a["unrealized"]) == (0, -400)


async def test_a_loss_carried_over_midnight_blocks_and_trips_nothing():
    g, _ = desk(carried=-400)                          # -4% on the old definition: past the 3% limit
    assert await consumers(g) == (False, ACTIVE, False, False, 0.0)


async def test_a_gain_carried_over_midnight_never_arms_the_give_back_stop():
    g, _ = desk(carried=300)                           # +3% on the old definition: would arm the give-back peak
    assert await consumers(g) == (False, ACTIVE, False, False, 0.0)


async def test_a_new_loss_today_trips_every_consumer():
    g, fake = desk(mark=10_400)                         # 400 lost since 00:00 UTC: -3.85% of the day's start
    income(fake, "REALIZED_PNL", -400)
    assert await consumers(g) == (True, REDUCING, True, False, 0.0)
    ev = json.loads(g.j.decisions()[0]["snapshot"])["evidence"]
    assert (ev["day_pnl"], ev["old_day_pnl"]) == (-400, -400)
    assert not ev["late"]["day"] and not ev["pending"]["day"] and not ev["degraded"]


@pytest.mark.parametrize("equity, verdict", [(9_705, (False, ACTIVE, False, False, 0.0)),
                                             (9_700, (True, REDUCING, True, False, 0.0))])
async def test_all_consumers_share_the_start_of_day_basis_at_the_limit(equity, verdict):
    g, fake = desk(equity=equity, mark=10_000)          # -2.95% / -3.00% of the day's start, limit 3%
    income(fake, "REALIZED_PNL", equity - 10_000)
    assert await consumers(g) == verdict


async def test_a_withdrawal_income_doesnt_show_yet_is_pending_and_trips_nothing():
    g, fake = desk()
    fake.equity -= 1_000                                 # the balance moved; its TRANSFER row isn't reported yet
    assert await consumers(g) == (False, ACTIVE, False, False, 0.0)
    a = await g.app.trader.account()
    assert a["pending"]["day"] and a["day_pnl"] == 0


@pytest.mark.parametrize("usdt", [1_000, -1_000])
async def test_a_deposit_or_withdrawal_moves_none_of_them(usdt):
    g, fake = desk()
    income(fake, "TRANSFER", usdt)
    fake.equity += usdt
    assert await consumers(g) == (False, ACTIVE, False, False, 0.0)
    a = await g.app.trader.account()
    assert (a["day_pnl"], a["week_pnl"], a["equity_peak"]) == (0, 0, 10_000 + usdt)   # the peak moves with it
    assert (await gather_inputs(g.app.trader, "favorable")).gov["drawdown_pct"] == 0


async def test_one_account_wide_income_read_per_guardian_pass_and_per_governor_read():
    g, fake = desk(carried=-400)
    reads, orig = [], fake.fapiPrivateGetIncome

    async def counted(req):
        if "symbol" not in req:
            reads.append(req)
        return await orig(req)
    fake.fapiPrivateGetIncome = counted
    await g.check_positions()
    assert len(reads) == 1
    await gather_inputs(g.app.trader, "favorable")
    assert len(reads) == 2


async def down(*a, **k):
    raise RuntimeError("income endpoint down")


async def test_an_income_outage_keeps_the_heartbeat_fresh_and_trips_reducing_after_three_passes(monkeypatch):
    """Protection worked (stops checked), so the heartbeat stays fresh: an income outage never reaches the stale-
    heartbeat HALTED. Losses can't be checked, so after three passes in a row: REDUCING, resting entries kept."""
    g, fake = desk(mark=10_400, carried=-40)   # past the limit on a guess; unprotected: its stop comes first
    monkeypatch.setattr(reconcile, "income_since", down)
    lane, states = g._lanes()["protective"], []
    for _ in range(5):
        lane.last = dict.fromkeys(lane.last, 0.0)   # every protective job due
        g.j.set(ks.HEARTBEAT_KEY, "0")
        await lane.tick()
        assert time.time() - float(g.j.get(ks.HEARTBEAT_KEY)) < 60
        states.append(ks.read(g.j)["state"])
    assert states == [ACTIVE, ACTIVE, REDUCING, REDUCING, REDUCING]
    st = ks.read(g.j)
    assert st["cause"] == "day_pnl_unverified" and "day P&L unverified" in st["reason"] and st["by"] == "guardian"
    assert list(fake.stops.values()) and kinds(g).count("day_pnl_unverified") == 1   # one alert per cooldown
    assert await ks.watch_guardian(g.app.trader) is None and ks.read(g.j)["state"] == REDUCING


async def test_a_hung_income_read_counts_as_unverified_and_never_holds_up_the_pass(monkeypatch):
    g, fake = desk(carried=-40)

    async def hung(*a, **k):
        await asyncio.sleep(60)
    monkeypatch.setattr(reconcile, "income_since", hung)
    monkeypatch.setattr(gp, "ACCOUNT_TIMEOUT_S", 0.01)
    for _ in range(3):
        await g.check_positions()
    assert list(fake.stops.values()) and ks.read(g.j)["cause"] == "day_pnl_unverified"


async def test_the_unverified_trip_is_sticky_and_a_clear_during_the_outage_holds(monkeypatch):
    g, _ = desk()
    real = reconcile.income_since
    monkeypatch.setattr(reconcile, "income_since", down)
    for _ in range(3):
        await g.check_positions()
    monkeypatch.setattr(reconcile, "income_since", real)
    await g.check_positions()                         # readable again: verified, but a human clears the trip
    assert ks.read(g.j)["cause"] == "day_pnl_unverified" and "day_pnl_verified" in kinds(g)
    monkeypatch.setattr(reconcile, "income_since", down)
    for _ in range(3):
        await g.check_positions()                     # a new outage while still REDUCING: nothing changes
    await ks.clear(g.j, g.app.settings, ACTIVE, "human:test", "checked P&L on Binance", g.j.get(ks.KV_KEY))
    for _ in range(3):
        await g.check_positions()                     # still down: the clear holds (one trip per outage)
    assert ks.read(g.j)["state"] == ACTIVE
    monkeypatch.setattr(reconcile, "income_since", real)
    await g.check_positions()
    monkeypatch.setattr(reconcile, "income_since", down)
    for n in range(3):
        assert ks.read(g.j)["state"] == ACTIVE        # a later outage: its own streak of three
        await g.check_positions()
    assert ks.read(g.j)["cause"] == "day_pnl_unverified"


async def test_opening_paths_still_refuse_while_income_is_unreadable(monkeypatch):
    g, fake = desk()
    monkeypatch.setattr(reconcile, "income_since", down)
    with pytest.raises(RuntimeError, match="income endpoint down"):
        await g.app.trader.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100)
    r = await EntryDesk(g.app.trader, regime_fn=favorable).propose_group([prop(1.04)])
    assert not r["ok"] and "income endpoint down" in r["rejected"][0]["error"]
    assert not fake.orders and not fake.limits
