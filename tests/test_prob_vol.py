"""σ inputs: gap-free returns, the sanity band, Yang-Zhang, the HAR blend and the hour-of-day profile (vol.py,
vol_har.py), and engine.sigma_hourly choosing the horizon-matched forecast with the legacy EWMA beside it."""

import math

import numpy as np
import pytest

from shortbot.probability import vol, vol_har

H = 3_600_000
T0 = 1_760_000_000_000 // H * H   # an exact UTC hour


def bars(n, sig_bar, step_ms=H, seed=1, start=T0, hour_scale=None, sub=20):
    """[ts, o, h, l, c, v] Gaussian bars with per-bar σ (optionally scaled by UTC hour); o = previous close."""
    rng, px, out = np.random.default_rng(seed), 100.0, []
    for i in range(n):
        ts = start + i * step_ms
        s = sig_bar * (hour_scale((ts // H) % 24) if hour_scale else 1.0)
        path = px * np.exp(np.cumsum(rng.normal(0, s / math.sqrt(sub), sub)))
        out.append([ts, px, max(px, path.max()), min(px, path.min()), float(path[-1]), 1.0])
        px = float(path[-1])
    return out


def test_returns_never_span_a_timestamp_gap():
    ts = [0, 300_000, 600_000, 3_600_000, 3_900_000]    # a 50-minute hole between bar 2 and bar 3
    closes = [100, 101, 100, 130, 131]                 # the hole hides a +30% move
    r = vol.log_returns(closes, ts)
    assert len(r) == 3 and max(abs(r)) < 0.02          # the jump across the gap is dropped
    assert len(vol.log_returns(closes)) == 4            # legacy call (no timestamps): unchanged
    assert vol.gaps(ts) == 1


def test_gap_free_ewma_is_not_inflated_by_a_hole():
    rng = np.random.default_rng(4)
    c = list(100 * np.exp(np.cumsum(rng.normal(0, 0.002, 400))))
    ts = [i * 300_000 for i in range(400)]
    c2, ts2 = c[:200] + [x * 1.2 for x in c[200:]], ts[:200] + [t + 6 * 3_600_000 for t in ts[200:]]
    clean = vol.ewma_sigma_hourly(c2, ts2)
    assert clean == pytest.approx(vol.ewma_sigma_hourly(c, ts), rel=0.15)
    assert vol.ewma_sigma_hourly(c2) > 2 * clean        # without timestamps the 20% gap jump dominates


def test_sigma_band():
    assert vol.clamp_sigma(1e-9) == vol.SIGMA_FLOOR_H and vol.clamp_sigma(3.0) == vol.SIGMA_CAP_H
    assert vol.clamp_sigma(0.01) == 0.01 and vol.clamp_sigma(None) is None and vol.clamp_sigma(float("nan")) is None


def test_yang_zhang_recovers_the_bar_variance():
    b = bars(600, 0.01, sub=400)   # a finely sampled path: a coarse one understates the high-low range
    assert math.sqrt(vol_har.yang_zhang_var(b)) == pytest.approx(0.01, rel=0.1)


def test_har_weights_follow_the_horizon():
    w1, w24, w168 = vol_har.har_weights(1), vol_har.har_weights(24), vol_har.har_weights(500)
    assert w1[0] > w24[0] > w168[0] and w1[2] < w24[2] < w168[2]
    assert all(sum(w) == pytest.approx(1.0) for w in (w1, w24, w168, vol_har.har_weights(6)))


def test_hour_profile_and_forward_window():
    loud = lambda h: 1.8 if 7 <= h <= 9 else 1.0   # noqa: E731 - 07-09 UTC louder, as in the research note
    prof = vol_har.hour_profile(bars(480, 0.01, hour_scale=loud))
    assert sum(prof) / 24 == pytest.approx(1.0)
    assert min(prof[7:10]) > 1.3 > max(prof[20:24])
    morning = vol_har.window_factor(prof, T0 // (24 * H) * 24 * H + 7 * H, 3)
    evening = vol_har.window_factor(prof, T0 // (24 * H) * 24 * H + 20 * H, 3)
    assert morning > 1.5 * evening
    assert vol_har.window_factor(prof, T0, 24) == pytest.approx(1.0, abs=1e-9)   # a whole day integrates to 1


def test_horizon_sigma_blends_components_and_uses_15m_for_short_windows():
    h1 = bars(499, 0.01) + [[T0 + 499 * H, 1, 1, 1, 1, 1]]   # the forming bar is dropped
    m15 = bars(97, 0.005, step_ms=H // 4, start=T0 + 475 * H)
    long = vol_har.horizon_sigma(m15, h1, 24, T0 + 500 * H)
    assert long["sampling"] == "1h" and set(long["parts"]) == {"d", "w", "m"}
    assert long["sigma_h"] == pytest.approx(0.01, rel=0.2)
    short = vol_har.horizon_sigma(m15, h1, 2, T0 + 500 * H)
    assert short["sampling"] == "15m" and short["parts"]["d"] == pytest.approx(0.01, rel=0.2)   # 0.005/15m = 0.01/h
    young = vol_har.horizon_sigma([], bars(30, 0.01), 24, T0 + 30 * H)
    assert set(young["parts"]) == {"d"} and young["weights"] == {"d": 1.0}
    assert vol_har.horizon_sigma([], bars(5, 0.01), 24, T0) is None


class Ctx:
    symbol = "X/USDT:USDT"

    def __init__(self, h1):
        self.h1 = h1

    async def flow_candles(self, tf, n):
        return bars(300, 0.004, step_ms=300_000)

    async def candles(self, tf, n):
        return {"1h": self.h1[-n:], "15m": bars(97, 0.005, step_ms=H // 4)}[tf]


async def test_engine_uses_the_horizon_forecast_keeps_ewma_and_applies_the_scale():
    from shortbot.probability.engine import sigma_context, sigma_hourly
    s = await sigma_hourly(Ctx(bars(500, 0.01)), 24.0, 0.9)
    assert s["model"] == "har" and s["ewma"] and s["har"]
    assert s["used"] == pytest.approx(s["har"] * 0.9)
    assert sigma_context(s) == {"sigma_ewma": s["ewma"], "sigma_har": s["har"], "sigma_model": "har",
                                "sigma_scale": 0.9}
    young = await sigma_hourly(Ctx(bars(8, 0.01)))
    assert young["model"] == "ewma" and young["used"] == pytest.approx(young["ewma"])
