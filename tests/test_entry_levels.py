"""The level picker (entry_levels.py, owner decision 2026-10-05): one full-size limit short at the highest candidate
level whose fill-adjusted touch odds clear the threshold, resting 2 ticks inside it. The pick is pure given an engine
(a fake one here: deterministic); propose_entries with `candidates` runs it on the fake exchange's market."""

import json
import math

import pytest

from shortbot import entry_levels as lv
from shortbot.probability.baseline import touch_up
from tests.fakes import SYM
from tests.test_entries import desk, prop

TICK = 0.0001


class Engine:
    """ProbabilityEngine.short_bracket's shape: a fixed touch probability per level (or the closed form at sigma)."""

    def __init__(self, touch=None, sigma=None):
        self.touch, self.sigma, self.calls = touch or {}, sigma, []

    async def short_bracket(self, symbol, stop, targets, hours, entry=None, fill_hours=None, mc=True, **kw):
        self.calls.append(entry)
        p = self.touch[entry] if entry in self.touch else touch_up(1.0, entry, self.sigma, fill_hours)
        return {"ok": True, "fill_prob": round(p, 4), "sigma_hourly": {"used": self.sigma or 0.02},
                "targets": [{"target": t, "p_target": 0.3, "mc_p_target": 0.28, "mc_ev_R": -0.02} for t in targets]}


async def run(touch=None, cands=(1.02, 1.04, 1.06), last=1.0, stop=1.10, sigma=None, **kw):
    return await lv.assess(Engine(touch, sigma), SYM, stop, [0.95, 0.90], list(cands), 6.0, 24.0, last, TICK, **kw)


async def test_the_highest_level_whose_fill_adjusted_odds_clear_the_threshold_is_picked():
    res = await run({1.02: 0.95, 1.04: 0.85, 1.06: 0.60})      # p_fill .8075, .7225, .51
    assert res["ok"] and res["chosen"]["price"] == 1.04 and res["threshold"] == 0.70
    assert [r["p_fill"] for r in res["candidates"]] == [0.51, 0.7225, 0.8075]   # highest level first
    assert res["chosen"]["order_price"] == pytest.approx(1.0398)               # 2 ticks inside, below the level
    assert res["chosen"]["rr"] == pytest.approx(round((1.0398 - 0.95) / (1.10 - 1.0398), 3))
    assert "clears 0.7" in res["note"] and "split_hint" not in res


async def test_the_haircut_is_applied_and_can_change_the_pick():
    res = await run({1.02: 0.95, 1.04: 0.85, 1.06: 0.60}, haircut=0.8)        # 1.04: .68 < .70 now
    assert all(r["p_fill"] == pytest.approx(r["p_touch"] * 0.8, abs=1e-4) for r in res["candidates"])
    assert res["chosen"]["price"] == 1.02 and res["haircut"] == 0.8


async def test_no_level_clears_the_threshold_falls_back_to_the_likeliest_fill_with_a_note():
    res = await run({1.02: 0.70, 1.04: 0.55, 1.06: 0.40})
    assert res["ok"] and res["chosen"]["price"] == 1.02 and "no level clears 0.7" in res["note"]


async def test_deterministic_with_a_fixed_volatility_and_the_closed_form():
    a, b = await run(sigma=0.024), await run(sigma=0.024)
    assert a["candidates"] == b["candidates"] and a["chosen"]["price"] == 1.02
    s = 0.024 * math.sqrt(6.0)                                                # touch_up at drift 0: 2 Φ(-u / σ√T)
    p = 1 + math.erf(-math.log(1.02) / s / math.sqrt(2))
    assert a["candidates"][-1]["p_touch"] == pytest.approx(p, abs=1e-4)


def test_inside_rests_two_ticks_below_the_level_on_the_grid_and_above_the_market():
    assert lv.inside(1.04, TICK, 1.0) == pytest.approx(1.0398)
    assert lv.inside(1.04005, TICK, 1.0) == pytest.approx(1.0398)     # an off-grid level: floored after 2 ticks
    assert lv.inside(0.00262, 0.00001, 0.0025) == pytest.approx(0.0026)   # BEAMX: 0.00260, not the touch 0.00262
    assert lv.inside(1.04, TICK, 1.03985) == pytest.approx(1.0399)    # two would reach the market: one tick
    assert lv.inside(1.04, TICK, 1.0399) is None                      # the level is already reached


async def test_unusable_candidates_are_listed_with_why_and_never_picked():
    res = await run({1.12: 0.99, 1.0001: 1.0, 1.04: 0.9}, cands=(1.12, 1.0001, 1.04))
    why = {r["price"]: r.get("why", "") for r in res["candidates"]}
    assert "stop" in why[1.12] and "market" in why[1.0001] and res["chosen"]["price"] == 1.04
    none = await run({1.12: 0.99}, cands=(1.12,))
    assert not none["ok"] and none["chosen"] is None and "no usable candidate" in none["note"]
    with pytest.raises(ValueError):
        await run(cands=(1.01, 1.02, 1.03, 1.04, 1.05))                # at most 4


async def test_a_split_is_hinted_only_when_two_levels_clear_and_the_far_one_is_a_sweep():
    eng = Engine({1.02: 0.95, 1.04: 0.9})
    cands = [{"price": 1.02, "level_type": "round_number"}, {"price": 1.04, "level_type": "sweep"}]
    res = await lv.assess(eng, SYM, 1.10, [0.95], cands, 6.0, 24.0, 1.0, TICK)
    assert res["chosen"]["price"] == 1.04 and "split" in res["split_hint"]
    cands[1]["level_type"] = "prior_high"
    assert "split_hint" not in await lv.assess(eng, SYM, 1.10, [0.95], cands, 6.0, 24.0, 1.0, TICK)


# ------------------------------------------------------------ propose_entries with candidates
LEVELS = [{"price": 1.04, "level_type": "prior_high", "level_reason": "4h swing high"},
          {"price": 1.02, "level_type": "round_number", "level_reason": "1.02 round"},
          {"price": 1.01, "level_type": "vwap_band", "level_reason": "VWAP +2σ"}]


def picked(**kw):
    p = prop(None, plan={"targets": [{"price": 0.95, "fraction": 0.5}], "time_stop_hours": 24}, candidates=LEVELS,
             **kw)
    del p["limit_price"]
    return p


async def test_propose_with_candidates_places_one_full_size_order_inside_the_picked_level_and_records_it():
    d, t, fake = desk()                     # price 1.0, flat ±2% 1h bars: σ ≈ 2.4%/h, TTL 6 h
    r = await d.propose_group([picked()], ttl_hours=6)
    assert r["ok"] and len(r["placed"]) == 1 and len(fake.limits) == 1
    (o,) = fake.limits.values()
    assert o["price"] == pytest.approx(1.0098)                   # 1.01 (p_fill ≈ .74) clears .70; 1.02 (≈ .63) not
    row = d._rows("1=1")[0]
    pick = json.loads(row["level_pick"])
    assert (pick["chosen"], pick["order_price"], pick["threshold"], pick["haircut"]) == (1.01, 1.0098, 0.7, 0.85)
    assert [c["price"] for c in pick["candidates"]] == [1.04, 1.02, 1.01]
    assert all(c["p_fill"] == pytest.approx(c["p_touch"] * 0.85, abs=1e-4) for c in pick["candidates"])
    assert (row["level_type"], row["level_reason"]) == ("vwap_band", "VWAP +2σ")
    assert r["placed"][0]["level_pick"]["chosen"] == 1.01
    preds = t.j._rows("SELECT * FROM predictions WHERE source='entry_level' ORDER BY id")
    assert [p["target"] for p in preds] == [1.04, 1.02, 1.01] and {p["ref_id"] for p in preds} == {row["id"]}
    assert [json.loads(p["context"])["chosen"] for p in preds] == [False, False, True]
    assert all(p["kind"] == "touch" and p["entry"] == 1.0 for p in preds)


async def test_a_fill_carries_the_pick_onto_its_trade():
    d, t, fake = desk()
    await d.propose_group([picked()])
    fake.fill_limit(next(iter(fake.limits)))
    (ev,) = await d.poll()
    assert t.j.get_trade(ev["trade_id"])["entry_features"]["level_pick"]["chosen"] == 1.01


async def test_a_threshold_from_the_proposal_changes_the_pick():
    d, t, fake = desk()
    await d.propose_group([picked(fill_threshold=0.6)])
    assert next(iter(fake.limits.values()))["price"] == pytest.approx(1.0198)    # 1.02 clears .60


async def test_candidates_refused_beside_a_limit_price_or_without_targets_and_explicit_limits_unchanged():
    d, t, fake = desk()
    both = {**picked(), "limit_price": 1.04}
    bare = {**picked(), "plan": {"time_stop_hours": 24}}
    r = await d.propose_group([both, bare])
    assert not r["ok"] and fake.limits == {}
    assert "replace limit_price" in r["rejected"][0]["error"] and "needs plan targets" in r["rejected"][1]["error"]
    r = await d.propose_group([prop(1.04)])
    assert r["ok"] and next(iter(fake.limits.values()))["price"] == 1.04 and "level_pick" not in r["placed"][0]
    assert d._rows("1=1")[0]["level_pick"] is None
    assert not t.j._rows("SELECT id FROM predictions WHERE source='entry_level'")


def test_the_levels_command_parses_comma_lists():
    from shortbot import commands
    commands.load()
    cmd = commands.by_cli("levels")
    ns = commands.build_parser().parse_args(["levels", "ZRO", "--stop", "1.1", "--targets", "0.95,0.9",
                                              "--candidates", "1.02, 1.04,1.06", "--hours", "4"])
    kw = cmd.cli_kwargs(ns)
    assert kw["targets"] == [0.95, 0.9] and kw["candidates"] == [1.02, 1.04, 1.06] and kw["hours"] == 4.0
    assert not cmd.write and cmd.name == "entry_levels"
