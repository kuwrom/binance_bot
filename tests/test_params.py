"""AI research overrides: tunable within bounds, logged, ignored in live mode and by the guardian."""

import pytest

from shortbot.config import ResearchBounds, RiskLimits, Settings
from shortbot.journal import Journal
from shortbot.params import ParamError, ResearchParams


def rp(mode="demo", allow_live=False):
    s = Settings(mode, "k", "s", ":memory:", "", "", RiskLimits(), ResearchBounds(), allow_live)
    return ResearchParams(s, Journal(":memory:"))


def test_defaults_equal_baseline():
    p = rp()
    assert p.effective() == RiskLimits() and p.experiment() is None


def test_set_within_bounds_applies_and_is_logged():
    p = rp()
    r = p.set({"max_leverage": 5}, "test 5x on tight-stop fades", "lev5")
    assert r["changed"]["max_leverage"] == {"from": 3, "to": 5}
    assert p.effective().max_leverage == 5 and p.experiment() == "lev5"
    assert p.j.decisions()[0]["action"] == "set_params"


def test_out_of_bounds_unknown_and_missing_reason_rejected():
    p = rp()
    with pytest.raises(ParamError, match="outside research bounds"):
        p.set({"max_leverage": 50}, "why", "x")
    with pytest.raises(ParamError, match="unknown"):
        p.set({"withdraw_everything": 1}, "why", "x")
    with pytest.raises(ParamError, match="required"):
        p.set({"max_leverage": 5}, "", "x")
    with pytest.raises(ParamError, match="whole number"):
        p.set({"max_positions": 2.5}, "why", "x")
    assert p.effective() == RiskLimits()


def test_inconsistent_values_rejected():
    with pytest.raises(ParamError, match="max_total_notional"):
        rp().set({"max_position_notional": 900, "max_total_notional": 400}, "why", "x")


def test_reset():
    p = rp()
    p.set({"max_leverage": 5}, "why", "x")
    p.reset("done")
    assert p.effective() == RiskLimits() and p.experiment() is None


def test_live_mode_ignores_overrides_unless_human_allows():
    p = rp("live")
    with pytest.raises(ParamError, match="live mode"):
        p.set({"max_leverage": 5}, "why", "x")
    p2 = rp("live", allow_live=True)
    p2.set({"max_leverage": 5}, "why", "x")
    assert p2.effective().max_leverage == 5


def test_experiment_tagged_in_journal_stats():
    j = Journal(":memory:")
    tid = j.create_trade("A", "pump_fade", "t", 1.1, 5, experiment="lev5")
    j.activate_trade(tid, 1, 1, 1)
    j.close_trade(tid, 0.9, 0.1, "tp")
    assert j.stats()["by_experiment"]["lev5"]["n"] == 1
