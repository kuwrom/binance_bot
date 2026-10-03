"""The monitor and research lanes and the guardian's stores never write on the event loop outside a non-waiting unit
(#79): every job runs for real over faked network inputs, under the loop-write guard (tests/loop_guard.py); the
CPU-heavy registry feed runs in a worker thread."""

import asyncio
import threading
import time
from types import SimpleNamespace

from shortbot import guardian_jobs as gj
from shortbot import entry_gates, intents, refresh, registry_seed, research, sweeps
from shortbot import killswitch as ks
from shortbot.journal import Journal
from shortbot.market import calendar, market_map, social, supply
from shortbot.probability import labels
from tests.fakes import SYM
from tests.held_lock import STALL_REPORT, HeldLock
from tests.loop_guard import connections, loop_writes
from tests.test_guardian_offloop import guardian_on
from tests.test_journal_writer import ticking

OLD_MS = int(time.time() * 1000) - 100 * 3_600_000


def fake_inputs(g, monkeypatch) -> None:
    async def announcements():
        return [{"catalog": "New Listing", "title": f"Will list X{i}", "url": f"u{i}", "time": "2026-10-03T00:00:00+00:00"}
                for i in range(3)]

    async def regime(ex):
        return {"verdict_for_shorts": "caution"}

    async def governor(trader, reg, limits=None):
        return {"halt": True, "reasons": ["drawdown"], "drawdown_pct": 99.0}   # trips the kill switch too

    async def perps():
        return {SYM: {"info": {}}}

    async def rates():
        return {SYM: {"fundingRate": 0.0001}}

    async def oi(sym):
        return {"openInterestAmount": 1.0}

    async def tickers():
        return {SYM: {"symbol": SYM, "last": 1.0, "quoteVolume": 1e8, "percentage": 5.0}}

    async def empty(*a, **k):
        return {}

    async def nothing(*a, **k):
        return []

    async def no_label(ex, r, held):
        raise RuntimeError("no bars")

    async def spawn():
        return SimpleNamespace(returncode=None, pid=1)

    async def build(ex, journal):
        return {"ts": int(time.time()), "header": {}, "sectors": [], "vetoes": {"pause": []}, "coins": {}}

    class NoHttp:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise RuntimeError("offline")
    monkeypatch.setattr(gj.news, "binance_announcements", announcements)
    monkeypatch.setattr(gj, "market_regime", regime)
    monkeypatch.setattr(gj, "governor_status", governor)
    monkeypatch.setattr(refresh, "due", lambda j, now=None: True)
    monkeypatch.setattr(refresh, "spawn", spawn)
    monkeypatch.setattr(research, "_outcome", no_label)
    monkeypatch.setattr(labels, "fetch_mark_1m", nothing)
    monkeypatch.setattr(market_map, "build_map", build)
    monkeypatch.setattr(supply, "fetch_supply", empty)
    monkeypatch.setattr(sweeps, "_fetch_1m", nothing)
    monkeypatch.setattr(calendar.httpx, "AsyncClient", NoHttp)
    for name in ("coingecko_trending", "binance_hype"):
        monkeypatch.setattr(social, name, empty)
    monkeypatch.setattr(social, "square_hashtag", lambda base: _value({"posts": 3.0}))
    g.ex.coin_perps = perps
    g.ex.public.fetch_funding_rates, g.ex.public.fetch_open_interest = rates, oi
    g.ex.public.fetch_tickers = tickers


async def _value(v):
    return v


def due_research(j: Journal) -> None:
    """A candidate and a prediction due for labelling, and a ladder stop alert to forward."""
    j.log_candidates("2026-09-28T00:00:00+00:00", OLD_MS, [{"symbol": SYM, "price": 1.0, "score": 40}], "caution")
    j.log_prediction("scan", SYM, 1.0, 1.1, 0.9, 1.0, "bracket", 0.4, ts_ms=OLD_MS)
    j.log_alert("setup_ladder_stop", "pump_fade hit its ladder stop", None)


async def test_monitor_and_research_jobs_never_write_on_the_loop_outside_a_unit(tmp_path, monkeypatch):
    g = guardian_on(tmp_path / "j.db")
    fake_inputs(g, monkeypatch)
    due_research(g.j)
    with loop_writes(*connections(g)) as bad:
        for job in (g.check_announcements, g.check_governor, g.research_refresh, g.check_processes, g.housekeeping,
                    g.setup_ladder_check, g.forward_ladder_stops, g.record_metrics, g.label_research, g.market_map,
                    g.refresh_supply, g.refresh_calendar, g.sweep_study, g.laggard_study):
            await job()
        await social.snapshot(g.social_store, ["BTC"])
        g.liq_store.w.post(g.liq_store.record, [(OLD_MS - 40 * 86_400_000, SYM, "long_liquidated", 1.0, 1.0, 1.0)])
        assert await g.liq_store.prune() == 1
    assert not bad, "\n".join(sorted({b.split(": ")[0] for b in bad}))
    assert g.j.get("ann_initialised") == "1" and g.j.get(refresh.KV_KEY)
    assert g.j.db.execute("SELECT label_tries FROM candidates").fetchone()[0] == 1   # its failure, journalled
    assert g.j.db.execute("SELECT outcome FROM predictions").fetchone()[0] is not None
    assert g.j.db.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 1
    assert g.j.db.execute("SELECT COUNT(*) FROM kv WHERE k LIKE ?", (gj.KV_STOP_SENT + "%",)).fetchone()[0] == 1
    assert ks.read(g.j)["state"] != ks.ACTIVE and g.j.get(g.j.mode_key(entry_gates.GOV_KEY))   # tripped, published
    assert g.social_store.db.execute("SELECT COUNT(*) FROM social_snapshots").fetchone()[0] == 1


async def test_the_registry_feed_runs_off_the_loop_on_its_own_connection(tmp_path, monkeypatch):
    g = guardian_on(tmp_path / "j.db")
    fake_inputs(g, monkeypatch)
    seen = []

    async def labelled(ex, journal, now_ms, limit=120):
        return 1

    def feed(j):
        seen.append((threading.current_thread() is threading.main_thread(), j is g.j, j.w.path))
        return 5
    monkeypatch.setattr(research, "label_outcomes", labelled)
    monkeypatch.setattr(registry_seed, "feed_candidates", feed)
    await g.label_research()
    assert seen == [(False, False, str(tmp_path / "j.db"))]


@STALL_REPORT
async def test_store_writes_wait_for_a_held_lock_off_the_loop(tmp_path, monkeypatch):
    from shortbot.market.liquidations import LiquidationStore
    path = tmp_path / "j.db"
    Journal(path)
    store, cal = LiquidationStore(path), calendar.MacroCalendar(path)
    monkeypatch.setattr(calendar.httpx, "AsyncClient", _Offline)
    gaps, stop = [], asyncio.Event()
    with HeldLock(path, 1.0) as held:
        tick = asyncio.create_task(ticking(gaps, stop))
        store.w.post(store.record, [(int(time.time() * 1000), SYM, "long_liquidated", 1.0, 1.0, 1.0)])
        got = await cal.refresh()
        await store.w.flush()
        stop.set()
        await tick
        released = held.wait()
    assert max(gaps) < 0.1 and time.time() >= released and got == {"bls": "error: RuntimeError",
                                                                    "forexfactory": "error: RuntimeError"}
    assert store.db.execute("SELECT COUNT(*) FROM liquidations").fetchone()[0] == 1
    assert cal.db.execute("SELECT COUNT(*) FROM macro_events WHERE kind='fomc'").fetchone()[0] > 0


class _Offline:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        raise RuntimeError("offline")


def test_intents_columns_are_migrated(tmp_path):
    j = Journal(tmp_path / "j.db")
    for table, cols in intents.COLUMNS.items():
        assert set(cols) <= {r[1] for r in j.db.execute(f"PRAGMA table_info({table})")}
    assert intents.COLUMNS["intents"]["journal_late"] == "INTEGER"


@STALL_REPORT
async def test_a_long_journal_stall_is_alerted_with_the_lock_holder(tmp_path, monkeypatch):
    from shortbot import guardian as gmod
    from shortbot import journal_writer
    monkeypatch.setattr(journal_writer, "STALL_LOG_S", (0.2,))
    monkeypatch.setattr(gmod, "STALL_ALERT_S", 0.2)
    g = guardian_on(tmp_path / "j.db")
    with HeldLock(tmp_path / "j.db", 1.0) as held:
        g.j.post(lambda j: j.set("k", "v"))
        await asyncio.sleep(0.6)                               # the report asks who holds the lock, in a thread
        await g.j.flush()
    (a,) = [a for a in g.j.alerts(20) if a["kind"] == "journal_locked"]
    assert f"pid {held.p.pid}" in a["message"] and g.j.get("k") == "v"
