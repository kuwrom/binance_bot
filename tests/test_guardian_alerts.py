"""Guardian alert call sites (#23): stable cooldown keys, no false alarms, wording that matches what happened; and
the watch lane's per-symbol streak (#57): a failed squeeze input is alerted and escalates, never silently skipped."""

import time
from datetime import datetime, timedelta, timezone

import pytest

from shortbot import guardian_jobs as gj
from shortbot import killswitch as ks
from shortbot.guardian_positions import UNTRACKED_GRACE_S
from shortbot.journal import now_iso
from tests.fakes import SYM, FakeCcxt
from tests.test_guardian import guardian_with, kinds, open_trade

NO_AUTO = {"breakeven": False, "trail": False, "targets": False, "time_stop": False}
FLAT = [[k, 1, 1, 1, 1.0, 1] for k in range(13)]


async def open_plan(g, **plan):
    return await open_trade(g, auto=NO_AUTO, review_every_hours=1000, **plan)


def rows(g, kind):
    return [a for a in g.j.alerts(500) if a["kind"] == kind]


def spy(g):
    calls, real = [], g.n.send

    async def send(kind, message, symbol=None, **kw):
        calls.append((kind, symbol, kw.get("urgent", False)))
        return await real(kind, message, symbol, **kw)
    g.n.send = send
    return calls


# ------------------------------------------------------------ plan alert keys
async def test_time_stop_alert_keeps_its_cooldown_as_the_trade_ages():
    g = guardian_with(FakeCcxt())
    tid = await open_plan(g, time_stop_hours=1)
    for age_h in (2.0, 2.1, 2.2, 2.3):   # 18 minutes of passes, well inside the 4 h cooldown
        g.j.update_trade(tid, opened_at=(datetime.now(timezone.utc) - timedelta(hours=age_h)).isoformat())
        await g.check_positions()
    assert len(rows(g, "plan:time stop")) == 1


async def test_target_alert_keeps_its_cooldown_as_pnl_moves_and_each_target_has_its_own():
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_plan(g, targets=[{"price": 0.95, "fraction": 0.5}, {"price": 0.9, "fraction": 0.5}])
    for px in (0.94, 0.93, 0.92, 0.89):
        fake.price = px
        await g.check_positions()
    assert len(rows(g, "plan:target 0.95")) == 1 and len(rows(g, "plan:target 0.9")) == 1


# ------------------------------------------------------------ false alarms and wording
async def test_untracked_alert_waits_for_the_grace_and_for_nothing_to_explain_the_position():
    fake = FakeCcxt()
    g = guardian_with(fake)
    fake.position, fake.entry, fake.stops["s1"] = 50, 1.0, 1.1   # opened in the Binance app
    await g.check_positions()
    assert "untracked_position" not in kinds(g)                 # inside the grace: maybe a fill being journalled
    g.untracked_since[SYM] -= UNTRACKED_GRACE_S
    eid = g.j._insert("INSERT INTO entries (group_id, symbol, status, created_at, expires_ms) VALUES ('g', ?, "
                      "'pending', ?, 0)", (SYM, now_iso()))
    await g.check_positions()
    assert "untracked_position" not in kinds(g)                 # a limit of ours explains it
    g.j.db.execute("UPDATE entries SET status='cancelled' WHERE id=?", (eid,))
    g.j.db.commit()
    await g.check_positions()
    g.untracked_since[SYM] -= UNTRACKED_GRACE_S
    await g.check_positions()
    assert kinds(g).count("untracked_position") == 1 and ks.read(g.j)["state"] == ks.REDUCING


async def test_no_stop_missing_alert_when_the_restore_finds_a_stop_under_the_lock(monkeypatch):
    g = guardian_with(FakeCcxt())

    async def found_one(sym, pct):
        return None
    monkeypatch.setattr(g.app.trader, "ensure_stop", found_one)
    await g._restore_stop(SYM)
    assert "stop_missing" not in kinds(g) and SYM not in g.stop_fails


async def test_forced_close_says_so_when_the_position_was_already_gone(monkeypatch):
    g = guardian_with(FakeCcxt())
    g.stop_fails[SYM] = 2

    async def fails(sym, pct):
        raise RuntimeError("-2010")

    async def gone(sym):
        return None
    monkeypatch.setattr(g.app.trader, "ensure_stop", fails)
    monkeypatch.setattr(g.app.trader, "position", gone)
    await g._restore_stop(SYM)
    msg = rows(g, "forced_close")[0]["message"]
    assert "already gone" in msg and "CLOSED" not in msg


@pytest.mark.parametrize("loss, fires", [(300, False), (400, True)])
async def test_daily_loss_alert_fires_exactly_when_the_kill_switch_trips(loss, fires):
    fake = FakeCcxt()   # equity 10k: -300 is -3.0% of equity now but -2.9% of start-of-day equity (the trip's basis)
    g = guardian_with(fake)
    fake.income.append({"symbol": "PUMPUSDT", "incomeType": "REALIZED_PNL", "income": str(-loss), "asset": "USDT",
                        "time": int(time.time() * 1000)})
    await g.check_positions()
    assert ("daily_loss_limit" in kinds(g)) is fires and (ks.read(g.j)["state"] == ks.REDUCING) is fires
    if fires:
        msg = rows(g, "daily_loss_limit")[0]["message"]
        assert "00:00" not in msg and "REDUCING" in msg and "human" in msg


async def test_delisting_is_urgent_only_for_a_coin_we_are_short_or_about_to_be(monkeypatch):
    g = guardian_with(FakeCcxt())
    await open_trade(g)
    g.j._insert("INSERT INTO entries (group_id, symbol, status, created_at, expires_ms) VALUES ('g', ?, 'pending', ?,"
                " 0)", ("ALPACA/USDT:USDT", now_iso()))
    now = datetime.now(timezone.utc).isoformat()
    titles = ["Binance Futures Will Delist USDⓈ-M PUMPUSDT Perpetual Contract", "Binance Will Delist ALPACA, VIB",
              "Binance Will Delist WING, PDA"]

    async def announcements():
        return [{"catalog": "Delisting", "title": t, "url": f"u{i}", "time": now} for i, t in enumerate(titles)]
    monkeypatch.setattr(gj.news, "binance_announcements", announcements)
    g.j.set("ann_initialised", "1")
    calls = spy(g)
    await g.check_announcements()
    assert sorted(calls, key=str) == sorted([("binance_delisting", SYM, True),
                                             ("binance_delisting", "ALPACA/USDT:USDT", True),
                                             ("binance_delisting", None, False)], key=str)


async def test_every_decaying_pattern_is_alerted(monkeypatch):
    g = guardian_with(FakeCcxt())

    async def nothing(*a, **k):
        return 0
    for target in ("shortbot.research.label_outcomes", "shortbot.research.label_post_exit",
                   "shortbot.guardian_jobs.label_predictions", "shortbot.laggard.label"):
        monkeypatch.setattr(target, nothing)
    monkeypatch.setattr("shortbot.registry.decay_alarms", lambda db: [
        {"id": "p1", "status": "decaying", "verdict": "edge gone"}, {"id": "p2", "status": "decaying", "verdict": "x"}])
    await g.label_research()
    assert len([k for k in kinds(g) if k.startswith("pattern_decay")]) == 2


# ------------------------------------------------------------ #57: the watch lane's per-symbol streak
def watch_inputs(g, monkeypatch):
    """The real squeeze/funding check over stubbed public inputs; set a value to an Exception to make it raise."""
    del g._squeeze_and_funding   # guardian_with stubs it out
    inputs = {"candles": FLAT, "oi": [{"oi": 100.0}] * 13, "funding": {"fundingRate": 0.0001}, "intervals": {}}

    def feed(name):
        async def fetch(*a, **k):
            if isinstance(inputs[name], Exception):
                raise inputs[name]
            return inputs[name]
        return fetch
    monkeypatch.setattr(g.ex.public, "fetch_ohlcv", feed("candles"), raising=False)
    monkeypatch.setattr(g.ex.public, "fetch_funding_rate", feed("funding"), raising=False)
    monkeypatch.setattr(g.ex, "oi_history", feed("oi"), raising=False)
    monkeypatch.setattr(g.ex, "funding_intervals", feed("intervals"), raising=False)
    return inputs


async def test_a_failed_squeeze_input_is_alerted_escalates_per_symbol_and_recovers(monkeypatch):
    g = guardian_with(FakeCcxt())
    await open_trade(g)
    inputs = watch_inputs(g, monkeypatch)
    inputs["candles"], inputs["funding"] = RuntimeError("klines 503"), {"fundingRate": -0.0005}
    for _ in range(3):
        await g.watch_positions()
    got = [(a["kind"], a["symbol"]) for a in g.j.alerts(100)]
    err = rows(g, "position_watch_error")
    assert len(err) == 1 and "candles" in err[0]["message"] and err[0]["symbol"] == SYM
    assert got.count(("guardian_failing:watch", SYM)) == 1
    assert ("funding_negative", SYM) in got   # the checks that had their inputs still ran
    inputs["candles"] = FLAT
    await g.watch_positions()
    assert kinds(g).count("guardian_recovered:watch") == 1 and not g.watch_streak.fails


@pytest.mark.parametrize("broken, value", [("oi", RuntimeError("openInterestHist 503")), ("candles", []),
                                           ("oi", [])])
async def test_a_squeeze_check_never_skips_silently(monkeypatch, broken, value):
    g = guardian_with(FakeCcxt())
    await open_trade(g)
    inputs = watch_inputs(g, monkeypatch)
    inputs["candles"] = [[k, 1, 1, 1, 1 + 0.005 * k, 1] for k in range(13)]   # +6% in 1h: squeeze territory
    inputs[broken] = value
    await g.watch_positions()
    assert broken in rows(g, "position_watch_error")[0]["message"] and g.watch_streak.fails == {SYM: 1}


async def test_a_closed_short_leaves_the_watch_streak():
    g = guardian_with(FakeCcxt())

    async def boom(sym):
        raise RuntimeError("down")
    g._squeeze_and_funding = boom
    await open_trade(g)
    await g.watch_positions()
    assert g.watch_streak.fails == {SYM: 1}

    async def none():
        return []
    g.app.trader.raw_positions = none
    await g.watch_positions()
    assert not g.watch_streak.fails


async def test_a_dead_news_feed_is_alerted_once_per_cooldown_never_as_a_watch_failure(monkeypatch):
    from shortbot import news
    from shortbot.guardian_watch import WatchChecks
    g = guardian_with(FakeCcxt())
    g._news = WatchChecks._news.__get__(g)                     # guardian_with silences it

    async def news_for(base, max_items=15):                    # the announcements feed is down, RSS still answers
        return {"coin": base, "binance_announcements": [], "errors": ["binance_announcements"],
                "headlines": [{"title": f"{base} exploit", "url": f"https://news.example/{base}"}]}
    monkeypatch.setattr(news, "news_for", news_for)
    await open_trade(g)
    for _ in range(2):
        await g.watch_positions()
        await g._news("ALT/USDT:USDT")
    down = rows(g, "news_feed_down")
    assert len(down) == 1 and down[0]["symbol"] is None and "binance_announcements" in down[0]["message"]
    assert len(rows(g, "news_on_position")) == 2                # what arrived still alerts, once per item
    assert not g.watch_streak.fails and not rows(g, "position_watch_error")   # squeeze/funding checks are fine
