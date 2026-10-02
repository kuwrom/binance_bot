"""#62: a failed label fetch never wipes a label. Legacy labels survive relabel failures (retried with backoff, kept
after LABEL_MAX_TRIES); never-labelled rows still end empty after the last try; the report shows the label mix."""

import sqlite3

import pytest

from shortbot.journal import Journal
from shortbot.journal_research import LABEL_MAX_TRIES
from shortbot.research import RETRY_H, label_outcomes, report

H = 3_600_000
DAY = 24 * H
T0 = 1_790_000_000_000 - 1_790_000_000_000 % DAY + 2 * H
NOW = T0 + 100 * H
LEGACY = {"A": 2.0, "B": -1.0, "C": 4.0, "D": 0.0, "E": 6.0}


class _Pub:
    """fetch_ohlcv per symbol: 'boom' raises like a delisted symbol / 429, 'short' returns 50 bars, else 864 bars
    closing at 0.9 (a 10% short win). `fail` maps symbol -> how many calls fail before it behaves."""
    def __init__(self, mode: dict[str, str], fail: dict[str, int] | None = None):
        self.mode, self.fail, self.calls = mode, dict(fail or {}), []

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls.append(symbol)
        if self.fail.get(symbol, 0) > 0:
            self.fail[symbol] -= 1
            raise RuntimeError("binanceusdm GET /fapi/v1/klines 429 Too Many Requests")
        m = self.mode.get(symbol, "ok")
        if m == "boom":
            raise RuntimeError("binance -1121 Invalid symbol (delisted)")
        n = 50 if m == "short" else limit
        return [[since + i * 300_000, 1.0, 1.01, 0.98, 0.9, 1.0] for i in range(n)]


class _Ex:
    def __init__(self, mode=None, fail=None):
        self.public = _Pub(mode or {}, fail)


def _legacy(symbols=LEGACY) -> Journal:
    """One scan, labelled by the legacy method (label_v NULL)."""
    j = Journal(":memory:")
    j.log_candidates("t", T0, [{"symbol": s, "price": 1.0, "score": 10, "setups": ["pump_fade"], "features": {}}
                               for s in symbols], "caution")
    for c in j.unlabelled_candidates(older_than_ms=10**15, limit=100):
        j.label_candidate(c["id"], {"short_ret_24h": symbols[c["symbol"]], "max_adverse_24h": 3.0})
    j.db.execute("UPDATE candidates SET label_v=NULL")
    j.db.commit()
    return j


def _row(j: Journal, sym: str) -> dict:
    return j._rows("SELECT * FROM candidates WHERE symbol=?", (sym,))[0]


async def _run_until_settled(ex, j, now=NOW, runs=48):
    """Hourly guardian runs until the queue is empty; returns the time of the last run."""
    for _ in range(runs):
        await label_outcomes(ex, j, now_ms=now)
        if not j.unlabelled_candidates(older_than_ms=10**15):
            return now
        now += H
    raise AssertionError("queue never settled")


async def test_relabel_failure_keeps_a_good_legacy_label():
    """Inverse of the review repro: every fetch fails, nothing is wiped, the rows stay queued with backoff."""
    j = _legacy()
    assert report(j)["all"]["n"] == 5
    assert await label_outcomes(_Ex({s: "boom" for s in LEGACY}), j, now_ms=NOW) == 0
    rows = j._rows("SELECT symbol, short_ret_24h, label_v, label_tries, label_error, label_retry_ms FROM candidates")
    assert {r["symbol"]: r["short_ret_24h"] for r in rows} == LEGACY
    assert all(r["label_v"] is None and r["label_tries"] == 1 and r["label_retry_ms"] == NOW + RETRY_H * H
               and "Invalid symbol" in r["label_error"] for r in rows)
    assert report(j)["all"]["n"] == 5 and report(j)["sample"]["legacy_labels"] == 5
    assert j.unlabelled_candidates(older_than_ms=10**15, now_ms=NOW + H) == []          # backing off
    assert len(j.unlabelled_candidates(older_than_ms=10**15, now_ms=NOW + RETRY_H * H)) == 5


@pytest.mark.parametrize("mode", ["boom", "short"])
async def test_delisted_symbol_keeps_its_legacy_label(mode):
    """'boom': the symbol is gone; 'short': bars stop before 24h. Either way A keeps its legacy outcome forever."""
    j = _legacy()
    ex = _Ex({"A": mode})
    await _run_until_settled(ex, j)
    a = _row(j, "A")
    assert a["short_ret_24h"] == 2.0 and a["max_adverse_24h"] == 3.0 and a["label_v"] is None
    assert a["label_tries"] == LABEL_MAX_TRIES and a["label_retry_ms"] is None and a["label_error"]
    assert ex.public.calls.count("A") == LABEL_MAX_TRIES
    others = j._rows("SELECT short_ret_24h, label_v FROM candidates WHERE symbol != 'A'")
    assert all(r["label_v"] == 2 and r["short_ret_24h"] == pytest.approx(10.0) for r in others)
    s = report(j)["sample"]
    assert (s["legacy_labels"], s["v2_labels"], s["legacy_kept"]) == (1, 4, 1) and "mixed" in s["label_note"]
    await label_outcomes(ex, j, now_ms=NOW + 100 * DAY)                                  # given up: never fetched again
    assert ex.public.calls.count("A") == LABEL_MAX_TRIES


async def test_backoff_doubles_between_tries():
    j = _legacy({"A": 2.0})
    ex = _Ex({"A": "boom"})
    now, seen = NOW, []
    while j.unlabelled_candidates(older_than_ms=10**15):
        await label_outcomes(ex, j, now_ms=now)
        if len(ex.public.calls) > len(seen):
            seen.append(now)
        now += H
    assert [(b - a) // H for a, b in zip(seen, seen[1:])] == [RETRY_H * 2 ** k for k in range(LABEL_MAX_TRIES - 1)]


async def test_transient_failure_is_retried_and_later_succeeds():
    j = _legacy({"A": 2.0, "B": -1.0})
    ex = _Ex(fail={"A": 2})
    assert await label_outcomes(ex, j, now_ms=NOW) == 1                                 # B relabelled, A failed once
    assert _row(j, "A")["short_ret_24h"] == 2.0 and _row(j, "A")["label_tries"] == 1
    assert await label_outcomes(ex, j, now_ms=NOW + H) == 0 and ex.public.calls.count("A") == 1   # backing off
    assert await label_outcomes(ex, j, now_ms=NOW + RETRY_H * H) == 0                    # second failure
    assert _row(j, "A")["label_tries"] == 2 and _row(j, "A")["label_v"] is None
    assert await label_outcomes(ex, j, now_ms=NOW + 3 * RETRY_H * H) == 1                # 2h + 4h later: works
    a = _row(j, "A")
    assert a["label_v"] == 2 and a["short_ret_24h"] == pytest.approx(10.0)
    assert (a["label_tries"], a["label_error"], a["label_retry_ms"]) == (None, None, None)
    s = report(j)["sample"]
    assert (s["legacy_labels"], s["v2_labels"], s["legacy_kept"]) == (0, 2, 0) and "label_note" not in s


async def test_unlabelled_row_that_keeps_failing_ends_empty_after_max_tries():
    j = Journal(":memory:")
    j.log_candidates("t", T0, [{"symbol": "Z", "price": 1.0, "features": {}}], None)
    ex = _Ex({"Z": "boom"})
    written = []
    now = NOW
    for _ in range(40):
        written.append(await label_outcomes(ex, j, now_ms=now))
        now += H
    z = _row(j, "Z")
    assert ex.public.calls.count("Z") == LABEL_MAX_TRIES and sum(written) == 1
    assert z["labelled_at"] and z["label_v"] == 2 and z["short_ret_24h"] is None and z["max_adverse_24h"] is None
    assert z["label_tries"] == LABEL_MAX_TRIES and "Invalid symbol" in z["label_error"]
    assert j.unlabelled_candidates(older_than_ms=10**15) == []


async def test_unlabelled_row_with_partial_bars_is_labelled_at_once():
    """Unchanged for never-labelled rows: bars that stop before 24h still give the 4h outcome straight away."""
    j = Journal(":memory:")
    j.log_candidates("t", T0, [{"symbol": "Y", "price": 1.0, "features": {}}], None)
    assert await label_outcomes(_Ex({"Y": "short"}), j, now_ms=NOW) == 1
    y = _row(j, "Y")
    assert y["label_v"] == 2 and y["short_ret_24h"] is None and y["short_ret_4h"] == pytest.approx(10.0)


def test_old_journal_gains_the_retry_columns(tmp_path):
    p = tmp_path / "old.db"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE candidates (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, ts_ms INTEGER NOT NULL, symbol TEXT"
               " NOT NULL, price REAL, short_ret_24h REAL, labelled_at TEXT, label_v INTEGER)")
    db.commit()
    db.close()
    cols = {r[1] for r in Journal(p).db.execute("PRAGMA table_info(candidates)")}
    assert {"label_tries", "label_error", "label_retry_ms"} <= cols
