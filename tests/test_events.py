from datetime import UTC, datetime, timedelta

import pytest

from shortbot.market.events import EventStore
from shortbot.setups import detect_all
from shortbot.signals import evaluate
from shortbot.tape import classify
from tests.test_signals import FakeCtx


def in_days(d):
    return (datetime.now(UTC) + timedelta(days=d)).isoformat()


def test_store_requires_source_and_valid_recipient(tmp_path):
    st = EventStore(tmp_path / "e.db")
    with pytest.raises(ValueError):
        st.add_unlock("ARB", in_days(10), 3.0, "team", "")
    with pytest.raises(ValueError):
        st.add_unlock("ARB", in_days(10), 3.0, "whales", "https://x")
    st.add_unlock("1000ARB", in_days(10), 3.0, "team", "https://docs.example/unlocks")
    up = st.upcoming("ARB")
    assert len(up) == 1 and 9.9 < up[0]["days_to_unlock"] < 10.1


class UnlockCtx(FakeCtx):
    def __init__(self, events, **kw):
        super().__init__(**kw)
        self._ev = events

    async def unlocks(self): return self._ev


async def test_team_unlock_in_window_scores_detects_and_classifies():
    ev = [{"days_to_unlock": 12.0, "pct_supply": 6.0, "recipient": "team", "source": "https://x"}]
    e = await evaluate(UnlockCtx(ev, change=2, rate=0.0001, candles=[[i, 1, 1, 1, 1, 1] for i in range(60)]))
    assert any("team unlock" in s for s in e.signals) and "unlock" in detect_all(e.features)
    assert classify(e.features)["state"] == "pre_unlock"


async def test_ecosystem_or_far_unlock_not_scored():
    far = [{"days_to_unlock": 45.0, "pct_supply": 6.0, "recipient": "team", "source": "x"}]
    eco = [{"days_to_unlock": 10.0, "pct_supply": 6.0, "recipient": "ecosystem", "source": "x"}]
    for ev in (far, eco):
        e = await evaluate(UnlockCtx(ev))
        assert not any("unlock" in s for s in e.signals)
