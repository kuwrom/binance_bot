"""Cooldowns and dedupes hold while their markers are queued (#81). Alert stamps, news and announcement markers and
the empty-positions flag are posted (#79): while another process holds the journal's write lock they wait in the
queue, and a check that read only the journal repeated its alert on every pass (EC2: "plan:review due" each pass of a
hold). Journal.mark keeps this process's markers readable until they land."""

import sqlite3
from datetime import datetime, timezone

from shortbot import guardian_jobs, guardian_watch, reconcile
from shortbot.config import RiskLimits, Settings
from shortbot.journal import Journal
from shortbot.notify import Notifier
from tests.fakes import SYM
from tests.held_lock import STALL_REPORT, HeldLock
from tests.test_guardian import open_trade
from tests.test_guardian_alerts import NO_AUTO, rows
from tests.test_guardian_offloop import guardian_on

LOGGED = Settings("demo", "", "", ":memory:", "", "", RiskLimits())   # no Telegram: an alert is logged, and True


async def passes(j: Journal, path, n: int, one) -> None:
    """n passes inside one hold, none of them waiting for it; then the queue lands."""
    with HeldLock(path, 5.0) as held:
        for _ in range(n):
            await one()
        assert j.w.pending() and held.p.poll() is None             # every pass ran while the lock was held
    await j.flush()


async def test_a_mark_is_read_back_at_once_and_lands_in_order(tmp_path):
    j = Journal(tmp_path / "j.db")
    other = sqlite3.connect(tmp_path / "j.db")                       # what every other process reads
    with HeldLock(tmp_path / "j.db", 0.3):
        j.mark("k", "1")
        j.mark("k", "2")
        assert j.get("k") == "2" and other.execute("SELECT v FROM kv WHERE k='k'").fetchone() is None
        await j.flush()
    assert j.get("k") == other.execute("SELECT v FROM kv WHERE k='k'").fetchone()[0] == "2" and not j._marks


@STALL_REPORT
async def test_an_alert_with_a_1h_cooldown_fires_once_across_20_held_passes(tmp_path):
    j = Journal(tmp_path / "j.db")
    n, sent = Notifier(LOGGED, j), []

    async def one():
        sent.append(await n.send("stop_near", "close to the stop", SYM, cooldown_s=3600))
    await passes(j, tmp_path / "j.db", 20, one)
    assert sent.count(True) == 1 and len(j.alerts()) == 1
    assert j.get(f"alert_ts:stop_near:{SYM}")                       # the journal stays the cross-process record


@STALL_REPORT
async def test_the_plan_review_alert_fires_once_across_20_held_passes(tmp_path):
    g = guardian_on(tmp_path / "j.db")
    await open_trade(g, auto=NO_AUTO, review_every_hours=1)
    await passes(g.j, tmp_path / "j.db", 20, g.check_positions)
    assert len(rows(g, "plan:review due")) == 1


@STALL_REPORT
async def test_a_news_item_on_a_short_is_alerted_once_across_held_passes(tmp_path, monkeypatch):
    g = guardian_on(tmp_path / "j.db")
    del g._news                                                      # the real check (guardian_on quiets it)

    async def news_for(coin, max_items=5):
        return {"binance_announcements": [], "headlines": [{"url": "https://example.com/a", "title": "hack"}]}
    monkeypatch.setattr(guardian_watch.news, "news_for", news_for)
    await passes(g.j, tmp_path / "j.db", 5, lambda: g._news(SYM))
    assert len(rows(g, "news_on_position")) == 1


@STALL_REPORT
async def test_a_listing_announced_during_a_held_first_run_is_announced_once(tmp_path, monkeypatch):
    g = guardian_on(tmp_path / "j.db")
    now = datetime.now(timezone.utc).isoformat()
    old = {"catalog": "New Listing", "url": "https://example.com/old", "title": "FOOUSDT perpetual", "time": now}
    new = {**old, "url": "https://example.com/new", "title": "BARUSDT perpetual"}
    seen = iter([[old]] + [[old, new]] * 4)                          # the first run only marks what is listed

    async def announcements():
        return next(seen)
    monkeypatch.setattr(guardian_jobs.news, "binance_announcements", announcements)
    await passes(g.j, tmp_path / "j.db", 5, g.check_announcements)
    assert [a["message"].split("\n")[0] for a in rows(g, "binance_new_listing")] == ["BARUSDT perpetual"]


@STALL_REPORT
async def test_an_empty_positions_answer_is_alerted_once_per_streak_across_held_passes(tmp_path):
    g = guardian_on(tmp_path / "j.db")
    await open_trade(g)
    await passes(g.j, tmp_path / "j.db", 5, lambda: reconcile.reconcile(g.app.trader, []))
    assert len(rows(g, "positions_empty")) == 1
