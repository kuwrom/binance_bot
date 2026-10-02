"""#64 (docs/PROPOSALS.md P1b): the ADL quantile of every open short, read once per watch pass. The top bucket alerts,
a persistent one escalates once, and an answer that can't be read is a loud watch failure, never a silent skip."""

import json
import logging

import pytest

from shortbot import margin
from shortbot.alerts_view import is_urgent
from tests.fakes import SYM, FakeCcxt
from tests.test_guardian import guardian_with, kinds, open_trade


def rows(g, kind):
    return [a for a in g.j.alerts(500) if a["kind"] == kind]


@pytest.mark.parametrize("resp, want, problems", [
    ([], {}, 0),                                                                          # flat (demo, 2026-10-02)
    ([{"symbol": "BTCUSDT", "adlQuantile": {"LONG": 0, "SHORT": 0, "BOTH": 4}}], {"BTCUSDT": 4}, 0),
    ({"symbol": "BTCUSDT", "adlQuantile": {"LONG": 0, "SHORT": 0, "BOTH": "2"}}, {"BTCUSDT": 2}, 0),  # one object
    ([{"symbol": "BTCUSDT", "adlQuantile": {"LONG": 1, "SHORT": 2}}], {"BTCUSDT": 2}, 1),  # no BOTH: SHORT, noted
    ([{"symbol": "ETHUSDT", "adlQuantile": {"LONG": 3, "SHORT": 3, "HEDGE": 0}}], {}, 1),  # crossed hedge mode
    ([{"symbol": "BTCUSDT", "adlQuantile": {"BOTH": 7}}], {}, 1),
    ([{"symbol": "BTCUSDT", "adlQuantile": {"BOTH": "x"}}], {}, 1),
    ([{"symbol": "BTCUSDT", "adlQuantile": {"BOTH": 2.5}}], {}, 1),
    ([{"symbol": "BTCUSDT"}, "junk"], {}, 2),
    ("oops", {}, 1),
    (None, {}, 1),
])
def test_the_adl_parser_is_tolerant_and_names_what_it_could_not_read(resp, want, problems):
    got, bad = margin.parse_adl(resp)
    assert got == want and len(bad) == problems


async def test_the_top_bucket_alerts_once_a_persistent_one_escalates_once_and_its_end_is_announced():
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    fake.sym[SYM]["adl"] = 4
    for _ in range(margin.ADL_PERSIST_PASSES + 3):
        await g.watch_positions()
    (a,) = rows(g, "adl_risk")
    assert a["symbol"] == SYM and "effective leverage" in a["message"] and "partial" in a["message"]
    assert len(rows(g, "adl_risk_persistent")) == 1 and not rows(g, "position_watch_error")
    fake.sym[SYM]["adl"] = 2
    await g.watch_positions()
    assert len(rows(g, "adl_risk_cleared")) == 1 and not g.adl_streak.fails
    assert is_urgent("adl_risk_persistent", SYM) and not is_urgent("adl_risk", SYM)


async def test_the_second_highest_bucket_and_a_short_streak_stay_quiet():
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    fake.sym[SYM]["adl"] = 3
    await g.watch_positions()
    assert not rows(g, "adl_risk")
    fake.sym[SYM]["adl"] = 4
    for _ in range(margin.ADL_PERSIST_PASSES - 1):
        await g.watch_positions()
    fake.sym[SYM]["adl"] = 1
    await g.watch_positions()
    assert not rows(g, "adl_risk_persistent") and not rows(g, "adl_risk_cleared") and not g.adl_streak.fails


async def test_effective_leverage_is_notional_over_isolated_margin_and_is_logged(caplog):
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    fake.price = 0.9   # a winning short: unrealized profit adds to the isolated margin, once
    (p,) = await fake.fetch_positions()
    info = p["info"]
    assert margin.effective_leverage(info) == pytest.approx(90 / (50 + 10), abs=0.01)   # 2x: wallet 50, upnl +10
    with caplog.at_level(logging.INFO, logger="shortbot.guardian"):
        await g.watch_positions()
    assert "adl PUMP/USDT:USDT quantile 0" in caplog.text and "effective leverage 1.5" in caplog.text


@pytest.mark.parametrize("answer", [[], [{"symbol": "PUMPUSDT", "adlQuantile": {"BOTH": "?"}}], "garbage"])
async def test_an_answer_without_our_short_is_a_loud_watch_failure_that_escalates(caplog, answer):
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    fake.adl_answer = answer
    with caplog.at_level(logging.INFO, logger="shortbot.guardian"):
        for _ in range(3):
            await g.watch_positions()
    (err,) = rows(g, "position_watch_error")
    assert "adl" in err["message"] and err["symbol"] == SYM
    assert kinds(g).count("guardian_failing:watch") == 1 and "ADL" in rows(g, "guardian_failing:watch")[0]["message"]
    assert "adlQuantile" in caplog.text and "ERROR" in caplog.text


async def test_a_failed_adl_read_never_costs_the_squeeze_checks():
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    ran = []

    async def squeeze(sym):
        ran.append(sym)

    async def down(req=None):
        raise RuntimeError("adlQuantile 503")
    g._squeeze_and_funding, fake.fapiPrivateGetAdlQuantile = squeeze, down
    await g.watch_positions()
    assert ran == [SYM] and "adlQuantile 503" in rows(g, "position_watch_error")[0]["message"]
    assert g.watch_streak.fails == {SYM: 1}


async def test_no_short_no_adl_read_and_an_empty_answer_is_no_failure():
    fake = FakeCcxt()
    g = guardian_with(fake)
    calls = []
    real = fake.fapiPrivateGetAdlQuantile

    async def counted(req=None):
        calls.append(req)
        return await real(req)
    fake.fapiPrivateGetAdlQuantile = counted
    await g.watch_positions()
    assert calls == [] and not kinds(g)


async def test_the_first_non_empty_answer_is_recorded_once_for_the_demo_probe():
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    fake.sym[SYM]["adl"] = 1
    await g.watch_positions()
    first = g.j.get("adl_sample")
    assert json.loads(first) == [{"symbol": "PUMPUSDT", "adlQuantile": {"LONG": 0, "SHORT": 0, "BOTH": 1}}]
    fake.sym[SYM]["adl"] = 2
    await g.watch_positions()
    assert g.j.get("adl_sample") == first


async def test_a_closed_short_leaves_the_adl_streak():
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    fake.sym[SYM]["adl"] = 4
    await g.watch_positions()
    assert g.adl_streak.fails == {SYM: 1}
    fake.trigger_stop()
    await g.watch_positions()
    assert not g.adl_streak.fails
