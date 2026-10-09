"""σ-scale recalibration (recal.py), realized variance at labelling, QLIKE and the barrier calibration (evaluate.py),
and the research brackets' drawn geometry (scanner.py)."""

import json
import math
from types import SimpleNamespace

import numpy as np
import pytest

from shortbot.journal import Journal
from shortbot.probability import baseline as b
from shortbot.probability import evaluate, recal
from shortbot.probability.labels import realized_var_h

DAY = 86_400_000


def test_vectorised_three_way_matches_the_scalar_series():
    p0, up, lo = np.array([1.0, 100.0]), np.array([1.03, 104.0]), np.array([0.98, 90.0])
    sig, t = np.array([0.01, 0.02]), np.array([4.0, 24.0])
    pu, pd, pt = recal.three_way_vec(p0, up, lo, sig, t, terms=300)
    for i in range(2):
        ref = b.three_way(p0[i], up[i], lo[i], sig[i], t[i], -(sig[i] ** 2) / 2)
        assert (pu[i], pd[i], pt[i]) == pytest.approx((ref["up"], ref["down"], ref["time"]), abs=1e-9)


def brackets(true_scale, n=1500, seed=3):
    """Labelled short brackets whose outcomes are drawn from the baseline at σ x true_scale (the logged σ is 1x)."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        s, k, rw = rng.uniform(0.004, 0.02), rng.choice([0.5, 1.0, 1.5]), rng.choice([0.5, 1.0, 2.0])
        stop, tgt = math.exp(k * s * math.sqrt(24)), math.exp(-rw * k * s * math.sqrt(24))
        tw = b.three_way(1.0, stop, tgt, s * true_scale, 24.0, -((s * true_scale) ** 2) / 2, terms=120)
        out = rng.choice(["stop", "target", "time"], p=np.array([tw["up"], tw["down"], tw["time"]]) /
                         (tw["up"] + tw["down"] + tw["time"]))
        rows.append({"kind": "bracket", "outcome": str(out), "entry": 1.0, "stop": stop, "target": tgt, "hours": 24.0,
                     "sigma_h": s, "symbol": f"C{i % 20}", "ts_ms": i * (30 * DAY // n), "context": {}})
    return rows


def test_walk_forward_finds_and_applies_a_too_wide_sigma():
    res = recal.walk_forward(brackets(0.85))
    assert res["applied"] and res["scale"] == pytest.approx(0.85, abs=0.06)
    assert all(f["train"] > 0 for f in res["folds"]) and res["n_eff"] < recal.PLATT_MAX_NEFF
    assert "platt" in res["folds"][0]                       # small sample: Platt reported beside it, never applied


def test_a_right_sigma_stays_near_one_and_too_few_rows_are_never_applied():
    res = recal.walk_forward(brackets(1.0))
    assert res["fitted_scale"] == pytest.approx(1.0, abs=0.06)
    small = recal.walk_forward(brackets(0.85, n=100))
    assert not small["applied"] and small["scale"] == 1.0


def test_purged_training_never_sees_a_label_that_overlaps_the_test_window():
    rows = brackets(0.85, n=600)
    res = recal.walk_forward(rows)
    assert res["folds"]
    for f in res["folds"]:
        before = sum(1 for r in rows if r["ts_ms"] < f["test_start_ms"])
        purged = sum(1 for r in rows if r["ts_ms"] + 24 * 3_600_000 < f["test_start_ms"])
        assert f["train"] == purged < before


def test_scale_for_reads_only_an_applied_verdict_and_logged_sigma_is_unscaled_for_the_fit():
    j = Journal(":memory:")
    assert recal.scale_for(j) == 1.0 and recal.scale_for(None) == 1.0
    recal.store(j, {"applied": True, "scale": 0.875})
    assert recal.scale_for(j) == 0.875
    recal.store(j, {"applied": False, "scale": 0.8})
    assert recal.scale_for(j) == 1.0
    recal.store(j, {"applied": True, "scale": 0.4})          # outside the sanity band: ignored
    assert recal.scale_for(j) == 1.0
    row = {**brackets(1.0, n=1)[0], "sigma_h": 0.009, "context": {"sigma_scale": 0.9}}
    assert recal.usable([row])[0]["_sig"] == pytest.approx(0.01)


def candles_1m(hours, sig_min, gap_at=None, seed=2):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, sig_min, int(hours * 60))))
    rows = [[i * 60_000, x, x, x, x, 1] for i, x in enumerate(c)]
    if gap_at:   # 2h of candles missing, and a 20% jump hidden in the hole
        rows = rows[:gap_at] + [[r[0], *(v * 1.2 for v in r[1:5]), 1] for r in rows[gap_at + 120:]]
    return rows


def test_realized_variance_is_per_hour_horizon_sampled_and_gap_free():
    rv, k = realized_var_h(candles_1m(24, 0.001), 24)
    assert k == 60 and rv == pytest.approx(0.001**2 * 60, rel=0.25)
    rv_gap, _ = realized_var_h(candles_1m(24, 0.001, gap_at=600), 24)
    assert rv_gap == pytest.approx(rv, rel=0.3)               # the hidden jump is not counted
    assert realized_var_h(candles_1m(2, 0.001), 2)[1] == 15
    assert realized_var_h([], 24) == (None, None)


def test_qlike_scores_each_sigma_on_the_same_rows_and_barrier_odds_are_calibrated():
    rows = [{"kind": "bracket", "outcome": o, "rv_h": 1e-4, "sigma_h": 0.01, "ts_ms": i * DAY, "symbol": "X",
             "context": {"sigma_ewma": 0.012, "sigma_har": 0.0101, "p_stop": 0.3, "p_time": 0.6}}
            for i, o in enumerate(["stop", "time", "time", "target"] * 5)]
    q = evaluate.qlike(rows)
    assert q["paired"] and q["used"]["qlike"] == pytest.approx(0.0, abs=1e-9)
    assert q["har"]["qlike"] < q["ewma"]["qlike"] and q["ewma"]["median_rv_ratio"] < 1   # EWMA too wide
    cal = evaluate.barrier_calibration(rows)
    assert cal["stop"]["n"] == 20 and cal["time"]["reliability_diagram"][0]["observed"] == 0.5


async def test_labelling_stores_realized_variance(monkeypatch):
    from shortbot.probability import labels
    j = Journal(":memory:")
    pid = j.log_prediction("scan", "X/USDT:USDT", 100.0, 105.0, 90.0, 24.0, "bracket", 0.1, sigma_h=0.01)

    async def fetch(ex, s, since, hours):
        return [[since + r[0], *r[1:]] for r in candles_1m(24, 0.001)]
    monkeypatch.setattr(labels, "fetch_mark_1m", fetch)
    assert await labels.label_due(None, j, 10**15) == 1
    await j.flush()
    rv_h, rv_k = j.db.execute("SELECT rv_h, rv_k FROM predictions WHERE id=?", (pid,)).fetchone()
    assert rv_k == 60 and rv_h == pytest.approx(6e-5, rel=0.25)
    report = evaluate.report(j)
    assert report["sigma_qlike"]["n"] == 1 and report["sigma_scale"] is None


def test_research_brackets_draw_their_geometry_and_log_every_outcome_odds():
    from shortbot.scanner import K_STOPS, REWARDS, _log_research_brackets
    j = Journal(":memory:")
    recal.store(j, {"applied": True, "scale": 0.9})
    evals = [SimpleNamespace(symbol=f"C{i}/USDT:USDT", score=1, vetoes=[],
                             features={"last": 100.0, "sigma_h": 0.01, "sigma_h_ewma": 0.012, "sigma_h_har": 0.01,
                                       "sigma_model": "har"}) for i in range(40)]
    rows = [{"setups": [], "tape": {"state": "x"}}] * 40
    _log_research_brackets(j, evals, rows, "caution")
    preds = j.predictions(labelled_only=False)
    shapes = {(p["context"]["k_stop"], p["context"]["reward"]) for p in preds}
    assert len(preds) == 40 and len(shapes) > 3 and all(k in K_STOPS and r in REWARDS for k, r in shapes)
    p = preds[0]   # σ used = the EWMA (the model until the QLIKE gate passes) x the applied scale; HAR logged beside
    assert p["sigma_h"] == pytest.approx(0.0108) and p["context"]["sigma_scale"] == 0.9
    assert p["context"]["sigma_model"] == "ewma" and p["context"]["sigma_har"] == 0.01
    assert p["context"]["p_stop"] + p["context"]["p_time"] + p["p_model"] == pytest.approx(1.0, abs=2e-3)
    assert json.dumps(p["context"])   # serialisable as logged
    recal.store_model(j, {"model": "har"})
    _log_research_brackets(j, evals[:1], rows[:1], "caution")
    last = j.predictions(labelled_only=False)[0]
    assert last["context"]["sigma_model"] == "har" and last["sigma_h"] == pytest.approx(0.009)


def qrows(n, days, har_err, ewma_err, seed=5):
    """Labelled brackets whose realized variance is 1e-4/h; each σ forecast off by its own factor (1 = exact)."""
    return [{"kind": "bracket", "outcome": "time", "rv_h": 1e-4, "sigma_h": 0.01, "symbol": f"C{i % 30}",
             "ts_ms": int(i * days * DAY / n), "context": {"sigma_ewma": 0.01 * ewma_err, "sigma_har": 0.01 * har_err}}
            for i in range(n)]


def test_model_gate_needs_lower_har_qlike_on_500_rows_over_7_days():
    assert recal.model_verdict(qrows(600, 10, 1.02, 1.2))["model"] == "har"
    assert recal.model_verdict(qrows(400, 10, 1.02, 1.2))["model"] == "ewma"      # too few rows
    assert recal.model_verdict(qrows(600, 5, 1.02, 1.2))["model"] == "ewma"       # too few days
    worse = recal.model_verdict(qrows(600, 10, 1.3, 1.05))
    assert worse["model"] == "ewma" and worse["qlike_har"] > worse["qlike_ewma"]
    assert recal.model_verdict([])["model"] == "ewma"


def test_the_persisted_model_verdict_is_what_sigma_hourly_falls_back_on():
    j = Journal(":memory:")
    assert recal.policy(j) == (1.0, "ewma") and recal.active_model() == "ewma"
    recal.store_model(j, recal.model_verdict(qrows(600, 10, 1.02, 1.2)))
    assert recal.sigma_model(j) == "har" and recal.active_model() == "har"
    j.set(recal.KV_MODEL, "{broken")
    recal._mcache.clear()
    assert recal.sigma_model(j) == "ewma"


def test_a_journal_less_caller_reads_the_shared_verdict_not_this_process_history(tmp_path):
    # the level picker's engine has no journal: its model must not depend on whether this process read the verdict
    from shortbot.market.supply import KVReader
    j = Journal(tmp_path / "j.db")
    kv = KVReader(tmp_path / "j.db")
    assert recal.active_model(kv) == "ewma"
    recal.store_model(j, recal.model_verdict(qrows(600, 10, 1.02, 1.2)))
    recal._active["model"] = "ewma"   # a fresh process: nothing read yet
    assert recal.active_model(kv) == "har" and recal.active_model() == "ewma"
    assert recal.policy(None) == (1.0, None)   # so sigma_hourly resolves it through the hub's kv reader


def test_the_scale_fit_uses_only_rows_of_the_model_in_use():
    rows = brackets(0.85, n=400)
    for r in rows[:100]:
        r["context"] = {"sigma_model": "har"}
    assert len(recal.usable(rows)) == 300 and len(recal.usable(rows, "har")) == 100


async def test_guardian_recal_stores_the_model_and_the_scale():
    from shortbot.guardian_prob import ProbJobs
    g = ProbJobs()
    g.j = Journal(":memory:")
    await g.prob_recal()
    await g.j.flush()
    assert json.loads(g.j.get(recal.KV_MODEL))["model"] == "ewma"
    assert json.loads(g.j.get(recal.KV))["model"] == "ewma"
    assert evaluate.report(g.j)["labelled"] == 0
