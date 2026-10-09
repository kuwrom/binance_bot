"""σ inputs: gap-free returns, the sanity band, Yang-Zhang, the HAR blend and the hour-of-day profile (vol.py,
vol_har.py), and engine.sigma_hourly choosing the horizon-matched forecast with the legacy EWMA beside it."""

import math
from types import SimpleNamespace

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
    def __init__(self, h1, symbol="X/USDT:USDT"):
        self.h1, self.symbol = h1, symbol

    async def flow_candles(self, tf, n):
        return bars(300, 0.004, step_ms=300_000)

    async def candles(self, tf, n):
        return {"1h": self.h1[-n:], "15m": bars(97, 0.005, step_ms=H // 4)}[tf]


async def test_used_stays_on_ewma_with_har_logged_beside_it_until_the_gate_says_har():
    from shortbot.probability import recal
    from shortbot.probability.engine import sigma_context, sigma_hourly
    s = await sigma_hourly(Ctx(bars(500, 0.01)), 24.0, 0.9, har=True)
    assert s["model"] == "ewma" and s["ewma"] and s["har"]
    assert s["used"] == pytest.approx(vol.clamp_sigma(s["ewma"] * 0.9))
    assert sigma_context(s) == {"sigma_ewma": s["ewma"], "sigma_har": s["har"], "sigma_model": "ewma",
                                "sigma_scale": 0.9}
    quiet = await sigma_hourly(Ctx(bars(500, 0.01)))          # the per-coin scan signal: no HAR, no 1h history
    assert quiet["har"] is None and quiet["used"] == pytest.approx(s["ewma"])
    gated = await sigma_hourly(Ctx(bars(500, 0.01)), 24.0, 1.0, model="har")
    assert gated["model"] == "har" and gated["used"] == pytest.approx(gated["har"])
    recal._active["model"] = "har"                             # a persisted verdict read by this process
    assert (await sigma_hourly(Ctx(bars(500, 0.01))))["model"] == "har"
    young = await sigma_hourly(Ctx(bars(8, 0.01), "NEW/USDT:USDT"), model="har")   # 8 hours of history
    assert young["model"] == "ewma" and young["used"] == pytest.approx(young["ewma"])   # no HAR: the legacy σ


class Public:
    """A cold store's REST side: every kline call counted per timeframe."""

    def __init__(self):
        self.calls = []

    async def fetch_ohlcv(self, symbol, tf="1h", since=None, limit=60, params=None):
        from shortbot import clock
        self.calls.append(tf)
        step = {"1h": H, "15m": H // 4}[tf]
        return [[r[0] - 600 * H + clock.now_ms() // step * step - T0, *r[1:]] for r in bars(limit, 0.01, step_ms=step)]


async def test_a_cold_store_costs_one_rest_kline_call_per_coin_per_bar(tmp_path):
    from shortbot import clock, weight
    from shortbot.config import RiskLimits
    from shortbot.market import DataHub
    from shortbot.probability.engine import sigma_hourly
    pub = Public()

    async def flow(symbol, tf, limit):
        return bars(limit, 0.004, step_ms=300_000)
    ex = SimpleNamespace(settings=SimpleNamespace(db_path=tmp_path / "shortbot.db"), public=pub, limits=RiskLimits(),
                         flow_klines=flow)
    t = T0 + 10 * 60_000
    with weight.tagged(weight.RESEARCH):
        with clock.at(t):
            for hours in (24.0, 2.0, 2.0):   # three callers, each its own context, within one 15m bar
                await sigma_hourly(DataHub(ex).context("X/USDT:USDT"), hours, har=True)
        assert pub.calls.count("1h") == 1 and pub.calls.count("15m") == 1
        with clock.at(t + 15 * 60_000):      # the next 15m bar: one new 15m read, the 1h history still current
            await sigma_hourly(DataHub(ex).context("X/USDT:USDT"), 2.0, har=True)
        assert pub.calls.count("1h") == 1 and pub.calls.count("15m") == 2
        with clock.at(t + H):                # the next 1h bar
            await sigma_hourly(DataHub(ex).context("X/USDT:USDT"), 24.0, har=True)
        assert pub.calls.count("1h") == 2
        assert all(k[1] == "1h" for k in vol_har._HIST)   # the expired 15m history is dropped, not kept for good


async def test_the_scanner_computes_har_only_for_coins_reaching_the_probability_stage():
    from shortbot.scanner import add_har
    from shortbot.signals import Evaluation
    asked = []

    class Hub:
        def context(self, symbol):
            asked.append(symbol)
            return Ctx(bars(500, 0.01), symbol)
    ev = lambda s, vetoes=(), **f: Evaluation(s, {"sigma_h": 0.01, **f}, 50, [], list(vetoes), {})  # noqa: E731
    evals = [ev("OK/USDT:USDT"), ev("VETO/USDT:USDT", ["squeeze"]), ev("LATE/USDT:USDT", data_deferred=True)]
    assert await add_har(Hub(), evals) == 1
    assert asked == ["OK/USDT:USDT"]
    assert evals[0].features["sigma_h_har"] == pytest.approx(0.01, rel=0.25)
    assert "sigma_h_har" not in evals[1].features and "sigma_h_har" not in evals[2].features
