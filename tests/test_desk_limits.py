"""Desk limits (desk_limits.py): defaults -> .env -> the human's per-desk limits -> research overrides (demo only), and
the equity-relative caps (max_position_equity_x, max_total_equity_x) in every notional check."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from shortbot import desk_limits as dl
from shortbot import governor
from shortbot import killswitch as ks
from shortbot.config import RiskLimits, Settings
from shortbot.exchange import Exchanges
from shortbot.journal import Journal
from shortbot.params import ResearchParams
from shortbot.risk import AccountState, AddRequest, RiskViolation, ShortRequest, check_add, check_open_short
from tests.fakes import SYM, FakeCcxt, FakeEx
from tests.test_entries import desk, prop


def settings(mode="demo", allow=False, **lim):
    return Settings(mode, "", "", ":memory:", "", "", RiskLimits(**lim), allow_ai_params_live=allow)


def put(j, mode=None, **vals):
    """The desk-limits row as shortbot-admin writes it."""
    assert dl.write(j, vals, j.get(dl.key(j, mode)), mode)


# ------------------------------------------------------------ precedence
def test_default_then_env_then_desk_then_research(monkeypatch):
    j = Journal(":memory:")
    assert dl.baseline(settings(), j).max_leverage == 3                          # built-in default
    monkeypatch.setenv("MAX_LEVERAGE", "4")
    s = Settings("demo", "", "", ":memory:", "", "", RiskLimits.from_env())
    assert dl.baseline(s, j).max_leverage == 4                                    # .env
    assert dl.describe(s, j)["fields"]["max_leverage"]["source"] == ".env"
    put(j, max_leverage=2, daily_loss_limit_pct=2.0)
    assert dl.baseline(s, j).max_leverage == 2 and dl.baseline(s, j).daily_loss_limit_pct == 2.0   # desk
    p = ResearchParams(s, j)
    assert p.effective().max_leverage == 2 and p.base().max_leverage == 2
    p.set({"max_leverage": 5}, "test 5x", "lev5")
    assert p.effective().max_leverage == 5 and p.effective().daily_loss_limit_pct == 2.0   # research over desk
    rows = dl.describe(s, j, p)["fields"]
    assert (rows["max_leverage"]["source"], rows["daily_loss_limit_pct"]["source"],
            rows["max_positions"]["source"]) == ("research", "desk", "default")
    assert rows["max_leverage"]["desk"] == 2 and rows["max_leverage"]["env"] == 4


def test_live_ignores_research_unless_the_human_allows_it_and_desks_are_isolated(tmp_path):
    j_demo, j_live = Journal(tmp_path / "j.db"), Journal(tmp_path / "j.db", "live")   # one journal, two desks
    put(j_live, max_leverage=2)
    assert dl.baseline(settings(), j_demo).max_leverage == 3                    # live's row isn't demo's
    assert dl.baseline(settings("live"), j_live).max_leverage == 2
    assert dl.baseline(settings(), j_demo, "live").max_leverage == 2            # read across desks (go-live report)
    j_live.set(j_live.mode_key("research_params"), json.dumps({"max_leverage": 5}))
    assert ResearchParams(settings("live"), j_live).effective().max_leverage == 2
    assert ResearchParams(settings("live", allow=True), j_live).effective().max_leverage == 5


def test_exchange_limits_and_the_guardian_see_the_desk_baseline():
    s, j = settings(), Journal(":memory:")
    ex = Exchanges(s)
    assert ex.limits.max_positions == 5                                         # no journal: .env
    ex.journal = j
    put(j, max_positions=2)
    assert ex.limits.max_positions == 2                                         # re-read on each use: no restart
    ex.params = ResearchParams(s, j)
    assert ex.limits.max_positions == 2
    from shortbot import origin
    ex.params.set({"max_positions": 7}, "more slots", "slots7")
    assert ex.limits.max_positions == 7
    with origin.guardian(None):
        assert ex.limits.max_positions == 2                                     # the guardian: baseline, no research


def test_an_invalid_stored_value_is_ignored_not_fatal():
    j = Journal(":memory:")
    j.set(dl.key(j), json.dumps({"max_leverage": 0, "risk_per_trade_pct": "x", "nonsense": 1, "max_positions": 3}))
    assert dl.stored(j) == {"max_positions": 3}
    j.set(dl.key(j), "{not json")
    assert dl.stored(j) == {} and dl.baseline(settings(), j) == settings().limits


def test_coerce_rules():
    assert dl.coerce("max_leverage", "2") == 2 and isinstance(dl.coerce("max_positions", "3.0"), int)
    assert dl.field_name("MAX_TOTAL_EQUITY_X") == "max_total_equity_x"
    for k, v, why in (("max_leverage", "0.5", "whole"), ("max_leverage", "0", "positive"),
                      ("daily_loss_limit_pct", "150", "<= 100"), ("max_total_equity_x", "25", "<= 20"),
                      ("max_position_notional", "-1", "positive"), ("max_positions", "abc", "not a number"),
                      ("max_position_notional", "nan", "finite")):
        with pytest.raises(dl.LimitError, match=why):
            dl.coerce(k, v)
    assert dl.coerce("max_adds", "0") == 0                                      # 0 means "no adds"


def test_env_exposes_the_equity_caps(monkeypatch):
    monkeypatch.setenv("MAX_POSITION_EQUITY_X", "0.5")
    lim = RiskLimits.from_env()
    assert lim.max_position_equity_x == 0.5 and lim.max_total_equity_x is None


# ------------------------------------------------------------ equity-relative caps
ACCT = AccountState(equity=1000.0, open_symbols=(), total_open_notional=0.0, day_pnl=0.0)


def req(notional):
    return ShortRequest(SYM, notional, 1.0, 1.05, 2, 50e6)


def test_equity_caps_bind_when_tighter_and_not_when_unset():
    lim = RiskLimits(max_position_notional=500, max_total_notional=2000, risk_per_trade_pct=3.0)
    check_open_short(req(400), ACCT, lim)                                       # unset: the USDT caps only
    with pytest.raises(RiskViolation, match=r"per-coin max 300 \(0.3x equity"):
        check_open_short(req(400), ACCT, replace(lim, max_position_equity_x=0.3))
    check_open_short(req(400), ACCT, replace(lim, max_position_equity_x=5))    # looser than 500 USDT: no effect
    busy = replace(ACCT, open_symbols=("X",), total_open_notional=900)
    with pytest.raises(RiskViolation, match=r"total notional would be 1300.00 > max 1000 \(1x equity"):
        check_open_short(req(400), busy, replace(lim, max_total_equity_x=1.0))
    check_open_short(req(400), busy, lim)
    assert lim.position_cap(None) == 500 and lim.position_cap(0) == 500


def test_equity_caps_bind_an_add():
    lim = RiskLimits(max_position_notional=500, max_total_notional=2000, max_position_equity_x=0.15)
    add = AddRequest(SYM, 80, 0.9, 1.0, 100, 1.0, 3, 100, 10.0, 0)   # 180 x 0.9 = 162 USDT after the add
    with pytest.raises(RiskViolation, match=r"per-coin max 150 \(0.15x equity"):
        check_add(add, replace(ACCT, total_open_notional=90), lim)


async def test_equity_cap_from_the_desk_binds_open_short_and_a_limit_entry():
    """FakeCcxt's equity is 10 000: a 0.005x desk cap (50 USDT) is tighter than the 100 USDT default."""
    d, t, fake = desk()
    put(t.j, max_position_equity_x=0.005)
    r = await t.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=80)
    assert not r["ok"] and any("0.005x equity" in x for x in r["rejected_by_risk"])
    r = await d.propose_group([prop(1.04)])
    placed = r.get("placed") or []
    assert placed and all(p["amount"] * p["limit_price"] <= 50 for p in placed), r


async def test_without_a_desk_cap_the_same_short_passes():
    d, t, fake = desk()
    assert (await t.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=80))["ok"]


def test_fake_exchange_follows_the_trader_journal():
    ex = FakeEx(FakeCcxt())
    from shortbot.trading import Trader
    t = Trader(ex, Journal(":memory:"))
    put(t.j, max_leverage=2)
    assert ex.limits.max_leverage == 2


# ------------------------------------------------------------ guardian thresholds
async def test_daily_loss_trip_uses_the_desk_threshold():
    s, j = settings(), Journal(":memory:")
    assert await ks.trip_on_daily_loss(j, s, equity=9800, day_pnl=-200) is None     # -2% < the 3% default
    put(j, daily_loss_limit_pct=1.5)
    assert ks.effective_limits(s, j) == (replace(s.limits, daily_loss_limit_pct=1.5), "desk limits (human)")
    await ks.trip_on_daily_loss(j, s, equity=9800, day_pnl=-200)
    st = ks.read(j)
    assert st["state"] == ks.REDUCING and "1.5%" in st["reason"] and "desk limits" in st["reason"]


def test_weekly_halt_uses_the_desk_threshold():
    s, j = settings(), Journal(":memory:")
    inp = governor.GovInputs(equity=9700, peak_equity=10000, day_pnl=0, week_pnl=-300,
                             now=datetime(2026, 10, 7, 12, tzinfo=UTC))
    assert not governor.govern(ks.effective_limits(s, j)[0], inp)["halt"]     # -3% < the 4% default
    put(j, weekly_loss_limit_pct=2.5)
    g = governor.govern(ks.effective_limits(s, j)[0], inp)
    assert g["halt"] and any("2.5%" in r for r in g["reasons"])
