"""Reads that missed this process's queued writes while another process held the journal's write lock (#82).

- The governor verdict and the market map are marked (Journal.mark): the protective lane's sweep reads the halt or
  the pause it published during the hold, and withdraws the resting entries.
- The risk peaks (equity peak, day P&L peak, the period's first equity mark) merge with what this process posted:
  drawdown during a hold is never read smaller than what this process already saw, and a withdrawal still lowers
  the peak (no phantom drawdown).
- Every desk table is created when the journal opens: a first read runs no DDL, which waited for the write lock."""

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

from shortbot import entry_gates, laggard, shadow_eval, sweeps, targets, warmup
from shortbot import killswitch as ks
from shortbot.entries import EntryDesk
from shortbot.equity_marks import KV_CURSOR, KV_PEAK, adjusted_peak, period_pnl
from shortbot.journal import Journal
from shortbot.market import market_map, sectors
from shortbot.risk_budget import track_day_peak
from tests.held_lock import STALL_REPORT, HeldLock
from tests.test_entry_races import favorable, prop
from tests.test_guardian_offloop import guardian_on

NOW = datetime(2026, 10, 5, 0, 5, tzinfo=UTC)   # a Monday, 5 min into the day and the week
NOW_MS = int(NOW.timestamp() * 1000)


def stored(path, k: str) -> str | None:
    """What every other process reads."""
    row = sqlite3.connect(path).execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row[0] if row else None


# ------------------------------------------------------------ 1. what the sweeps read
@STALL_REPORT
async def test_a_governor_halt_published_during_a_hold_withdraws_the_resting_entries(tmp_path):
    g = guardian_on(tmp_path / "j.db")
    g.j.set(ks.HEARTBEAT_KEY, str(time.time()))
    assert (await EntryDesk(g.app.trader, regime_fn=favorable).propose_group([prop(1.04)]))["ok"]
    with HeldLock(tmp_path / "j.db", 1.0):
        entry_gates.publish_governor(g.j, {"halt": True, "reasons": ["daily loss cap hit"]}, ".env baseline")
        assert entry_gates.governor_halt(g.j) == ["daily loss cap hit"]   # before #82: [] until it landed
        out = await entry_gates.sweep(g.entries)
    assert [r["reason"] for r in out] == ["governor halt: daily loss cap hit"] and out[0]["cancelled"]
    await g.j.flush()
    assert stored(tmp_path / "j.db", entry_gates.GOV_KEY)


async def test_a_market_pause_built_during_a_hold_is_read_by_the_sweep(tmp_path, monkeypatch):
    async def tags():
        return {}

    async def none(*a, **k):
        return {}

    async def ohlcv(sym, tf, limit=0, **k):   # flat hourly bars; BTC and ETH +1% over the last 15 one-minute bars
        return [[i, 100, 100, 100, 100, 1] for i in range(40)] if tf == "1h" else \
            [[i, 100, 101, 100, 100 + i / 15, 1] for i in range(16)]
    monkeypatch.setattr(sectors, "fetch_tags", tags)
    ex = NS(coin_perps=none, public=NS(fetch_tickers=none, fetch_funding_rates=none, fetch_ohlcv=ohlcv))
    j = Journal(tmp_path / "j.db")
    with HeldLock(tmp_path / "j.db", 0.5):
        await market_map.build_map(ex, j)
        assert entry_gates.market_pause(j) == ["BTC +1.00% in 15 min: alts catch up, wait",
                                               "ETH +1.00% in 15 min: alts catch up, wait"]   # before #82: []
        assert j.get(sectors.KV_KEY) and j.get(market_map.KV_PREV_RANKS)    # the next build reads them back too
        await j.flush()
    assert stored(tmp_path / "j.db", market_map.KV_LAST) and not j._marks


# ------------------------------------------------------------ 3. risk peaks
async def test_the_day_pnl_peak_is_not_read_lower_during_a_hold(tmp_path):
    j, key = Journal(tmp_path / "j.db"), f"day_peak:{NOW.date().isoformat()}"
    with HeldLock(tmp_path / "j.db", 0.3):
        assert track_day_peak(j, 5.0, NOW) == 5.0
        assert track_day_peak(j, 2.0, NOW) == 5.0                # before #82: 2.0, the give-back stop disarmed
        await j.flush()
    assert stored(tmp_path / "j.db", key) == "5.0"
    Journal(tmp_path / "j.db").set(key, "7.0")                   # another process saw a higher one since
    assert track_day_peak(j, 1.0, NOW) == 7.0


async def test_the_equity_peak_is_not_read_lower_during_a_hold(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.set(KV_PEAK, "1000")
    j.set(KV_CURSOR, str(NOW_MS - 120_000))
    with HeldLock(tmp_path / "j.db", 0.3):
        assert adjusted_peak(j, 1100, [], NOW_MS) == 1100
        assert adjusted_peak(j, 1000, [], NOW_MS + 5000) == 1100   # before #82: 1000, a 9% drawdown read as none
        await j.flush()
    assert float(stored(tmp_path / "j.db", KV_PEAK)) == 1100 and j.pending(KV_PEAK) is None


async def test_a_withdrawal_during_a_hold_lowers_the_pending_peak_too(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.set(KV_PEAK, "1000")
    j.set(KV_CURSOR, str(NOW_MS - 120_000))
    out = [{"incomeType": "TRANSFER", "income": -100, "time": NOW_MS + 10_000}]   # 100 USDT withdrawn
    with HeldLock(tmp_path / "j.db", 0.3):
        assert adjusted_peak(j, 1100, [], NOW_MS) == 1100
        assert adjusted_peak(j, 1000, out, NOW_MS + 90_000) == 1000   # no drawdown: the peak moved with it
        await j.flush()
    assert float(stored(tmp_path / "j.db", KV_PEAK)) == 1000


async def test_the_first_day_mark_holds_during_a_hold_and_the_first_stored_one_wins(tmp_path):
    j = Journal(tmp_path / "j.db")
    with HeldLock(tmp_path / "j.db", 0.3):
        assert period_pnl(j, 1000, [], 0, 0, NOW)["day_pnl"] == 0
        p = period_pnl(j, 950, [], 0, 0, NOW + timedelta(minutes=1))
        assert (p["day_pnl"], p["week_pnl"]) == (-50, -50)       # before #82: 0, each pass marked itself first
    o = Journal(tmp_path / "j.db")
    assert period_pnl(o, 990, [], 0, 0, NOW + timedelta(minutes=2))["day_pnl"] == 0   # another process, first to land
    await j.flush()
    assert period_pnl(j, 950, [], 0, 0, NOW + timedelta(minutes=3))["day_pnl"] == -40   # its mark, not ours


# ------------------------------------------------------------ 4. tables at open
DESK_TABLES = {"target_orders", "ladder_shadow", "trial_ledger", "warmups", "laggard_events", "level_sweeps"}


def test_a_journal_creates_every_desk_table_when_it_opens(tmp_path):
    old = sqlite3.connect(tmp_path / "j.db")                     # a ladder table from before its columns
    old.execute("CREATE TABLE ladder_shadow (id INTEGER PRIMARY KEY, ts TEXT, trade_id INTEGER, symbol TEXT, "
                "price REAL, score REAL, parts TEXT, levels TEXT, x_star REAL, x_actual REAL, x_virtual REAL, "
                "action TEXT)")
    old.commit()
    j = Journal(tmp_path / "j.db")
    assert DESK_TABLES <= {r[0] for r in j.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"variant", "stop_v", "entry", "init_r"} <= {r[1] for r in j.db.execute("PRAGMA table_info(ladder_shadow)")}


@STALL_REPORT
async def test_first_reads_of_the_desk_tables_run_no_ddl_under_a_held_lock(tmp_path):
    j = Journal(tmp_path / "j.db")
    with HeldLock(tmp_path / "j.db", 2.0):
        t0 = time.monotonic()
        assert targets.live(j) == [] and targets.view(j, 1) == []
        assert warmup.desk_state(j)["cold"]
        assert shadow_eval.evaluate(j) is not None
        assert laggard.report(j)["labelled_events"] == 0
        assert sweeps.report(j) is not None
        assert time.monotonic() - t0 < 0.5                       # before #82: the first CREATE TABLE waited 2 s
        await j.flush()
