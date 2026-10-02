"""Experiment attribution (#32): every active experiment is named on a trade (a new tag no longer hides the one whose
changes still apply), every trade carries the limits it was sized under (limit fills too, stamped at placement because
the guardian that journals the fill has no research params), and stale overrides are dropped when the human tightens
the bounds."""

import json

import pytest

from shortbot.config import DEFAULT_BOUNDS, ResearchBounds, RiskLimits, Settings
from shortbot.entries import EntryDesk
from shortbot.journal import Journal
from shortbot.params import KV_EXPERIMENT, KV_OVERRIDES, ParamError, ResearchParams
from shortbot.trading import Trader
from tests.fakes import FakeCcxt, FakeEx, neutral_budget, ok_prechecks
from tests.test_entries import favorable, prop


def settings(bounds=None, limits=None, mode="demo", allow_live=False):
    return Settings(mode, "k", "s", ":memory:", "", "", limits or RiskLimits(),
                    ResearchBounds({**DEFAULT_BOUNDS, **(bounds or {})}), allow_live)


def rp(j=None, **kw):
    return ResearchParams(settings(**kw), j or Journal(":memory:"))


def closed(j, experiment, pnl=1.0, limits=None):
    tid = j.create_trade("A/USDT:USDT", "pump_fade", "t", 1.1, 3, entry_features={"limits": limits} if limits else None,
                         experiment=experiment)
    j.activate_trade(tid, 1.0, 10, 10)
    j.close_trade(tid, 0.9, pnl, "tp")
    return tid


# ------------------------------------------------------------ several experiments at once
def test_a_second_experiment_keeps_the_first_in_the_label():
    p = rp()
    p.set({"max_leverage": 10, "risk_per_trade_pct": 3}, "size up", "lev10_risk3")
    r = p.set({"min_quote_volume_24h": 5e6}, "liquidity tiers", "liq_tiers")
    assert p.experiment() == "lev10_risk3 + liq_tiers" and r["active_experiments"] == "lev10_risk3 + liq_tiers"
    assert p.experiments() == {"lev10_risk3": {"max_leverage": 10, "risk_per_trade_pct": 3.0},
                               "liq_tiers": {"min_quote_volume_24h": 5e6}}
    closed(p.j, p.experiment())
    by = p.j.stats()["by_experiment"]
    assert list(by) == ["lev10_risk3 + liq_tiers"]          # never credited to liq_tiers alone
    snap = json.loads(p.j.decisions()[0]["snapshot"])
    assert snap["experiment"] == "liq_tiers" and snap["active"] == "lev10_risk3 + liq_tiers"


def test_an_experiment_that_owns_nothing_any_more_leaves_the_label():
    p = rp()
    p.set({"max_leverage": 5, "risk_per_trade_pct": 2}, "why", "a")
    p.set({"max_leverage": 8}, "why", "b")
    assert p.experiment() == "a + b" and p.experiments()["a"] == {"risk_per_trade_pct": 2.0}
    p.set({"risk_per_trade_pct": 1.5}, "why", "c")
    assert p.experiment() == "b + c" and p.effective().max_leverage == 8
    p.reset("done")
    assert p.experiment() is None and p.experiments() == {}


def test_legacy_overrides_belong_to_the_stored_tag():
    j = Journal(":memory:")
    j.set(KV_OVERRIDES, json.dumps({"max_leverage": 10, "min_quote_volume_24h": 5e6}))
    j.set(KV_EXPERIMENT, "lev10_risk3+liq_tiers")              # written before owners existed
    p = rp(j)
    assert p.experiment() == "lev10_risk3+liq_tiers" and p.effective().max_leverage == 10


def test_a_tag_cannot_contain_the_joiner():
    with pytest.raises(ParamError, match="' \\+ '"):
        rp().set({"max_leverage": 5}, "why", "a + b")


def test_by_experiment_counts_the_parameter_sets_behind_a_label():
    j = Journal(":memory:")
    closed(j, "lev", limits={**RiskLimits().__dict__, "max_leverage": 5})
    closed(j, "lev", limits={**RiskLimits().__dict__, "max_leverage": 8})   # the tag was reused with new values
    closed(j, None, limits=RiskLimits().__dict__)
    by = j.stats()["by_experiment"]
    assert by["lev"]["param_sets"] == 2 and by["baseline"]["param_sets"] == 1


# ------------------------------------------------------------ limit fills
async def test_a_limit_fill_keeps_the_experiment_and_limits_it_was_placed_under():
    fake = FakeCcxt()
    ex = FakeEx(fake, RiskLimits(max_position_notional=150))
    ex.experiment = "lev10_risk3 + liq_tiers"
    t = Trader(ex, Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
    d = EntryDesk(t, regime_fn=favorable)
    await d.propose_group([prop(1.04)])
    ex.experiment, ex.settings = None, settings()               # the guardian: no research params, .env baseline
    fake.fill_limit(next(iter(fake.limits)))
    (ev,) = [e for e in await d.poll() if e["event"] == "filled"]
    tr = t.j.get_trade(ev["trade_id"])
    assert tr["experiment"] == "lev10_risk3 + liq_tiers"
    assert tr["entry_features"]["limits"]["max_position_notional"] == 150


# ------------------------------------------------------------ bounds re-checked on load
def test_an_override_outside_tightened_bounds_is_dropped_once_with_an_alert():
    j = Journal(":memory:")
    rp(j).set({"max_leverage": 10, "min_quote_volume_24h": 5e6}, "why", "lev10")
    p = rp(j, bounds={"max_leverage": (1, 5)})                  # the human tightened BOUND_MAX_LEVERAGE_MAX
    assert p.effective().max_leverage == RiskLimits().max_leverage   # back to .env, not clamped to 5
    assert p.effective().min_quote_volume_24h == 5e6 and p.experiment() == "lev10"
    assert [a["kind"] for a in j.alerts()] == ["params_dropped"] and "max_leverage=10" in j.alerts()[0]["message"]
    assert j.decisions()[0]["action"] == "params_dropped"
    p.effective()
    assert len(j.alerts()) == 1                                  # saved: not re-alerted on every read
    assert rp(j).effective().max_leverage == RiskLimits().max_leverage   # loosening again doesn't resurrect it


def test_overrides_inconsistent_with_a_new_baseline_are_all_dropped():
    j = Journal(":memory:")
    rp(j).set({"min_stop_distance_pct": 3, "max_leverage": 5}, "why", "x")
    p = rp(j, limits=RiskLimits(max_stop_distance_pct=2.5))    # human lowered the baseline max stop
    assert p.effective() == RiskLimits(max_stop_distance_pct=2.5) and p.experiment() is None
    assert "inconsistent" in j.alerts()[0]["message"]


def test_set_merges_onto_the_rechecked_overrides():
    j = Journal(":memory:")
    rp(j).set({"max_leverage": 10}, "why", "lev10")
    p = rp(j, bounds={"max_leverage": (1, 5)})
    p.set({"risk_per_trade_pct": 2}, "why", "risk2")
    assert p.effective().max_leverage == RiskLimits().max_leverage and p.experiment() == "risk2"


def test_a_drop_never_overwrites_a_concurrent_change():
    j = Journal(":memory:")
    rp(j).set({"max_leverage": 10}, "why", "lev10")
    p = rp(j, bounds={"max_leverage": (1, 5)})
    raw, ov, own = p._stored()
    j.set(KV_OVERRIDES, json.dumps({"max_leverage": 4}))         # another process wrote meanwhile
    assert not p._drop(raw, {}, {}, ["max_leverage=10 outside"])
    assert json.loads(j.get(KV_OVERRIDES)) == {"max_leverage": 4} and j.alerts() == []
