import json
import time
from types import SimpleNamespace

from shortbot.journal import Journal
from shortbot.laggard import SCHEMA, accepted_breakdown, detect, label, outcome, pick_laggards
from shortbot.market.market_map import KV_LAST


def bar(c, lo=None, hi=None):
    return [0, c, hi or c * 1.002, lo or c * 0.998, c, 1.0]


def test_accepted_breakdown_needs_two_closes_below_4h_low():
    base = [bar(100)] * 17
    assert accepted_breakdown(base + [bar(99), bar(98.5), bar(98.4)])[0]        # 2 closed below 99.8, +1 open
    assert not accepted_breakdown(base + [bar(99), bar(100), bar(98)])[0]       # only one closed below


def test_pick_laggards_and_outcome():
    mem = [{"symbol": "L", "r4h": -6}, {"symbol": "A", "r4h": -1}, {"symbol": "B", "r4h": -5}, {"symbol": "C", "r4h": 0.5}]
    lag, rest = pick_laggards(mem, "L", -6)
    assert {m["symbol"] for m in lag} == {"A", "C"} and [m["symbol"] for m in rest] == ["B"]
    c = [bar(100 - i * 0.1) for i in range(48)]
    o = outcome(100, c, gap_pct=3)
    assert o["r1h"] > 0 and o["catchup_hit"] and o["catchup_minutes"] > 0


class Public:
    """Records, at every exchange await, whether the journal holds an open write transaction (it would block the
    other processes' writes for up to the 15 s busy timeout)."""

    def __init__(self, j, c15):
        self.j, self.c15, self.in_txn = j, c15, []

    async def fetch_ohlcv(self, sym, tf="15m", since=None, limit=None):
        self.in_txn.append(self.j.db.in_transaction)
        return self.c15

    async def fetch_tickers(self):
        return {f"{s}/USDT:USDT": {"quoteVolume": 50e6, "last": 1.0} for s in "ABCDEFGH"}


async def test_label_holds_no_write_transaction_across_an_exchange_call():
    j = Journal(":memory:")
    j.db.execute(SCHEMA)
    for lag in ("A", "B", "C"):
        j.db.execute("INSERT INTO laggard_events (ts, ts_ms, leader, leader_ret4h, laggard, laggard_ret4h, laggard_px,"
                     " control, control_px) VALUES ('2026-10-01', 1, 'L', -6, ?, -1, 100, 'X', 100)", (lag,))
    j.db.commit()
    ex = SimpleNamespace(public=Public(j, [bar(100 - i * 0.1) for i in range(50)]))
    assert await label(ex, j) == 3
    assert len(ex.public.in_txn) == 6 and not any(ex.public.in_txn)
    assert j.db.execute("SELECT COUNT(*) FROM laggard_events WHERE outcome IS NOT NULL").fetchone()[0] == 3


async def test_detect_holds_no_write_transaction_across_an_exchange_call():
    j = Journal(":memory:")
    coins = {f"{s}/USDT:USDT": {"sector": "s1" if s in "ABCD" else "s2", "r4h": -6 if s in "AE" else -1}
             for s in "ABCDEFGH"}
    j.set(KV_LAST, json.dumps({"ts": time.time(), "coins": coins, "header": {"breadth_pct": 40}}))
    ex = SimpleNamespace(public=Public(j, [bar(100)] * 17 + [bar(99), bar(98.5), bar(98.4)]))
    assert await detect(ex, j) == 6                              # 3 laggards in each of the two sectors
    assert len(ex.public.in_txn) == 2 and not any(ex.public.in_txn)
