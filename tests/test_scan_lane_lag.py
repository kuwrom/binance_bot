"""The scan lane never blocks the guardian's event loop (#78): every lane shares one loop, and the protective lane
(fills, stops) waits for whatever synchronous stretch another lane runs.

Each scan-lane job's FIRST pass runs through the guardian's own wrapper (guardian_jobs.py), in a fresh interpreter
(a first-pass import is the classic stall, invisible in-process once another test imported the module: mtf_live
pulls in pandas, ~0.2 s), on a realistic stream store: 180 coins streaming 1m-1d bars, tickers and marks, 25 of them
pumping so tier 2 deep-scores a full batch of 20. Only the exchange is faked: the tier-2 signals' REST inputs (their
compute runs between network awaits) and the regime check.

A ticker task on the loop measures every gap between its ticks. The loop thread's CPU time inside a gap is the
synchronous work that held every lane: above MAX_LAG_S, the job fails. Wall time is checked too, loosely: it also
counts a worker thread holding the GIL, but a busy machine stretches it by itself (~0.1 s seen with every core busy).

    python -m tests.test_scan_lane_lag <dir>      # the probe the test runs: prints {job: {cpu_ms, wall_ms, ...}}
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import random
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

MAX_LAG_S = 0.05     # synchronous work on the loop between two ticks (the loop thread's CPU time)
MAX_WALL_S = 0.25    # any gap, e.g. a worker holding the GIL; loose: a loaded machine stretches gaps by itself
COINS, PUMPS = 180, 25
BARS = {"1m": 1900, "5m": 160, "15m": 160, "1h": 160, "4h": 160, "1d": 160}   # what cadence_shadow and mtf_live read
ROOT = Path(__file__).resolve().parents[1]


def test_scan_lane_jobs_never_block_the_event_loop(tmp_path):
    import shortbot
    path = [str(Path(shortbot.__file__).resolve().parents[1]), str(ROOT), os.environ.get("PYTHONPATH", "")]
    env = {**os.environ, "SHORTBOT_DB": str(tmp_path / "shortbot.db"), "PYTHONPATH": os.pathsep.join(path)}
    p = subprocess.run([sys.executable, "-m", "tests.test_scan_lane_lag", str(tmp_path)], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr[-4000:]
    lag = json.loads(p.stdout.strip().splitlines()[-1])
    assert lag["tiered_scan"]["coins"] == COINS and lag["tiered_scan"]["scored"] == 20   # tiers 1-3 all ran
    assert lag["tiered_scan"]["candidates"] == 20
    assert lag["cadence_shadow"]["ran"] and lag["mtf_views"]["coins_logged"] == COINS
    assert {job: r["cpu_ms"] for job, r in lag.items() if r["cpu_ms"] > MAX_LAG_S * 1000} == {}, lag
    assert {job: r["wall_ms"] for job, r in lag.items() if r["wall_ms"] > MAX_WALL_S * 1000} == {}, lag


# ------------------------------------------------------------------ the probe (runs in the fresh interpreter)
def _store(path: Path, now: int) -> None:
    """A live-shaped stream store: every coin's 1m-1d klines up to the forming bar, ticker, mark, fresh rows."""
    from shortbot.market import streams as S
    st, rnd = S.StreamStore(path), random.Random(78)
    for i in range(COINS):
        raw, base = f"C{i:03d}USDT", 10 ** rnd.uniform(-3, 3)
        for tf, n in BARS.items():
            step = S.TF_MS[tf]
            first, px = now // step * step - (n - 1) * step, base
            for k in range(n):
                o = px
                px *= 1 + rnd.gauss(0, 0.004) + (0.06 if i < PUMPS and tf == "15m" and n - 6 <= k < n - 1 else 0)
                v = rnd.uniform(5e3, 2e4)
                st.kline({"s": raw, "i": tf, "t": first + k * step, "o": o, "h": max(o, px) * 1.002,
                          "l": min(o, px) * 0.998, "c": px, "v": v, "q": v * px, "Q": v * px * 0.5,
                          "V": v * 0.5, "x": k < n - 1}, now - 2000)
        st.latest("ticker", raw, {"pct": 30.0 if i < PUMPS else rnd.uniform(-5, 5), "last": px, "qv": 5e7,
                                  "h": px * 1.05, "l": px * 0.9}, now - 2000)
        st.latest("mark", raw, {"mark": px, "index": px, "premium": rnd.gauss(0, 3e-4),
                                "funding": rnd.gauss(5e-5, 1e-4), "next_funding": now + 3_600_000}, now - 2000)
        st.flush()


def _features(sym: str, last: float, rnd: random.Random) -> dict:
    """A deep score's features, live-sized (~3.5 KB of JSON per candidate row)."""
    f = {"symbol": sym, "last": last, "change_24h": 30.0, "quote_volume": 5e7, "funding_8h": 1e-4, "oi_value": 3e6,
         "oi_change_24h": 12.0, "rsi_1h": 78.0, "vwap_z": 2.1, "off_high_pct": 3.0, "listing_age_days": 400.0,
         "sigma_h": 0.03, "lower_high": True, "tags": ["AI"], "data_missing": [], "lifecycle_stage": "mature"}
    return {**f, **{f"x{k}": rnd.random() for k in range(130)}}


async def _probe(tmp: Path) -> dict:
    from shortbot import provenance, scanner
    from shortbot.exchange import NO_DELIVERY
    from shortbot.guardian_jobs import MarketJobs
    from shortbot.journal import Journal
    from shortbot.market.liquidations import LiquidationStore
    from shortbot.scan_tiered import TieredScan
    from shortbot.signals.base import Evaluation

    importlib.import_module("shortbot.guardian")   # what the guardian process has loaded when its lanes start
    provenance.data_dir = lambda: tmp   # the shadow jobs' research DBs (SHORTBOT_DB says the same)
    now = int(time.time() * 1000)
    db, streams_db = tmp / "shortbot.db", tmp / "streams.db"
    _store(streams_db, now)
    journal = Journal(db)
    LiquidationStore(db).record([(now - 60_000 * k, f"C{k:03d}/USDT:USDT", "long_liquidated", 1.0, 1.0, 5e4)
                                 for k in range(40)])
    raws = [f"C{i:03d}USDT" for i in range(COINS)]

    async def coin_perps():
        await asyncio.sleep(0.02)
        return {f"{r[:-4]}/USDT:USDT": {"info": {"onboardDate": str(now - 400 * 86_400_000),
                                                 "deliveryDate": str(NO_DELIVERY)}} for r in raws}

    async def funding_intervals():
        await asyncio.sleep(0.02)
        return {}

    async def evaluate_tickers(ex, tickers, groups=None):   # tier 2 without the exchange: the network awaits
        rnd = random.Random(7)
        await asyncio.sleep(0.05)
        evals = [Evaluation(t["symbol"], _features(t["symbol"], t["last"], rnd), 45, ["pump"], [], {})
                 for t in tickers]
        return evals, [scanner._row(ev) for ev in evals]

    class Quiet:
        async def send(self, *a, **k):
            return None

    async def regime():
        return "caution"
    scanner.evaluate_tickers = evaluate_tickers
    ex = SimpleNamespace(coin_perps=coin_perps, funding_intervals=funding_intervals)
    tiered = TieredScan(ex, journal, Quiet(), streams_db, db)
    tiered._regime = regime
    g = SimpleNamespace(tiered=tiered, app=SimpleNamespace(settings=SimpleNamespace(db_path=str(db))))
    out = {}
    for name in ("tiered_scan", "cadence_shadow", "mtf_views"):
        out[name] = await _max_gap(getattr(MarketJobs, name)(g))
    row = journal.db.execute("SELECT coins, scored FROM scan_windows").fetchone()
    out["tiered_scan"] |= {"coins": row[0], "scored": len(json.loads(row[1])),
                           "candidates": journal.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]}
    research = tmp / "research"
    with sqlite3.connect(research / "cadence_shadow.db") as c:   # the wrapper keeps run()'s counts to itself
        out["cadence_shadow"]["ran"] = bool(c.execute("SELECT value FROM meta WHERE key='last_minute'").fetchone())
    with sqlite3.connect(research / "mtf.db") as c:
        out["mtf_views"]["coins_logged"] = c.execute("SELECT COUNT(DISTINCT symbol) FROM views").fetchone()[0]
    return out


async def _max_gap(job) -> dict[str, float]:
    """Run a job while a ticker task on the loop measures each gap between its ticks: the wall time, and the loop
    thread's CPU time inside it (synchronous code running on the loop; scheduling and GIL waits use no CPU)."""
    worst = {"cpu_ms": 0.0, "wall_ms": 0.0}
    last = [time.perf_counter(), time.thread_time()]

    async def ticker():
        while True:
            await asyncio.sleep(0.001)
            t, c = time.perf_counter(), time.thread_time()
            worst["wall_ms"] = max(worst["wall_ms"], (t - last[0]) * 1000)
            worst["cpu_ms"] = max(worst["cpu_ms"], (c - last[1]) * 1000)
            last[:] = [t, c]
    tick = asyncio.create_task(ticker())
    await asyncio.sleep(0.01)
    worst.update(cpu_ms=0.0, wall_ms=0.0)
    await job
    await asyncio.sleep(0.01)   # the tick after a block records it
    tick.cancel()
    return {k: round(v, 1) for k, v in worst.items()}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_probe(Path(sys.argv[1])))))
