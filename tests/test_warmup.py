from datetime import datetime, timedelta, timezone

from shortbot.journal import Journal
from shortbot.market.calendar import WINDOWS, blocking, parse_bls_ics, parse_ff
from shortbot.sessions import clock, execution_guard, minutes_to_funding, session_label, us_open_utc
from shortbot.warmup import SCHEMA, desk_state, drop_reasons, gate

NOW = datetime(2026, 9, 30, 10, 5, tzinfo=timezone.utc)


def j_with_trade(closed_h_ago: float) -> Journal:
    j = Journal(":memory:")
    tid = j.create_trade("X/USDT:USDT", "pump_fade", "t", 1.1, 3)
    j.activate_trade(tid, 1.0, 10, 10)
    j.update_trade(tid, status="closed", closed_at=(NOW - timedelta(hours=closed_h_ago)).isoformat())
    return j


def test_cold_when_idle_or_new_day_warm_otherwise():
    assert desk_state(Journal(":memory:"), NOW)["cold"]
    assert not desk_state(j_with_trade(2), NOW)["cold"]
    assert desk_state(j_with_trade(7), NOW)["cold"]
    assert desk_state(j_with_trade(11), NOW)["cold"]             # yesterday
    assert gate(j_with_trade(1), NOW) is None
    assert "warm-up" in gate(j_with_trade(8), NOW)


def test_completed_warmup_warms_the_desk():
    j = j_with_trade(20)
    j.db.executescript(SCHEMA)
    j.db.execute("INSERT INTO warmups (started_at, t0_ms, symbols, snap0, checked_at, status) VALUES (?,?,?,?,?,?)",
                 ("x", 0, "[]", "{}", (NOW - timedelta(minutes=3)).isoformat(), "done"))
    assert gate(j, NOW) is None


def test_open_position_is_warm():
    j = Journal(":memory:")
    tid = j.create_trade("X/USDT:USDT", "pump_fade", "t", 1.1, 3)
    j.activate_trade(tid, 1.0, 10, 10)
    assert not desk_state(j, NOW)["cold"]


def test_drop_reasons():
    s0 = {"last": 1.0, "high_6h": 1.05, "oi": 100, "funding_rate": 0.0001, "funding_interval_h": 8,
          "spread_bp": 3, "depth_1pct_usd": 100_000}
    assert drop_reasons(s0, {**s0, "last": 0.99}, 0.2) == []
    assert "new local high" in drop_reasons(s0, {**s0, "last": 1.06}, 0)[0]
    assert "fresh longs" in drop_reasons(s0, {**s0, "last": 1.01, "oi": 104}, 0)[0]
    assert "negative" in drop_reasons(s0, {**s0, "funding_rate": -0.0002}, 0)[0]
    assert "1h" in drop_reasons(s0, {**s0, "funding_interval_h": 1}, 0)[0]
    assert "halved" in drop_reasons(s0, {**s0, "depth_1pct_usd": 40_000}, 0)[0]
    assert "BTC" in drop_reasons(s0, s0, 1.4)[0]


def test_clock():
    assert session_label(datetime(2026, 9, 30, 7, 30, tzinfo=timezone.utc)) == "asia+europe"
    assert session_label(datetime(2026, 9, 30, 14, tzinfo=timezone.utc)) == "europe+us"
    assert minutes_to_funding(datetime(2026, 9, 30, 15, 30, tzinfo=timezone.utc)) == 30
    assert minutes_to_funding(datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc)) == 30
    assert minutes_to_funding(datetime(2026, 9, 30, 15, 30, tzinfo=timezone.utc), 1) == 30
    assert us_open_utc(datetime(2026, 9, 30, 12, tzinfo=timezone.utc)).hour == 13        # DST
    assert us_open_utc(datetime(2026, 12, 2, 12, tzinfo=timezone.utc)).hour == 14        # winter
    assert execution_guard(datetime(2026, 9, 30, 15, 59, tzinfo=timezone.utc))
    assert execution_guard(datetime(2026, 9, 30, 11, 0, 30, tzinfo=timezone.utc))
    assert execution_guard(datetime(2026, 9, 30, 11, 7, tzinfo=timezone.utc)) is None
    assert clock(datetime(2026, 9, 28, 3, 23, tzinfo=timezone.utc))["min_to_us_open"] == 607
    c = clock(datetime(2026, 10, 2, 8, 10, tzinfo=timezone.utc))
    assert any("Deribit" in w for w in c["soft_warnings"])


ICS = ("BEGIN:VEVENT\r\nDTSTART;TZID=US-Eastern:20261014T083000\r\nDURATION:PT0M\r\nSUMMARY:Consumer Price Index\r\n"
       "END:VEVENT\r\nBEGIN:VEVENT\r\nDTSTART;TZID=US-Eastern:20261014T083000\r\nSUMMARY:Real Earnings\r\nEND:VEVENT\r\n")


def test_calendar_parsers_and_windows():
    ev = parse_bls_ics(ICS)
    assert len(ev) == 1 and ev[0][2] == "tier1"
    assert datetime.fromtimestamp(ev[0][0] / 1000, timezone.utc).hour == 12            # 08:30 EDT = 12:30 UTC
    ff = parse_ff([{"title": "Core PCE Price Index m/m", "country": "USD", "date": "2026-09-30T08:30:00-04:00",
                    "impact": "High"}, {"title": "x", "country": "EUR", "date": "2026-09-30T08:30:00-04:00",
                                        "impact": "High"}])
    assert [k for _, _, k in ff] == ["tier1"]
    e = {"name": "CPI", "kind": "tier1", "utc": "t"}
    assert blocking([{**e, "minutes_to": 10}]) and blocking([{**e, "minutes_to": -25}])
    assert not blocking([{**e, "minutes_to": 20}]) and not blocking([{**e, "minutes_to": -31}])
    assert WINDOWS["fomc"] == (30, 60)


def test_session_signal_vetoes_inside_macro_window():
    import asyncio

    from shortbot.signals.session import session

    class Ctx:
        async def funding(self):
            return {"interval_h": 8}

        async def macro(self):
            return [{"name": "Consumer Price Index", "kind": "tier1", "utc": "t", "minutes_to": 8.0}]

    out = asyncio.run(session(Ctx()))
    assert out.vetoes and out.features["macro_next_kind"] == "tier1" and "session" in out.features


def test_session_buckets_follow_ny_dst():
    from shortbot.sessions import session_bucket
    d = lambda *a: datetime(*a, tzinfo=timezone.utc)  # noqa: E731
    assert session_bucket(d(2026, 9, 28, 4, 45)) == "asia"        # ONDO fake-out
    assert session_bucket(d(2026, 9, 28, 7, 10)) == "london"      # ONDO flush
    assert session_bucket(d(2026, 9, 28, 13, 29)) == "london"
    assert session_bucket(d(2026, 9, 28, 13, 31)) == "ny"         # 09:30 EDT
    assert session_bucket(d(2026, 12, 7, 14, 0)) == "london"      # winter: NY opens 14:30 UTC
    assert session_bucket(d(2026, 9, 27, 4, 0)) == "asia_wknd"
