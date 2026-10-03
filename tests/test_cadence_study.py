"""cadence_study's rules on synthetic minute series: each fires where it should, episodes dedupe within 60 minutes,
and the lead / transient comparison counts what it claims."""

import numpy as np

from shortbot.backtest import cadence_study as C

N = 3 * 1440


def walk(seed=1, vol=0.0005):
    rnd = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(rnd.normal(0, vol, N)))


def test_move_z_fires_on_a_jump_and_not_on_noise():
    c = walk()
    quiet = np.nanmax(np.abs(C.move_z(c, 4)))
    c2 = c.copy()
    c2[2000:] *= 1.05                                       # a 5% jump at minute 2000
    z = C.move_z(c2, 4)
    jump = np.nanmax(np.abs(z[2000:2060]))
    assert jump >= 10 and quiet < jump / 3


def test_volume_z_flags_a_surge_hour():
    qv = np.full(N, 100.0) + np.random.default_rng(2).normal(0, 5, N)
    qv[3000:3060] *= 6
    z = C.volume_z(qv)
    assert z[3059] > 10 * np.nanmax(np.abs(z[2500:2990]))     # a 6x hour stands far above hour-to-hour noise


def test_episodes_dedupe_and_cadence_alignment():
    m = np.arange(1000)
    f = np.zeros(1000, dtype=bool)
    f[[100, 101, 130, 250, 251]] = True
    assert C.episodes(f, m, 1).tolist() == [100, 250]           # 130 is within 60 min of 100
    f2 = np.zeros(1000, dtype=bool)
    f2[100:140] = True
    assert C.episodes(f2, m, 15).tolist() == [105]              # the first minute on the 15-minute grid


def test_compare_measures_leads_and_transients():
    eps = {15: np.array([105, 600]), 5: np.array([100, 595, 900]), 1: np.array([101, 596, 901])}
    out = C.compare(eps)
    assert out[1]["leads"] == [4, 4] and out[5]["leads"] == [5, 5]
    assert out[1]["transients"] == 1 and out[5]["transients"] == 1      # 900 / 901 never reach a 15m episode
