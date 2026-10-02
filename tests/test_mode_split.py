"""Demo and live share one journal (#32): every trade and entry carries its mode, and a live desk sees none of the demo
history, positions, resting entries or account state (equity peak, period marks, day peak, research overrides)."""

import sqlite3
from datetime import datetime, timezone

import pytest

from shortbot import equity_marks as em
from shortbot import tranches
from shortbot.config import ResearchBounds, RiskLimits, Settings
from shortbot.entries import EntryDesk
from shortbot.entry_intents import external_orders
from shortbot.governor import loss_streak
from shortbot.journal import Journal
from shortbot.params import ResearchParams
from shortbot.risk_budget import track_day_peak
from shortbot.trading import Trader
from tests.fakes import FakeCcxt, FakeEx, neutral_budget, ok_prechecks
from tests.test_entries import favorable, prop

T0 = datetime(2026, 10, 1, 0, 5, tzinfo=timezone.utc)


def pair(tmp_path):
    db = tmp_path / "shortbot.db"
    return Journal(db, "demo"), Journal(db, "live")


def loss(j, sym="A/USDT:USDT"):
    tid = j.create_trade(sym, "pump_fade", "t", 1.1, 3, experiment="lev10")
    j.activate_trade(tid, 1.0, 10, 10)
    j.close_trade(tid, 1.1, -1.0, "stop")
    return tid


def test_a_bad_mode_is_refused():
    with pytest.raises(ValueError, match="mode"):
        Journal(":memory:", "paper")


def test_trades_are_stamped_and_a_live_desk_sees_no_demo_history(tmp_path):
    demo, live = pair(tmp_path)
    for _ in range(5):
        loss(demo)
    held = demo.create_trade("B/USDT:USDT", "pump_fade", "t", 1.1, 3)
    demo.activate_trade(held, 1.0, 10, 10)
    assert demo.get_trade(held)["mode"] == "demo" and loss_streak(
        [(r["closed_at"], r["pnl"]) for r in demo.closed_results()])["current"] == 5
    assert live.closed_results() == [] and live.open_trades(include_opening=True) == []
    assert live.active_trade("B/USDT:USDT") is None and live.trades() == []
    s = live.stats()
    assert (s["mode"], s["closed_trades"], s["other_modes"]) == ("live", 0, {"demo": 5})
    tid = loss(live)
    assert live.get_trade(tid)["mode"] == "live" and len(live.closed_results()) == 1
    assert len(live.trades(all_modes=True)) == 7 and len(live.closed_results(all_modes=True)) == 6
    assert demo.stats()["closed_trades"] == 5 and demo.active_trade("B/USDT:USDT")["id"] == held


def test_existing_rows_migrate_as_demo(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, status TEXT NOT NULL "
                "DEFAULT 'open', opened_at TEXT, pnl REAL)")
    con.execute("INSERT INTO trades (symbol, opened_at) VALUES ('OLD', '2026-09-27T00:00:00+00:00')")
    con.commit()
    con.close()
    assert Journal(db).open_trades()[0]["mode"] == "demo" and Journal(db, "live").open_trades() == []


def test_mode_key_keeps_demo_keys_bare(tmp_path):
    demo, live = pair(tmp_path)
    assert demo.mode_key("equity_peak") == "equity_peak" and live.mode_key("equity_peak") == "equity_peak@live"


# ------------------------------------------------------------ account-level kv
def test_a_live_account_does_not_inherit_the_demo_peak_marks_or_day_peak(tmp_path):
    demo, live = pair(tmp_path)
    assert em.adjusted_peak(demo, 5126.78, []) == 5126.78
    em.period_pnl(demo, 5000.0, [], 0, 0, now=T0)
    assert track_day_peak(demo, 106.0, T0) == 106.0
    assert em.adjusted_peak(live, 500.0, []) == 500.0                  # not a 90% drawdown on day one
    r = em.period_pnl(live, 500.0, [], 0, 0, now=T0)
    assert (r["day_pnl"], r["week_pnl"]) == (0, 0)                      # its own marks, not demo's 5000
    assert track_day_peak(live, 2.0, T0) == 2.0                         # demo's +106 doesn't arm the give-back
    assert live.get(em.KV_PEAK) == "5126.78"                            # the bare key is demo's, untouched
    assert em.adjusted_peak(demo, 5000.0, []) == 5126.78


def test_research_overrides_do_not_cross_into_live(tmp_path):
    demo, live = pair(tmp_path)

    def s(mode):
        return Settings(mode, "k", "s", ":memory:", "", "", RiskLimits(), ResearchBounds(), True)
    ResearchParams(s("demo"), demo).set({"max_leverage": 10}, "why", "lev10")
    p = ResearchParams(s("live"), live)                                  # AI_PARAMS_IN_LIVE=true
    assert p.effective() == RiskLimits() and p.experiment() is None
    assert ResearchParams(s("demo"), demo).effective().max_leverage == 10


# ------------------------------------------------------------ resting entries
def desk(j, fake=None):
    fake = fake or FakeCcxt()
    return EntryDesk(Trader(FakeEx(fake), j, prechecks=ok_prechecks, budget=neutral_budget), regime_fn=favorable), fake


async def test_a_live_guardian_never_polls_or_counts_a_demo_entry(tmp_path):
    demo, live = pair(tmp_path)
    d, _ = desk(demo)
    eid = (await d.propose_group([prop(1.04, tranche="t1", leg=1)]))["placed"][0]["entry_id"]
    assert d._rows("id=?", (eid,))[0]["mode"] == "demo"
    demo.db.execute("INSERT INTO entries (group_id, symbol, status, created_at, expires_ms, limit_price, amount,"
                    " order_id) VALUES ('external', 'X/USDT:USDT', 'external', 'x', 0, 1.0, 5, '9')")
    demo.db.commit()
    lv, live_fake = desk(live)
    assert lv.pending(all_orders=True) == [] and await lv.poll() == [] and live_fake.orders == []
    assert external_orders(live) == [] and len(external_orders(demo)) == 1
    assert tranches.pending_legs(live, prop(1.04)["symbol"]) == [] and len(
        tranches.pending_legs(demo, prop(1.04)["symbol"])) == 1
    assert len(d.pending()) == 1                                          # demo still sees its own entry
