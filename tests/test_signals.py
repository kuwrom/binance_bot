"""Signal plug-ins against a duck-typed fake MarketContext (no network)."""

from types import SimpleNamespace

from shortbot.config import RiskLimits
from shortbot.signals import REGISTRY, evaluate


def pump_candles(n=60, base=1.0, pump=1.6, lower_high=True):
    """Flat, then a pump in the last hours; optionally a lower high in the last 4 bars."""
    c = [[i, base, base * 1.005, base * 0.995, base, 100] for i in range(n - 8)]
    px = base
    for i in range(4):
        px *= pump ** 0.25
        c.append([n - 8 + i, px / 1.02, px * 1.01, px / 1.03, px, 900])
    top = px
    for i in range(4):
        h = top * (0.97 if lower_high else 1.02)
        c.append([n - 4 + i, top * 0.98, h, top * 0.95, top * 0.97, 700])
    return c


class FakeCtx:
    def __init__(self, symbol="PUMP/USDT:USDT", change=60, qv=200e6, rate=0.0012, interval=8, oi_value=80e6,
                 oi_growth=1.45, taker=(1.2, 1.2, 1.2, 1.2, 1.2, 0.8), age=12, tags=(), on_acct=True, candles=None,
                 missing=()):
        self.symbol = symbol
        self.base = symbol.split("/")[0]
        self.ex = SimpleNamespace(limits=RiskLimits())
        self.missing = set(missing)
        self.features = {}
        self._c = candles if candles is not None else pump_candles()
        self._t = {"percentage": change, "quoteVolume": qv, "last": self._c[-1][4] if self._c else 1.0}
        self._f = {"rate": rate, "interval_h": interval}
        self._oi = [{"oi": 100.0, "oi_value": oi_value / oi_growth}, {"oi": 100 * oi_growth, "oi_value": oi_value}]
        self._tk = list(taker)
        self._info = {"listing_age_days": age, "delisting_scheduled": False, "tags": list(tags)}
        self._acct = on_acct

    async def ticker(self): return self._t
    async def last(self): return self._t["last"]
    async def candles(self, tf="1h", limit=60): return self._c
    async def funding(self): return self._f
    async def oi_history(self, period="1h", limit=25): return self._oi
    async def taker_ratio(self, period="1h", limit=6): return self._tk
    async def market_info(self): return self._info
    async def account_tradable(self): return self._acct


async def ev(**kw):
    return await evaluate(FakeCtx(**kw))


async def test_registry_has_core_signals():
    assert {"market", "funding", "open_interest", "stretch", "structure", "taker_flow", "data_completeness"} <= set(REGISTRY)


async def test_frothy_pump_scores_high_and_is_tradable():
    e = await ev()
    assert e.tradable and e.score >= 70, (e.score, e.signals, e.vetoes)
    f = e.features
    assert f["funding_8h"] == 0.0012 and f["lower_high"] and f["rsi_1h"] > 80 and f["vwap_z"] > 0.5


async def test_negative_funding_hourly_thin_alpha_major_unlisted_vetoes():
    neg = await ev(rate=-0.0005)                     # learning #22: a half-size flag, not a veto
    assert not any("negative funding" in v for v in neg.vetoes) and neg.features["funding_size_mult"] == 0.5
    one = await ev(interval=1)
    assert one.features["funding_flag"] == "1h_interval" and one.features["funding_size_mult"] == 0.5
    assert any("too thin" in v for v in (await ev(oi_value=5e6)).vetoes)            # below tier B
    b = await ev(oi_value=15e6)                                                         # tier B: half risk
    assert not any("too thin" in v for v in b.vetoes) and b.features["liq_tier"] == "B"
    assert b.features["liq_size_mult"] == 0.5
    assert any("Alpha" in v for v in (await ev(tags=("Alpha",))).vetoes)
    assert any("major coin" in v for v in (await ev(symbol="SOL/USDT:USDT")).vetoes)
    assert any("not listed" in v for v in (await ev(on_acct=False)).vetoes)


async def test_missing_critical_data_fails_closed():
    e = await ev(missing=("open_interest",))
    assert any("data unavailable" in v for v in e.vetoes)


async def test_funding_normalised_by_interval():
    e = await ev(rate=0.0003, interval=4)   # 0.03%/4h == 0.06%/8h -> elevated
    assert any("elevated" in s for s in e.signals)


async def test_losers_score_breakdown_momentum():
    e = await ev(change=-20)
    assert any("breakdown momentum" in s for s in e.signals)


async def test_quiet_coin_scores_low():
    flat = [[i, 1.0, 1.005, 0.995, 1.0, 100] for i in range(60)]
    e = await ev(change=11, rate=0.0001, oi_growth=1.0, taker=(1, 1, 1, 1, 1, 1), age=400, candles=flat)
    assert e.score < 10


async def test_broken_signal_is_isolated():
    class Broken(FakeCtx):
        async def funding(self):
            raise RuntimeError("feed down")
    e = await evaluate(Broken())
    assert "funding" in e.errors and e.score > 0
