"""Verify the live-pipeline replay against the scans the desk logged (task #29). A verification tool, not a
study: it tests the replay machinery, so it registers no trial.

Each logged scan is replayed through the REAL scanner (scanner.scan_shorts -> DataHub -> signals -> tape.classify
-> setups.detect_all) on a ReplayExchange pinned to the scan's moment, with the risk limits in force then and the
market map rebuilt (market_map.build_map, the guardian's code) at the moment the live desk's map was built. The
replayed candidates are compared with the logged ones (replay_compare).

    python -m shortbot.backtest.replay_verify --journal data/shortbot.db --work $SCRATCH/replay-cache \\
        --since 2026-09-27 --until 2026-10-02 --hist data/hist.db --daily data/daily.db --fetch --out DIR

Everything it writes goes under --work and --out (never data/); the journal is read through a read-only snapshot."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .. import clock
from .. import provenance as P
from ..journal import Journal
from ..market.market_map import build_map
from ..scanner import scan_shorts
from . import replay_compare as RC
from . import replay_world as W
from .replay_exchange import ReplayExchange
from .replay_store import Store

MAP_MAX_AGE_S = 1800      # MapCache.get default: signals ignore a map older than this


def live_maps(snap: Path | str) -> list[int]:
    db = sqlite3.connect(f"{Path(snap).resolve().as_uri()}?mode=ro", uri=True)
    try:
        return [r[0] for r in db.execute("SELECT ts FROM market_maps ORDER BY ts")]
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()


def map_for(maps: list[int], t_ms: int) -> int | None:
    """The live desk's latest map (s) a scan at t could see, if fresh."""
    prior = [m for m in maps if m * 1000 <= t_ms]
    return prior[-1] if prior and t_ms / 1000 - prior[-1] <= MAP_MAX_AGE_S else None


def _decode(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        d = dict(r)
        for k in ("setups", "vetoes", "signals", "features"):
            d[k] = json.loads(d[k]) if d.get(k) else ([] if k != "features" else {})
        out.append(d)
    return out


async def replay(scans: list[dict[str, Any]], rex: ReplayExchange, journal: Journal, maps: list[int],
                 oracle: W.Oracle | None = None, progress: bool = True) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    built: int | None = None
    with W.offline_feeds(oracle):
        for i, sc in enumerate(scans):
            t, args, t_start = sc["ts_ms"], W.infer_args(sc["rows"]), time.time()
            m, map_err = map_for(maps, t), None
            if m is not None and m != built:
                with clock.at(m * 1000):
                    try:
                        await build_map(rex, journal)
                    except Exception as e:  # noqa: BLE001 - live keeps the old map too
                        map_err = f"{type(e).__name__}: {e}"
                built = m
            if oracle is not None:
                oracle.load(sc["rows"])
            before = journal.db.execute("SELECT COALESCE(MAX(id), 0) FROM candidates").fetchone()[0]
            with clock.at(t):
                await scan_shorts(rex, min_change=args["min_change"], top_n=args.get("top_n") or 10,
                                  depth=args.get("depth") or 25, journal=journal, regime=sc["regime"],
                                  direction=args["direction"], groups=tuple(args["groups"]) if args.get("groups") else None)
            rows = _decode(journal.db.execute("SELECT * FROM candidates WHERE id > ? ORDER BY id", (before,)).fetchall())
            out.append({"ts_ms": t, "ts": sc["ts"], "args": args, "map_ts": m, "map_error": map_err,
                        "seconds": round(time.time() - t_start, 2), "cmp": RC.compare_scan(sc["rows"], rows)})
            if progress:
                u = out[-1]["cmp"]["universe"]
                print(f"[{i + 1}/{len(scans)}] {sc['ts']} {args['direction']}/{args['min_change']} "
                      f"universe {u['common']}/{u['logged']} (+{len(u['extra'])}) "
                      f"match {sum(r['match'] for r in out[-1]['cmp']['rows'])}/{u['logged']}", flush=True)
    return out


def _pct(x: dict[str, Any]) -> str:
    if not x or x.get("p") is None:
        return "n/a"
    ci = x.get("ci95")
    return f"{x['p']:.1%}" + (f" (95% CI {ci[0]:.1%}–{ci[1]:.1%}, {x['days']} days)" if ci else f" ({x['days']} day)")


def report(summary: dict[str, Any], results: list[dict[str, Any]], cfg: dict[str, Any], prov: dict[str, Any]) -> str:
    s = summary
    inferred = sum(1 for r in results if r["args"].get("inferred"))
    lines = [
        "# Live-pipeline replay vs logged scans (task #29)", "",
        f"Window {cfg['since']} → {cfg['until']}: {s['scans']} logged scans, {s['rows']} logged candidate rows. "
        f"Universe `{cfg['universe']}`, oracle feeds {'ON' if cfg['oracle'] else 'off'}, delisting lead "
        f"{cfg['lead_days']} d. Scan arguments inferred for {inferred} scans (logged since this change).", "",
        "| measure | value |", "|---|---|",
        f"| **row match** (same coin evaluated, same tradable, setups and tape state) | **{_pct(s['match'])}** |",
        f"| coin evaluated by the replay (universe recall per row) | {_pct(s['in_replay'])} |",
        f"| tradable agrees (rows in both) | {_pct(s['agree_tradable'])} |",
        f"| setups agree | {_pct(s['agree_setups'])} |",
        f"| tape state agrees (rows that logged one) | {_pct(s['agree_tape'])} |",
        f"| veto reasons agree (numbers ignored) | {_pct(s['agree_vetoes'])} |",
        f"| universe recall / precision (mean per scan) | {s['universe_recall_mean']} / {s['universe_precision_mean']} |",
        f"| scans with the exact logged universe | {s['universe_exact_share']} |",
        f"| tradable-set Jaccard (mean per scan) | {s['tradable_jaccard_mean']} |", "",
        "## Where they differ", "", "Veto reasons only in the logged scan: " +
        (", ".join(f"{k} ({n})" for k, n in s["vetoes_logged_only"]) or "none") + ".", "",
        "Veto reasons only in the replay: " + (", ".join(f"{k} ({n})" for k, n in s["vetoes_replay_only"]) or "none")
        + ".", "", "Tape state (logged -> replay): " +
        (", ".join(f"{k} ({n})" for k, n in s["tape_confusion"]) or "none") + ".", "",
        "## Features (rows in both)", "", "| feature | n | median rel. error / agreement | within 1% | within 10% |",
        "|---|---|---|---|---|"]
    for k, v in s["features"].items():
        if "agree" in v:
            lines.append(f"| {k} | {v['n']} | agree {v['agree']:.0%} | | |")
        else:
            lines.append(f"| {k} | {v['n']} | {v['median_rel_err']} | {v['within_1pct']:.0%} | {v['within_10pct']:.0%} |")
    lines += ["", "## Not replayable (no point-in-time history)", "",
              "- Order book (depth signal), news (hack), listing announcements, CoinGecko trending and Binance hype: "
              "blocked, so they show in data_missing (they never veto). `--oracle` feeds trending/hype from the log.",
              "- Product tags, supply facts (controller) and the demo account's coin list: today's snapshot.",
              "- Delisting announcement time: assumed `lead_days` before the delivery date.",
              "- Predicted funding: estimated from 1m premium-index klines (Binance samples every few seconds).",
              "- Liquidations, Square snapshots, unlocks and macro events: the guardian's recordings, as of each moment.",
              "", P.footer(prov)]
    return "\n".join(lines) + "\n"


def _ms(x: str) -> int:
    return W._ms(x if "T" in x else f"{x}T00:00:00+00:00")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--journal", required=True, help="the desk journal (read through a read-only snapshot)")
    ap.add_argument("--work", required=True, help="scratch dir: snapshot, world copy, cache")
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", help="replay cache (default <work>/cache.db)")
    ap.add_argument("--universe", choices=("demo", "prod"), default="demo")
    ap.add_argument("--lead-days", type=float, default=7.0)
    ap.add_argument("--oracle", action="store_true", help="logged CoinGecko/hype values instead of blocking them")
    ap.add_argument("--fetch", action="store_true", help="fill the cache first (network, public data)")
    ap.add_argument("--hist", help="data/hist.db (read-only)")
    ap.add_argument("--daily", help="data/daily.db (read-only)")
    ap.add_argument("--limit", type=int, help="replay only the first N scans")
    a = ap.parse_args(argv)
    work = Path(a.work)
    cache = Path(a.cache or work / "cache.db")
    snap = W.snapshot(a.journal, work / "snap.db")
    t0, t1 = _ms(a.since), _ms(a.until)
    scans = W.logged_scans(snap, t0, t1)[: a.limit]
    if a.fetch:
        from .replay_fetch import build
        asyncio.run(build(cache, scans[0]["ts_ms"], scans[-1]["ts_ms"], [s["ts_ms"] for s in scans], a.hist, a.daily,
                          a.universe))
    world = W.world(snap, work / "world.db")
    rex = ReplayExchange(Store(cache), W.limits_timeline(snap), db_path=world, universe=a.universe,
                         delist_lead_days=a.lead_days)
    results = asyncio.run(replay(scans, rex, Journal(world), live_maps(snap), W.Oracle() if a.oracle else None))
    summary = RC.summarize(results)
    cfg = {"since": a.since, "until": a.until, "universe": a.universe, "oracle": a.oracle, "lead_days": a.lead_days}
    inputs = {"journal_snapshot": P.fingerprint(snap, ["candidates", "decisions", "market_maps"]),
              "replay_cache": P.fingerprint(cache, ["bars", "metrics", "funding", "meta"], file_hash=False)}
    prov = P.stamp(P.config("shortbot.backtest.replay_verify", argv, **cfg), inputs,
                   window={"t0": a.since, "t1": a.until, "scans": len(scans)})
    out = P.save(P.unique(Path(a.out)), prov, {
        "report.md": report(summary, results, cfg, prov),
        "results.json": json.dumps({"summary": summary, "scans": results, "provenance": prov}, indent=1, default=str)})
    m = summary["match"]
    print(f"match {m['p']:.1%} ci {m['ci95']} over {summary['rows']} rows / {summary['scans']} scans -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
