"""The scan lane journals a tier-2 batch as one unit (#79): its research candidates, their brackets, the alert dedup
markers and the window's row in ONE transaction (it was 21-23 commits, each able to stall the loop), all or nothing,
and never on the loop while another process holds the write lock."""

import asyncio
import time

import pytest

from shortbot import scan_job as SJ
from shortbot import scan_tiered as ST
from shortbot import scanner
from shortbot.journal import Journal
from shortbot.notify import Notifier
from shortbot.scan_triggers import Trigger
from shortbot.signals.base import Evaluation
from tests.held_lock import HeldLock
from tests.test_guardian import SETTINGS
from tests.test_journal_writer import ticking
from tests.test_scan_tiered import Harness, ccxt, coins, row

N = 20


def batch(monkeypatch, path) -> Harness:
    """A close that deep-scores 20 coins through the real score_and_alert (only the signals are faked), every one
    alert-worthy and with a bracket (last, sigma_h), on a journal file."""
    real = SJ.score_and_alert
    h = Harness(monkeypatch, coins(N))
    monkeypatch.setattr(SJ, "score_and_alert", real)
    h.j = Journal(path)
    h.ts = ST.TieredScan(None, h.j, Notifier(SETTINGS, h.j), h.ts.streams_db, None)
    h.ts.now_ms = lambda: h.t
    h.ts._context, h.ts._regime = _none, _caution
    h.triggers = [Trigger(s, "move_1h", 1.0 + i, "d") for i, s in enumerate(coins(N))]

    async def evaluate(ex, tickers, groups=None):
        syms = [t["symbol"] for t in tickers]
        return ([Evaluation(s, {"last": 1.0, "sigma_h": 0.01}, 40, [], [], {}) for s in syms],
                [row(s) for s in syms])
    monkeypatch.setattr(scanner, "evaluate_tickers", evaluate)
    return h


async def _none(universe):
    return {}


async def _caution():
    return "caution"


def counts(j) -> tuple[int, ...]:
    q = ("SELECT (SELECT COUNT(*) FROM candidates), (SELECT COUNT(*) FROM predictions), "
         "(SELECT COUNT(*) FROM kv WHERE k LIKE ?), (SELECT COUNT(*) FROM scan_windows)")
    return tuple(j.db.execute(q, (SJ.KV_SEEN + "%",)).fetchone())


async def test_a_tier2_batch_and_its_window_are_one_transaction(tmp_path, monkeypatch):
    h = batch(monkeypatch, tmp_path / "j.db")
    sql = []
    h.j.db.set_trace_callback(sql.append)
    await h.ts.step()
    h.j.db.set_trace_callback(None)
    assert counts(h.j) == (N, N, N, 1)                            # 20 candidates, 20 brackets, 20 alerts, 1 window
    assert {ccxt(s) for s in coins(N)} == {c["symbol"] for c in h.j.candidates(limit=50)}
    units = transactions(sql)
    (window,) = [u for u in units if any(t in s for s in u for t in TABLES)]   # the alerts' own rows are posted apart
    assert sum(s.startswith("INSERT INTO candidates") for s in window) == N   # every row, in this one unit
    assert sum(s.startswith("INSERT INTO predictions") for s in window) == N
    assert sum(s.startswith("INSERT OR REPLACE INTO kv") for s in window) == N
    assert sum(s.startswith("INSERT INTO scan_windows") for s in window) == 1


TABLES = ("INTO candidates", "INTO predictions", "INTO scan_windows", f"'{SJ.KV_SEEN}")


def transactions(sql: list[str]) -> list[list[str]]:
    """The statements of each BEGIN ... COMMIT, in order."""
    out, cur = [], None
    for s in sql:
        if s.startswith("BEGIN"):
            cur = []
        elif s == "COMMIT" and cur is not None:
            out.append(cur)
            cur = None
        elif cur is not None:
            cur.append(s)
    return out


async def test_a_batch_that_fails_half_way_leaves_nothing(tmp_path, monkeypatch):
    h = batch(monkeypatch, tmp_path / "j.db")
    real, n = Journal.log_prediction, []

    def flaky(self, *a, **k):
        n.append(1)
        if len(n) == 7:
            raise RuntimeError("disk full")
        return real(self, *a, **k)
    monkeypatch.setattr(Journal, "log_prediction", flaky)
    with pytest.raises(RuntimeError, match="disk full"):
        await h.ts.step()
    assert counts(h.j) == (0, 0, 0, 0) and not h.j.db.in_transaction


async def test_a_window_waits_for_a_held_lock_off_the_loop(tmp_path, monkeypatch):
    h = batch(monkeypatch, tmp_path / "j.db")
    gaps, stop = [], asyncio.Event()
    with HeldLock(tmp_path / "j.db", 1.0) as held:
        tick = asyncio.create_task(ticking(gaps, stop))
        await h.ts.step()
        landed = time.time()
        stop.set()
        await tick
        released = held.wait()
    assert max(gaps) < 0.1 and landed >= released and counts(h.j) == (N, N, N, 1)
