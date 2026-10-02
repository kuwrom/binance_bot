"""The guardian heartbeat (#61 D): kv guardian_heartbeat = when the guardian last VERIFIED protection: a positions pass
in which every held position's check completed with its stop confirmed (or the account read flat), while the fill
poll's last run succeeded. Only that pass stamps it (the time of its position read). Readers: unverified for
VERIFIED_S -> new risk refused (computed, lifts itself); STALE_HEARTBEAT_S -> the session watchdog trips the
self-clearing HALTED; two verified passes lift it. (Named test_killswitch*: the real watchdog runs, conftest.py.)"""

import sqlite3
import time

import ccxt
import pytest

from shortbot import killswitch as ks
from shortbot import stops
from shortbot.journal import Journal
from shortbot.killswitch import ACTIVE, HALTED, STALE
from shortbot.symlock import SymbolBusy
from tests.fakes import SYM, FakeCcxt
from tests.test_guardian import SETTINGS, guardian_with, kinds, open_trade

OLD = "1000.0"   # a pass long ago: the tests read whether a new one replaced it


def beat(g) -> float | None:
    v = g.j.get(ks.HEARTBEAT_KEY)
    return None if v is None else float(v)


async def protective_tick(g):
    """One protective-lane tick with every job due, on the lanes run() builds."""
    g.lanes = g.lanes or g._lanes()
    ln = g.lanes["protective"]
    for name in ln.last:
        ln.last[name] = 0.0
    await ln.tick()


def job(g, name):
    return next(j for j in g.lanes["protective"].jobs if j.name == name)


async def boom(*a, **k):
    raise RuntimeError("boom")


async def guardian_holding():
    """A guardian over one open short (opened through the real gate: a live guardian is assumed for that)."""
    fake = FakeCcxt()
    g = guardian_with(fake)
    g.j.set(ks.HEARTBEAT_KEY, str(time.time()))
    await open_trade(g)
    g.j.set(ks.HEARTBEAT_KEY, OLD)
    return g, fake


# ------------------------------------------------------------ what stamps it
async def test_a_flat_account_read_stamps_the_time_of_that_read():
    g = guardian_with(FakeCcxt())
    t0 = time.time()
    await g.check_positions()
    assert beat(g) is not None and t0 <= beat(g) <= time.time()


async def test_a_held_short_with_its_stop_confirmed_stamps():
    g, fake = await guardian_holding()
    await g.check_positions()
    assert beat(g) > 1000 and list(fake.stops.values()) == [1.05]


async def test_no_trade_keys_never_stamps():
    """A monitor-only guardian protects nothing: every protective job returns early, which is not protection."""
    g = guardian_with(FakeCcxt())
    g.ex.trade = None
    await protective_tick(g)
    await protective_tick(g)
    assert beat(g) is None


def _break(failure, g, fake, monkeypatch):
    """Make one held position's check fail in a way check_positions isolates; returns the undo."""
    if failure == "stop_read":   # Trader.positions lists it UNPROTECTED 'unknown' with the error
        g.app.trader.stop_orders = boom
        return lambda: delattr(g.app.trader, "stop_orders")
    if failure == "restore":     # the stop is gone and restoring it fails (attempt 1 of 3, then a forced close)
        fake.stops.clear()
        fake.fail_stop = 1
        return lambda: None
    if failure == "busy":        # a session holds the symbol lock: skipped this pass

        async def busy(tr, sym):
            raise SymbolBusy(sym, 0, "session: move_stop")
        real = stops.listed
        monkeypatch.setattr(stops, "listed", busy)
        return lambda: monkeypatch.setattr(stops, "listed", real)
    if failure == "plan":        # the stop is confirmed, then the plan raises. If #65's move renames _run_plan or
        # swallows its errors, the plan runs (or fails) silently, the pass stamps, and this case fails: a plan that
        # raises must keep meaning "not verified"
        g._run_plan = boom
        return lambda: delattr(g, "_run_plan")
    raise AssertionError(failure)


@pytest.mark.parametrize("failure", ["stop_read", "restore", "busy", "plan"])
async def test_a_swallowed_position_failure_withholds_the_heartbeat(failure, monkeypatch):
    g, fake = await guardian_holding()
    undo = _break(failure, g, fake, monkeypatch)
    await g.check_positions()          # the job itself succeeds: the failure is isolated and alerted
    assert g.j.get(ks.HEARTBEAT_KEY) == OLD
    undo()
    await g.check_positions()
    assert beat(g) > 1000 and fake.position > 0 and fake.stops


async def test_a_long_position_is_not_verified():
    g = guardian_with(FakeCcxt())

    async def longs():
        return [{"symbol": SYM, "contracts": 5, "side": "long", "entryPrice": 1.0, "markPrice": 1.0, "notional": 5,
                 "unrealizedPnl": 0, "liquidationPrice": 0.5}]
    g.app.trader.t.fetch_positions = longs   # the fake (Trader.t)
    await g.check_positions()
    assert beat(g) is None and "unexpected_long" in kinds(g)


async def test_a_heartbeat_write_failure_never_costs_the_pass_its_income_read():
    g = guardian_with(FakeCcxt())
    real_set, real_account, reads = g.j.set, g.app.trader.account, []

    def locked(k, v):
        if k == ks.HEARTBEAT_KEY:
            raise sqlite3.OperationalError("database is locked")
        return real_set(k, v)

    async def account():
        reads.append(1)
        return await real_account()
    g.j.set, g.app.trader.account = locked, account
    await g.check_positions()
    assert reads and beat(g) is None


# ------------------------------------------------------------ which protective jobs count
async def test_a_failing_fill_poll_withholds_the_heartbeat():
    g = guardian_with(FakeCcxt())
    real, g.check_entries = g.check_entries, boom
    await protective_tick(g)           # the positions pass verifies a flat account, but fills aren't being adopted
    assert beat(g) is None
    job(g, "entries").fn = real
    await protective_tick(g)
    assert beat(g) is not None


async def test_entry_reconcile_failing_alone_keeps_the_heartbeat_and_a_stale_halt_lifts():
    """Bookkeeping, not protection: before #61 its failure alone stalled the heartbeat and a stale HALTED never lifted."""
    g = guardian_with(FakeCcxt())
    g.reconcile_entries = boom
    await ks.trip(g.j, SETTINGS, HALTED, "session watchdog", "laptop slept", cause=STALE)
    for _ in range(3):
        await protective_tick(g)
    assert beat(g) is not None and ks.read(g.j)["state"] == ACTIVE
    msg = next(a["message"] for a in g.j.alerts(100) if a["kind"] == "guardian_failing:entry_reconcile")
    assert "bookkeeping" in msg.lower() and "Fills and stops are NOT" not in msg


async def test_escalations_say_what_is_and_is_not_checked():
    g = guardian_with(FakeCcxt())
    g.check_positions = boom
    g.check_entries = boom
    for _ in range(3):
        await protective_tick(g)
    msgs = {a["kind"]: a["message"] for a in g.j.alerts(100)}
    assert "stops" in msgs["guardian_failing:positions"] and "refused" in msgs["guardian_failing:positions"]
    assert "fill" in msgs["guardian_failing:entries"].lower()


# ------------------------------------------------------------ readers
async def test_unverified_protection_refuses_new_risk_without_tripping_then_a_stale_heartbeat_halts():
    g = guardian_with(FakeCcxt())
    tr, now = g.app.trader, time.time()
    g.j.set(ks.HEARTBEAT_KEY, str(now - 30))
    assert await ks.gate(tr, "open_short") is None
    g.j.set(ks.HEARTBEAT_KEY, str(now - 200))   # two passes missed: not verified, not yet stale
    r = await ks.gate(tr, "open_short")
    assert not r["ok"] and r["kill_switch"] == ACTIVE and "unverified" in r["error"]
    assert ks.read(g.j)["state"] == ACTIVE and "lifts itself" in r["error"]
    g.j.set(ks.HEARTBEAT_KEY, str(now - ks.STALE_HEARTBEAT_S - 20))
    r = await ks.gate(tr, "open_short")
    assert r["kill_switch"] == HALTED and ks.read(g.j)["cause"] == STALE


async def test_a_stale_halt_lifts_after_two_verified_passes_at_the_positions_cadence():
    """The positions pass stamps every ~60 s and the guardian reads every 5 s: a 60 s freshness window reset the count
    just before each new pass, so the halt could never lift."""
    j, t = Journal(":memory:"), time.time()
    await ks.trip(j, SETTINGS, HALTED, "session watchdog", "laptop slept", cause=STALE)
    for p in range(3):
        j.set(ks.HEARTBEAT_KEY, str(t + 60 * p))
        for s in range(0, 65, 5):   # a pass can start a few seconds late
            await ks.auto_clear_stale(j, SETTINGS, t + 60 * p + s)
    assert ks.read(j)["state"] == ACTIVE


async def test_brief_line_says_unverified_between_the_thresholds():
    j, now = Journal(":memory:"), time.time()
    j.set(ks.HEARTBEAT_KEY, str(now - 200))
    assert "UNVERIFIED" in ks.brief_line(j, now) and "refused" in ks.brief_line(j, now)
    assert ks.status(j, now)["protection_verified"] is False
    j.set(ks.HEARTBEAT_KEY, str(now - 20))
    assert ks.status(j, now)["protection_verified"] is True and "UNVERIFIED" not in ks.brief_line(j, now)


# ------------------------------------------------------------ 2026-10-02 05:25-08:59 UTC, replayed
async def test_the_dns_outage_replayed():
    """The laptop slept with dark wakes and no DNS for ~215 min: every positions pass failed (17x), the fill poll had
    nothing to poll, the desk was flat. The heartbeat must stay where protection last worked; a session trying to
    enter is refused (then HALTED once stale); two verified passes after recovery lift it."""
    g = guardian_with(FakeCcxt())
    await protective_tick(g)
    before = beat(g)

    async def dns(*a, **k):
        raise ccxt.ExchangeNotAvailable("binanceusdm GET https://demo-fapi.binance.com/fapi/v3/positionRisk "
                                        "[Errno 8] nodename nor servname provided, or not known")
    g.app.trader._raw_positions = dns
    for _ in range(17):
        await protective_tick(g)
    assert beat(g) == before and "guardian_failing:positions" in kinds(g)
    tr = g.app.trader
    g.j.set(ks.HEARTBEAT_KEY, str(before - 200))        # 200 s after the last good pass
    assert (await ks.gate(tr, "open_short"))["kill_switch"] == ACTIVE and ks.read(g.j)["state"] == ACTIVE
    g.j.set(ks.HEARTBEAT_KEY, str(before - 1800))       # 30 min after
    assert (await ks.gate(tr, "open_short"))["kill_switch"] == HALTED
    del g.app.trader._raw_positions                     # DNS back
    for _ in range(3):
        await protective_tick(g)
    assert ks.read(g.j)["state"] == ACTIVE and "kill_switch_clear" in kinds(g)
