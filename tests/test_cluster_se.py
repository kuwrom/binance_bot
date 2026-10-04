"""Cluster-robust inference on the pooled mean (#31): one-way CV3 and two-way Cameron-Gelbach-Miller (2011)."""

import math
import random
import statistics

import numpy as np
import pytest

from shortbot import cluster_se as C
from shortbot import research_stats as rs


def _panel(rng: random.Random, days=40, coins=60, sa=0.5, sb=0.3, se=1.0, fill=0.5):
    """y = day effect + coin effect + noise, each (day, coin) cell present with prob `fill`."""
    a = [rng.gauss(0, sa) for _ in range(days)]
    b = [rng.gauss(0, sb) for _ in range(coins)]
    y, d, c = [], [], []
    for i in range(days):
        for j in range(coins):
            if rng.random() < fill:
                y.append(a[i] + b[j] + rng.gauss(0, se))
                d.append(i)
                c.append(j)
    return y, d, c


def test_two_way_se_matches_theory_where_the_naive_se_is_far_too_small():
    rng = random.Random(7)
    days, coins, sa, sb, se, fill = 40, 60, 0.5, 0.3, 1.0, 0.5
    theory = sa ** 2 / days + sb ** 2 / coins + se ** 2 / (days * coins * fill)
    means, v2, viid, vday, cover, cover_iid = [], [], [], [], 0, 0
    for _ in range(250):
        y, d, c = _panel(rng, days, coins, sa, sb, se, fill)
        r = C.mean_inference(y, d, c)
        means.append(r["mean"])
        v2.append(r["se_2way"] ** 2)
        viid.append(r["se_iid"] ** 2)
        vday.append(r["se_day"] ** 2)
        cover += abs(r["mean"]) <= rs.t_ppf(0.975, r["df_2way"]) * r["se_2way"]
        cover_iid += abs(r["mean"]) <= 1.96 * r["se_iid"]
    avg = statistics.fmean(v2)
    assert abs(avg / theory - 1) < 0.10                       # the two-way variance matches the design's variance
    assert abs(avg / statistics.variance(means) - 1) < 0.25   # ... and the Monte Carlo variance of the mean
    assert statistics.fmean(viid) < theory / 5                # the naive SE is far too small
    assert statistics.fmean(vday) < 0.9 * theory              # day-only misses the coin component
    assert cover / 250 > 0.90 and cover_iid / 250 < 0.65


def _cgm_matrix(y, g, h):
    """Reference: CGM (2011) eq. 2.14-2.15 for a regression on a constant, with N x N selection matrices and the
    Stata-style factors c1 = G/(G-1), c2 = H/(H-1), c3 = GH/(GH-1) ((N-1)/(N-K) = 1 for K = 1)."""
    y = np.asarray(y, float)
    u = y - y.mean()
    n = len(y)
    g, h = np.asarray(g), np.asarray(h)
    sg = (g[:, None] == g[None, :]).astype(float)
    sh = (h[:, None] == h[None, :]).astype(float)
    sgh = sg * sh
    uu = np.outer(u, u)
    G, H = len(set(g.tolist())), len(set(h.tolist()))
    GH = len(set(zip(g.tolist(), h.tolist())))
    part = lambda s, k: k / (k - 1) * (uu * s).sum() / n ** 2  # noqa: E731
    return part(sg, G) + part(sh, H) - part(sgh, GH), part(sg, G), part(sh, H)


def test_two_way_components_equal_the_selection_matrix_formula():
    rng = random.Random(3)
    y, d, c = _panel(rng, days=9, coins=11, fill=0.6)
    v, vg, vh = _cgm_matrix(y, d, c)
    got = C.two_way_cr1(y, d, c)
    assert got["v"] == pytest.approx(v, rel=1e-10)
    assert got["v_g"] == pytest.approx(vg, rel=1e-10) and got["v_h"] == pytest.approx(vh, rel=1e-10)
    assert (got["g"], got["h"]) == (len(set(d)), len(set(c)))


def test_negative_two_way_variance_falls_back_to_the_larger_one_way():
    # each observation is its own day AND coin cell pattern where the intersection term dominates
    y = [1.0, -1.0, 1.0, -1.0, 0.5, -0.5]
    d = [0, 0, 1, 1, 2, 2]
    c = [0, 1, 0, 1, 0, 1]
    raw = C.two_way_cr1(y, d, c)
    r = C.mean_inference(y, d, c)
    one_way = max(r["se_day"], r["se_coin"], r["se_iid"])
    assert r["se_2way"] == pytest.approx(one_way)
    assert r["se_2way"] ** 2 >= raw["v"]
    assert r["fallback"] == (raw["v"] < max(r["se_day"], r["se_coin"], r["se_iid"]) ** 2)


def test_one_way_day_equals_research_stats_and_the_two_way_never_exceeds_either_one_way_t():
    rng = random.Random(11)
    y, d, c = _panel(rng, days=25, coins=30, sa=0.4, sb=0.4)
    y = [v + 0.2 for v in y]
    r = C.mean_inference(y, d, c)
    ref = rs.clustered_mean(y, d)
    assert r["se_day"] == pytest.approx(ref["se"]) and r["mean"] == pytest.approx(ref["mean"])
    assert r["df_day"] == 24 and r["df_2way"] == min(25, 30) - 1
    assert abs(r["t_2way"]) <= min(abs(r["t_day"]), abs(r["t_coin"])) + 1e-12
    assert 0 < r["n_eff"] <= r["n"]
    assert r["n_eff"] == pytest.approx(statistics.variance(y) / r["se_2way"] ** 2)


def test_t_is_of_the_pooled_mean_not_of_equal_weighted_cluster_means():
    # one busy day of losers, many quiet days of small winners: pooled mean < 0, equal-weighted day mean > 0
    y = [-1.0] * 30 + [0.2, 0.3] * 10
    d = [0] * 30 + [1 + k // 2 for k in range(20)]
    c = list(range(50))
    r = C.mean_inference(y, d, c)
    assert r["mean"] < 0 and r["t_day"] < 0 and r["t_2way"] < 0


def test_degenerate_inputs():
    assert C.mean_inference([], [], [])["t_2way"] is None
    one_day = C.mean_inference([1.0, 2.0, 3.0], [0, 0, 0], ["a", "b", "c"])
    assert one_day["t_day"] is None and one_day["t_2way"] is None and one_day["t_coin"] is not None
    flat = C.mean_inference([0.0] * 5, [0, 1, 2, 3, 4], list("abcde"))
    assert flat["t_2way"] is None and flat["n_eff"] == 5


def test_psr_with_effective_t_is_close_to_the_clustered_t():
    from shortbot.backtest import stats
    rng = random.Random(5)
    y, d, c = _panel(rng, days=50, coins=40, sa=0.3, sb=0.1)
    y = [v + 0.1 for v in y]
    r = C.mean_inference(y, d, c)
    sr = stats.sharpe(y)
    p = stats.psr(sr, r["n_eff"], 0.0, 3.0, 0.0)
    assert p == pytest.approx(statistics.NormalDist().cdf(r["t_2way"]), abs=0.02)
    assert math.isfinite(p)
