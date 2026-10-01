"""Limit entries when the exchange moves between two 5 s polls: fills and stops that happen on their own (the fake's
matching engine), on more than one symbol. Public behaviour only: propose_group, poll, Trader, journal rows."""

from datetime import datetime, timedelta, timezone

import pytest

from shortbot.entries import EntryDesk
from shortbot.journal import Journal
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks

SYM2 = "MOVR/USDT:USDT"


async def favorable():
    return "favorable"


def desk():
    fake = FakeCcxt()
    fake.add_symbol(SYM2, 2.0, tick=0.001)
    t = Trader(FakeEx(fake), Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
    return EntryDesk(t, regime_fn=favorable), t, fake


def prop(limit, stop=1.10, symbol=SYM, **kw):
    return {"symbol": symbol, "limit_price": limit, "stop_price": stop, "setup": "pump_fade", "thesis": "sweep",
            "level_type": "prior_high", "level_reason": "swing high", "leverage": 3, **kw}


async def test_7_siblings_on_two_symbols_filling_in_one_poll_stay_protected_and_journaled():
    d, t, fake = desk()
    r = await d.propose_group([prop(1.04), prop(2.08, stop=2.2, symbol=SYM2)])
    assert r["ok"] and len(r["placed"]) == 2
    fake.price = 1.05
    fake.set_price(SYM2, 2.09)
    assert fake.position > 0 and fake.sym[SYM2]["qty"] > 0      # both filled inside one poll interval
    await d.poll()
    for p in await t.positions():                                # whatever the fix keeps open is protected + tracked
        assert await t.stop_orders(p["symbol"], close_position_only=True), f"{p['symbol']} has no whole stop"
        assert p["journal_status"] == "open"
    assert {tr["symbol"] for tr in t.j.trades()} == {SYM, SYM2}   # every fill reaches the journal
    assert d.pending() == []


async def test_8_a_fill_stopped_out_before_the_poll_is_journaled_as_a_round_trip(monkeypatch):
    d, t, fake = desk()
    await d.propose_group([prop(1.04)])
    fake.price = 1.12                 # fills at 1.04, then its pre-armed stop buys back at 1.10: flat before the poll
    assert fake.position == 0 and [f["side"] for f in fake.fills] == ["sell", "buy"]
    # the poll runs a 5 s tick after the fills: a trade stamped "now" would miss them (income/userTrades startTime)
    later = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(timespec="seconds")
    monkeypatch.setattr("shortbot.journal.now_iso", lambda: later)
    await d.poll()
    trades = t.j.trades()
    assert len(trades) == 1 and trades[0]["status"] == "closed" and trades[0]["pnl"] < 0
    assert trades[0]["exit_price"] == pytest.approx(1.10)
    assert d.pending() == []


async def test_8_a_fill_stopped_out_before_the_poll_still_cancels_its_oco_siblings():
    # the round trip cancels its OCO siblings like any fill; the OCO rule in _should_cancel (#7) also retires the
    # siblings of any 'filled' or 'error' row on the next look at them
    d, t, fake = desk()
    await d.propose_group([prop(1.04), prop(2.08, stop=2.2, symbol=SYM2)])
    fake.price = 1.12
    await d.poll()
    assert d.pending() == [] and all(o["status"] != "open" for o in fake.limits.values())
    assert fake.prearms == {}                                    # the sibling's pre-armed stop went with it


async def test_gtd_expiry_on_the_exchange_retires_the_entry_and_its_prearmed_stop():
    d, t, fake = desk()
    await d.propose_group([prop(1.04)], ttl_hours=1)
    fake.advance(3700)                                           # the exchange's clock: GTD fires there, not here
    ev = await d.poll()
    assert [e["event"] for e in ev] == ["expired"] and d.pending() == [] and fake.prearms == {}
    fake.price = 1.2                                             # nothing is left to fill or trigger
    assert fake.fills == []


async def test_split_rollback_cancels_the_prearmed_stop_of_the_unfilled_leg():
    calls = {"n": 0}

    async def veto_second(ex, symbol, ticker):
        calls["n"] += 1
        return {"vetoes": ["breakout accepted"] if calls["n"] > 1 else [], "regime": "favorable", "features": {}}
    d, t, fake = desk()
    fake.price = 1.035
    t.prechecks = veto_second
    r = await d.propose_group([prop(1.04, tranches=[{"limit_price": 1.04}, {"limit_price": 1.07}])])
    assert not r["ok"] and d.pending() == []
    assert fake.prearms == {} and fake.stops == {}


async def test_a_prearmed_stop_alone_protects_a_fill_until_the_poll():
    d, t, fake = desk()
    await d.propose_group([prop(1.04)])
    fake.price = 1.06                                            # filled; the poll has not run yet
    assert fake.position > 0 and list(fake.prearms.values()) == [1.10]
    await d.poll()                                               # the poll swaps it for the whole-position stop
    assert fake.prearms == {} and list(fake.stops.values()) == [1.10]
    assert t.j.active_trade(SYM)["status"] == "open"
