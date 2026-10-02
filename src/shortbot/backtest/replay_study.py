"""Stage 2 of the live-pipeline replay (task #73): the REAL scanner over a window at a fixed cadence, the desk's own
outcome labels and research report on what it logged, and the setups' R by entry delay.

    python -m shortbot.backtest.replay_study all --work $SCRATCH/replay-hist --journal data/shortbot.db \\
        --hist data/hist.db --daily data/daily.db --since 2026-08-03 --until 2026-09-28 --workers 6 --fetch --out DIR

- scan: every `--cadence` minutes, market map rebuilt (market_map.build_map) then scanner.scan_shorts with the
  desk's most common logged scan (both directions, min_change 5, depth 25) on a ReplayExchange pinned to that
  moment, universe = every coin perp listed then (prod), risk limits in force then. Workers split the window; each
  logs into its own copy of the journal snapshot and resumes where it stopped.
- label: research.label_outcomes (the guardian's labeller) with the clock at the window's end + 73 h.
- report: research.report on the merged replay journal, the delay table (replay_delays), trial register rows,
  provenance. Outputs go to --out, never data/."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .. import clock
from .. import provenance as P
from .. import research
from ..exchange import to_raw
from ..journal import Journal
from ..market.market_map import build_map
from ..scanner import scan_shorts
from . import replay_delays as RD
from . import replay_world as W
from .replay_exchange import ReplayExchange
from .replay_store import Store

SCAN = {"direction": "both", "min_change": 5.0, "depth": 25, "top_n": 10}   # 69 of 122 logged scans (09-27..10-01)
LABEL_AFTER_MS = 73 * 3_600_000
HOUR = 3_600_000


def times(since_ms: int, until_ms: int, cadence_min: int) -> list[int]:
    return list(range(since_ms, until_ms, cadence_min * 60_000))


def exchange(work: Path, world: Path) -> ReplayExchange:
    return ReplayExchange(Store(work / "cache.db"), W.limits_timeline(work / "snap.db"), db_path=world,
                          universe="prod")


async def run_scans(rex: ReplayExchange, journal: Journal, ts: list[int], scan: dict[str, Any] = SCAN,
                    progress: bool = True) -> int:
    """Replay each moment once; resumable (kv replay_done_ms)."""
    done = int(journal.get("replay_done_ms") or 0)
    n = 0
    with W.offline_feeds():
        for k, t in enumerate(ts):
            if t <= done:
                continue
            with clock.at(t):
                try:
                    await build_map(rex, journal)
                except Exception:  # noqa: BLE001 - the guardian keeps the previous map too
                    pass
                await scan_shorts(rex, min_change=scan["min_change"], top_n=scan["top_n"], depth=scan["depth"],
                                  journal=journal, regime=None, direction=scan["direction"])
            journal.set("replay_done_ms", str(t))
            n += 1
            if progress and k % 48 == 0:
                print(f"{W.iso(t)} {k + 1}/{len(ts)}", flush=True)
    return n


async def label_all(rex: ReplayExchange, journal: Journal, now_ms: int) -> int:
    n = 0
    with clock.at(now_ms):
        while True:
            k = await research.label_outcomes(rex, journal, now_ms, limit=5000)
            n += k
            if not k:
                return n


def candidates(db_path: Path) -> list[dict[str, Any]]:
    db = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    out = []
    for r in db.execute("SELECT * FROM candidates ORDER BY ts_ms, id"):
        d = dict(r)
        for k in ("setups", "vetoes", "signals", "features"):
            d[k] = json.loads(d[k]) if d.get(k) else ([] if k != "features" else {})
        out.append(d)
    db.close()
    return out


def merge(worlds: list[Path], dest: Path) -> Path:
    """One journal with every worker's candidates (research.report reads a Journal). The sqlite backup API, not a
    file copy: a worker's WAL may still hold rows that are not in the main file yet."""
    W._remove(dest)
    src = sqlite3.connect(str(worlds[0]))
    db = sqlite3.connect(str(dest))
    src.backup(db)
    src.close()
    cols = [r[1] for r in db.execute("PRAGMA table_info(candidates)") if r[1] != "id"]
    for i, w in enumerate(worlds[1:]):
        db.execute(f"ATTACH DATABASE ? AS w{i}", (str(w),))
        db.execute(f"INSERT INTO candidates ({', '.join(cols)}) SELECT {', '.join(cols)} FROM w{i}.candidates")
        db.commit()
        db.execute(f"DETACH DATABASE w{i}")
    db.close()
    return dest


def delays(store: Store, cands: list[dict[str, Any]]) -> dict[str, list[tuple[dict, dict]]]:
    out: dict[str, list[tuple[dict, dict]]] = {}
    for setup, eps in RD.setup_episodes(cands).items():
        for c in eps:
            raw, t = to_raw(c["symbol"]), c["ts_ms"]
            hold = RD.hold_hours(setup)
            bars = store.fine("perp5", raw, t - 25 * HOUR, t + int((hold + 2) * HOUR))
            fund = [[ts, r] for ts, r in store.funding(raw, t - HOUR, t + int((hold + 2) * HOUR))]
            out.setdefault(setup, []).append((c, RD.simulate_delays(c, setup, RD.rows_of(bars), fund)))
    return out


def report_md(tab: list[dict[str, Any]], rep: dict[str, Any], cfg: dict[str, Any], prov: dict[str, Any]) -> str:
    f = lambda x, n=2: "" if x is None else f"{x:.{n}f}"   # noqa: E731
    lines = ["# Live-pipeline replay over the hist window (task #73)", "",
             f"{cfg['since']} → {cfg['until']}, a scan every {cfg['cadence']} min ({cfg['scans']} scans, "
             f"{cfg['candidates']} candidate rows, {cfg['labelled']} labelled). Scan: {cfg['scan']}; universe: every "
             "coin perp listed at the moment; risk limits in force then. Plan: the scan's bracket (stop +1σ√24h, "
             "target −2σ√24h), the setup's time stop (≤ 72 h) and breakeven; one trade per setup-episode.", "",
             "## Setups' R by entry delay (after costs)", "",
             "| setup | delay | n | mean R | t (day) | t (2-way) | win | days | coins |", "|---|---|---|---|---|---|---|---|---|"]
    for r in tab:
        lines.append(f"| {r['setup']} | +{r['delay_min']} min | {r['n']} | {f(r['mean_r'], 3)} | {f(r['t_day'])} | "
                     f"{f(r['t_2way'])} | {f(r['win'])} | {r['days']} | {r['coins']} |")
    keys = ("n", "days", "avg_short_ret_24h", "avg_excess_ret_24h", "avg_net_ret_24h", "hit_rate_pct",
            "squeeze_rate_pct")
    lines += ["", "## Desk research report on the replayed scans (research.report, 24h short return %)", "",
              "| group | " + " | ".join(keys) + " |", "|---" * (len(keys) + 1) + "|"]
    groups = {"all": rep.get("all") or {}, "tradable": rep.get("tradable") or {}, "vetoed": rep.get("vetoed") or {},
              **{f"setup {k}": v for k, v in (rep.get("by_setup") or {}).items()}}
    for g, v in groups.items():
        if isinstance(v, dict):
            lines.append(f"| {g} | " + " | ".join("" if v.get(k) is None else str(v.get(k)) for k in keys) + " |")
    lines += ["",
              "Notes: rescans are deduplicated into symbol-episodes; t's are clustered by UTC day and coin "
              "(cluster_se, two-way floored at the larger one-way). One window, one regime.", "", P.footer(prov)]
    return "\n".join(lines) + "\n"


def _scan_worker(a: argparse.Namespace) -> int:
    work, (i, k) = Path(a.work), map(int, a.chunk.split("/"))
    ts = times(W._ms(a.since), W._ms(a.until), a.cadence)
    part = ts[len(ts) * i // k: len(ts) * (i + 1) // k]
    world = work / f"world_{i}.db"
    if not world.exists():
        W.world(work / "snap.db", world)
    j = Journal(world)
    rex = exchange(work, world)
    asyncio.run(run_scans(rex, j, part))
    end = W._ms(a.until) + LABEL_AFTER_MS
    print(f"chunk {i}: labelled {asyncio.run(label_all(rex, j, end))}", flush=True)
    j.db.close()                                   # checkpoints the WAL into the file
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="live-pipeline replay over a window (stage 2, #73)")
    ap.add_argument("cmd", choices=("all", "scan", "report"))
    ap.add_argument("--work", required=True)
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--cadence", type=int, default=30)
    ap.add_argument("--journal", help="the desk journal (read through a read-only snapshot)")
    ap.add_argument("--hist", help="data/hist.db (read-only)")
    ap.add_argument("--daily", help="data/daily.db (read-only)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--chunk", help="scan worker i/k (internal)")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--out")
    given = list(sys.argv[1:] if argv is None else argv)
    a = ap.parse_args(given)
    if a.since and "T" not in a.since:
        a.since += "T00:00:00+00:00"
    if a.until and "T" not in a.until:
        a.until += "T00:00:00+00:00"
    work = Path(a.work)
    if a.cmd == "scan":
        return _scan_worker(a)
    ts = times(W._ms(a.since), W._ms(a.until), a.cadence)
    if a.cmd == "all":
        if a.journal:
            W.snapshot(a.journal, work / "snap.db")
        if a.fetch:
            from .replay_window import build_window
            asyncio.run(build_window(work / "cache.db", ts[0], ts[-1] + 1, ts, a.hist, a.daily))
        procs = [subprocess.Popen([sys.executable, "-m", "shortbot.backtest.replay_study", "scan", "--work", str(work),
                                   "--since", a.since, "--until", a.until, "--cadence", str(a.cadence),
                                   "--chunk", f"{i}/{a.workers}"]) for i in range(a.workers)]
        if any(p.wait() for p in procs):
            raise SystemExit("a scan worker failed: rerun 'all' to resume")
    worlds = sorted(work.glob("world_*.db"))
    merged = merge(worlds, work / "merged.db")
    cands = candidates(merged)
    rep = {k: v for k, v in research.report(Journal(merged)).items() if k != "learnings"}
    store = Store(work / "cache.db")
    t0 = time.time()
    tab = RD.table(delays(store, cands))
    cfg = {"since": a.since, "until": a.until, "cadence": a.cadence, "scans": len(ts), "candidates": len(cands),
           "labelled": sum(1 for c in cands if c.get("short_ret_24h") is not None), "scan": SCAN,
           "delays_min": RD.DELAYS_MIN, "delay_seconds": round(time.time() - t0)}
    inputs = {"replay_cache": P.fingerprint(work / "cache.db", ["meta", "funding", "metrics"], file_hash=False),
              "journal_snapshot": P.fingerprint(work / "snap.db", ["decisions", "learnings"])}
    prov = P.stamp(P.config("shortbot.backtest.replay_study", given, **{k: v for k, v in cfg.items()
                                                                          if k not in ("delay_seconds",)}),
                   inputs, window={"t0": a.since, "t1": a.until, "scans": len(ts)})
    out = P.unique(Path(a.out)) if a.out else work / "out"
    P.save(out, prov, {"report.md": report_md(tab, rep, cfg, prov),
                       "results.json": json.dumps({"config": cfg, "delays": tab, "research_report": rep,
                                                   "provenance": prov}, indent=1, default=str)})
    from .. import trial_register
    n = trial_register.record("replay", RD.register_rows(tab, a.cadence, "both/5/25"), prov, out)
    print(f"{len(cands)} candidates; delay table {len(tab)} rows; trial register {n} rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
