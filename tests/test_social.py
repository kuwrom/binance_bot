from shortbot.signals import evaluate
from shortbot.signals.social import hourly_increments
from tests.test_signals import FakeCtx

H = 3_600_000


def cumulative(increments, start=1000.0):
    out, total = [], start
    for i, x in enumerate(increments):
        total += x
        out.append((i * H, total))
    return out


class SocialCtx(FakeCtx):
    def __init__(self, posts_per_hour, c4h=None, c1h=None, social_extra=None, **kw):
        super().__init__(**kw)
        self._soc = {"square_posts_series": cumulative(posts_per_hour), **(social_extra or {})}
        self._c4h = c4h or [[0, 1.0, 1.0, 1.0, 1.0, 1]] * 18
        self._c1h = c1h

    async def social(self): return self._soc

    async def candles(self, tf="1h", limit=60):
        if tf == "4h":
            return self._c4h
        return self._c1h if self._c1h is not None else self._c


def test_hourly_increments():
    assert hourly_increments([(0, 10), (H, 15), (2 * H, 15), (3 * H, 30)]) == [0.0, 5.0, 0.0, 15.0]


async def test_hype_fade_scores_when_mentions_peaked_and_price_extended():
    posts = [2.0] * 30 + [40, 80, 60, 30, 15, 8, 5]
    low_4h = [[0, 1.0, 1.0, 0.8, 1.0, 1]] * 18          # 3-day low 0.8, price ~1.0+ => extended
    falling_1h = [[i, 1.3, 1.3, 1.2, 1.3 - i * 0.001, 1] for i in range(60)]
    e = await evaluate(SocialCtx(posts, c4h=low_4h, c1h=falling_1h, rate=0.0003))
    assert e.features.get("hype_fade") and any("hype fading" in s for s in e.signals)


async def test_rising_mentions_with_rising_price_vetoed():
    posts = [2.0] * 30 + [10, 30, 70, 150]
    rising_1h = [[i, 1.0, 1.0, 1.0, 1.0 + i * 0.01, 1] for i in range(60)]
    e = await evaluate(SocialCtx(posts, c1h=rising_1h))
    assert any("mentions still accelerating" in v for v in e.vetoes)


async def test_no_history_is_neutral():
    e = await evaluate(SocialCtx([]))
    assert not any("mention" in v for v in e.vetoes) and e.features["social_history_h"] == 0
