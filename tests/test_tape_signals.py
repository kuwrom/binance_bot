import time

from shortbot.signals import evaluate
from shortbot.tape import classify
from tests.test_signals import FakeCtx


def b(o, h, l, c, v=100.0, buy=50.0):
    return [0, o, h, l, c, v, buy]


class TapeCtx(FakeCtx):
    def __init__(self, flow15=None, flow5=None, flow1h=None, spot=None, prem=None, liqs=(), flow1m=None,
                 mark1m=None, **kw):
        super().__init__(**kw)
        self._flow = {"15m": flow15 or [], "5m": flow5 or [], "1h": flow1h or [], "1m": flow1m or []}
        self._mark = {"1m": mark1m or []}
        self._spot = spot or {"raw": None, "mult": 1.0, "candles": []}
        self._prem = prem or []
        self._liqs = list(liqs)

    async def flow_candles(self, tf="5m", limit=288): return self._flow.get(tf, [])[-limit:]
    async def spot_flow(self, tf="1h", limit=24): return self._spot
    async def mark_candles(self, tf="5m", limit=96): return self._mark.get(tf, [])
    async def premium(self, tf="1h", limit=24): return self._prem
    async def liquidations(self, since_ms): return [x for x in self._liqs if x["ts_ms"] >= since_ms]


BASE15 = [b(1.0, 1.05, 0.98, 1.0) for _ in range(40)]
FAILED = BASE15 + [b(1.0, 1.02, 0.99, 1.01), b(1.01, 1.06, 1.0, 1.04), b(1.04, 1.045, 1.0, 1.02)]
ACCEPTED = BASE15 + [b(1.0, 1.02, 0.99, 1.01), b(1.01, 1.07, 1.0, 1.06), b(1.06, 1.08, 1.05, 1.07)]


async def test_failed_sweep_scores_and_short_liqs_confirm():
    now = int(time.time() * 1000)
    e = await evaluate(TapeCtx(flow15=FAILED, liqs=[{"ts_ms": now, "side": "short_liquidated", "notional": 90_000}]))
    assert e.features["sweep_state"] == "failed_sweep"
    assert any("failed sweep" in s for s in e.signals) and any("short liquidations" in s for s in e.signals)
    assert classify(e.features)["stance"] == "fade"


async def test_accepted_breakout_vetoes_and_stands_aside():
    e = await evaluate(TapeCtx(flow15=ACCEPTED))
    assert any("breakout accepted" in v for v in e.vetoes)
    assert classify(e.features)["stance"] == "stand_aside"


async def test_spot_led_rally_vetoed_perp_led_scored():
    perp = [b(1.0, 1.0, 1.0, 1.0)] * 6 + [b(1.0, 1.05, 1.0, 1.05)]
    spot_led = {"raw": "XUSDT", "mult": 1, "candles": [b(1.0, 1.0, 1.0, 1.0, 100, 70)] * 6 + [b(1.0, 1.06, 1.0, 1.06, 100, 70)]}
    e = await evaluate(TapeCtx(flow1h=perp, spot=spot_led, prem=[[0, 0, 0, 0, -0.0002]] * 7))
    assert any("spot-led rally" in v for v in e.vetoes)
    flat_spot = {"raw": "XUSDT", "mult": 1, "candles": [b(1.0, 1.0, 1.0, 1.0)] * 7}
    widening = [[0, 0, 0, 0, 0.0001]] * 6 + [[0, 0, 0, 0, 0.0020]]
    e2 = await evaluate(TapeCtx(flow1h=perp, spot=flat_spot, prem=widening))
    assert any("perp-led move" in s for s in e2.signals) and not any("spot-led" in v for v in e2.vetoes)


async def test_pump_in_progress_vetoed():
    base = [b(1, 1.01, 0.99, 1.0, 100) for _ in range(20)]
    e = await evaluate(TapeCtx(flow1h=base + [b(1.0, 1.2, 1.0, 1.15, 1000)]))
    assert any("pump in progress" in v for v in e.vetoes)
    assert classify(e.features)["state"] == "pump_in_progress"


async def test_long_liquidation_cascade_vetoed():
    now = int(time.time() * 1000)
    liqs = [{"ts_ms": now - 60_000, "side": "long_liquidated", "notional": 200_000}]
    e = await evaluate(TapeCtx(liqs=liqs))
    assert any("liquidation cascade" in v for v in e.vetoes)


async def test_missing_tape_data_never_vetoes():
    e = await evaluate(TapeCtx())
    assert not any(k in " ".join(e.vetoes) for k in ("sweep", "spot", "pump", "liquidation")) and e.tradable


async def test_painted_rejection_is_vetoed_not_scored():
    # 15m says "failed sweep", but 1m time/volume stayed ABOVE the level: the rejection is painted
    held_1m = [b(1.06, 1.07, 1.055, 1.065, 300)] * 50 + [b(1.06, 1.06, 1.03, 1.04, 5)] * 2
    e = await evaluate(TapeCtx(flow15=FAILED, flow1m=held_1m, mark1m=[[0, 1.06, 1.07, 1.05, 1.06, 0]]))
    assert e.features["sweep_state"] == "unconfirmed" and any("painted rejection" in v for v in e.vetoes)
    assert not any("failed sweep" in s for s in e.signals)


async def test_real_rejection_confirmed_by_time_and_volume():
    below_1m = [b(1.0, 1.01, 0.99, 1.0, 100)] * 55 + [b(1.0, 1.06, 1.0, 1.02, 100)] + [b(1.02, 1.03, 1.01, 1.02)] * 4
    e = await evaluate(TapeCtx(flow15=FAILED, flow1m=below_1m, mark1m=[[0, 1.0, 1.055, 1.0, 1.02, 0]]))
    assert e.features["sweep_rejected_robust"] is True and any("failed sweep" in s for s in e.signals)


async def test_volume_heat_veto_when_accelerating_and_points_when_cooling():
    calm = [b(1.0, 1.0, 1.0, 1.0, 10)] * 240
    surge = [b(1.0 + i * 0.002, 1.0, 1.0, 1.0 + (i + 1) * 0.002, 10 + i * 20) for i in range(48)]
    e = await evaluate(TapeCtx(flow5=calm + surge))
    assert any("volume still accelerating" in v for v in e.vetoes)
    fade = [b(1.0, 1.0, 1.0, 1.0, 10)] * 216 + [b(1.0, 1.0, 1.0, 1.0, 400)] * 24 + \
        [b(1.0, 1.0, 1.0, 0.99, 400 - i * 8) for i in range(48)]
    e2 = await evaluate(TapeCtx(flow5=fade, change=30))
    assert e2.features["vol_cooling"] and any("volume cooling" in s for s in e2.signals)
