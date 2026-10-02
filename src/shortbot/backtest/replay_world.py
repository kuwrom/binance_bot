"""The replay's world (task #29): a read-only snapshot of the desk journal (ground truth: logged scans, risk-param
decisions), a writable copy the replayed pipeline reads its guardian feeds from and logs into, the risk limits in
force at any moment, and the network feeds blocked while replaying (no point-in-time history: CoinGecko trending,
Binance hype, news, listing announcements, the product-tag refresh).

The live journal is only ever opened read-only (sqlite backup from a mode=ro connection)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from ..config import RiskLimits
from .replay_store import ro_connect

WORLD_EMPTY = ("candidates", "predictions")          # the replay logs its own rows here


def snapshot(live_db: Path | str, dest: Path | str) -> Path:
    """Consistent copy of the (WAL, in-use) journal via the sqlite backup API from a read-only connection; the copy
    is switched to rollback journaling and made read-only on disk."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR)
        dest.unlink()
    src = ro_connect(live_db)
    dst = sqlite3.connect(str(dest))
    src.backup(dst)
    src.close()
    dst.execute("PRAGMA journal_mode=DELETE")
    dst.close()
    os.chmod(dest, stat.S_IRUSR | stat.S_IRGRP)
    return dest


def world(snap: Path | str, dest: Path | str) -> Path:
    """Writable copy of the snapshot for the replayed DataHub stores and Journal, minus the logged scans (the replay
    writes its own) and minus the guardian's market-map cache (the replay rebuilds maps as of each moment)."""
    from ..market.market_map import KV_LAST, KV_PREV_RANKS
    dest = Path(dest)
    if dest.exists():
        dest.unlink()
    shutil.copyfile(snap, dest)
    os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR)
    db = sqlite3.connect(str(dest))
    have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in WORLD_EMPTY:
        if t in have:
            db.execute(f"DELETE FROM {t}")
    if "kv" in have:
        db.executemany("DELETE FROM kv WHERE k=?", [(KV_LAST,), (KV_PREV_RANKS,)])
    db.commit()
    db.close()
    return dest


def limits_timeline(snap: Path | str) -> Callable[[int], RiskLimits]:
    """Risk limits in force at t (ms): the baseline (`before` of the first override), then every set_params change
    and reset_params, in order (decisions table). The live floor was 20M until 2026-09-29T18:15, then 5M."""
    db = ro_connect(snap)
    rows = db.execute("SELECT ts, action, snapshot FROM decisions WHERE action IN ('set_params', 'reset_params')"
                      " ORDER BY ts, id").fetchall() if _has(db, "decisions") else []
    db.close()
    names = {f.name for f in fields(RiskLimits)}
    pick = lambda d: {k: v for k, v in (d or {}).items() if k in names}   # noqa: E731
    events: list[tuple[int, dict[str, Any] | None]] = []
    baseline: dict[str, Any] | None = None
    for ts, action, snap_json in rows:
        s = json.loads(snap_json or "{}")
        if baseline is None and s.get("before"):
            baseline = pick(s["before"])
        events.append((_ms(ts), pick(s.get("changes")) if action == "set_params" else None))
    base = replace(RiskLimits(), **(baseline or {}))

    def at(t_ms: int) -> RiskLimits:
        cur: dict[str, Any] = {}
        for ts, ch in events:
            if ts > t_ms:
                break
            cur = {} if ch is None else {**cur, **ch}
        return replace(base, **cur)
    return at


def logged_scans(snap: Path | str, since_ms: int, until_ms: int) -> list[dict[str, Any]]:
    """Logged scans in [since, until): {"ts_ms", "ts", "regime", "rows"} with decoded JSON columns."""
    db = ro_connect(snap)
    db.row_factory = sqlite3.Row
    cur = db.execute("SELECT * FROM candidates WHERE ts_ms>=? AND ts_ms<? ORDER BY ts_ms, id", (since_ms, until_ms))
    scans: dict[int, dict[str, Any]] = {}
    for r in cur:
        row = dict(r)
        for k in ("setups", "vetoes", "signals", "features"):
            row[k] = json.loads(row[k]) if row.get(k) else ([] if k != "features" else {})
        s = scans.setdefault(row["ts_ms"], {"ts_ms": row["ts_ms"], "ts": row["ts"], "regime": row["regime"], "rows": []})
        s["rows"].append(row)
    db.close()
    return list(scans.values())


THRESHOLDS = (10.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0, 0.0)


def infer_args(rows: list[dict[str, Any]], depth: int = 25) -> dict[str, Any]:
    """scan_shorts arguments of a logged scan: logged since 2026-10-02 (features.scan), inferred before that.
    All gains -> gainers, all losses -> losers, both signs with <= depth//2+1 per side -> both, else now. min_change =
    the largest usual threshold at or below the smallest |24h move| logged."""
    logged = next((r["features"].get("scan") for r in rows if r["features"].get("scan")), None)
    if logged:
        return {**logged, "inferred": False}
    ch = [float(r["features"].get("change_24h") or 0) for r in rows]
    up, down = [c for c in ch if c >= 0], [c for c in ch if c < 0]
    side = depth // 2 + 1
    if not down:
        direction = "gainers"
    elif not up:
        direction = "losers"
    elif len(up) <= side and len(down) <= side:
        direction = "both"
    else:
        direction = "now"
    smallest = min((abs(c) for c in ch), default=10.0)
    lim = smallest * 2 if direction == "now" else smallest      # "now" ranks |4h| >= min_change / 2
    min_change = next(x for x in THRESHOLDS if x <= lim + 1e-9)
    return {"direction": direction, "min_change": min_change, "depth": depth, "top_n": 10, "groups": None,
            "inferred": True}


class Oracle:
    """Optional stand-ins for the blocked feeds (verification only): what the live scan logged for each coin."""

    def __init__(self) -> None:
        self.cg: dict[str, int] = {}
        self.hype: dict[str, dict[str, Any]] = {}

    def load(self, rows: list[dict[str, Any]]) -> None:
        self.cg, self.hype = {}, {}
        for r in rows:
            f, base = r["features"], r["symbol"].split("/")[0].removeprefix("1000")
            if f.get("cg_trending_rank") is not None:
                self.cg[base] = f["cg_trending_rank"]
            if f.get("binance_hype") is not None or f.get("hype_sentiment") is not None:
                self.hype[base] = {"hype": f.get("binance_hype"), "sentiment": f.get("hype_sentiment"),
                                   "wash_trading_tag": f.get("hype_wash_tag"), "summary": None}


@contextmanager
def offline_feeds(oracle: Oracle | None = None) -> Iterator[None]:
    """Block the live network feeds the pipeline would call while replaying (they would return TODAY's data);
    with an oracle, CoinGecko trending and Binance hype come from the logged scan instead."""
    from .. import news
    from ..market import sectors, social
    from .replay_exchange import ReplayUnavailable

    async def unavailable(*a: Any, **k: Any) -> Any:
        raise ReplayUnavailable("network feed: no point-in-time history")

    async def cg() -> dict[str, int]:
        return dict(oracle.cg) if oracle else await unavailable()

    async def hype() -> dict[str, dict[str, Any]]:
        return dict(oracle.hype) if oracle else await unavailable()

    patches = [(social, "coingecko_trending", cg), (social, "binance_hype", hype),
               (news, "news_for", unavailable), (news, "listing_announcements", unavailable),
               (sectors, "fetch_tags", unavailable)]
    saved = [(m, n, getattr(m, n)) for m, n, _ in patches]
    try:
        for m, n, f in patches:
            setattr(m, n, f)
        yield
    finally:
        for m, n, f in saved:
            setattr(m, n, f)


def _has(db: sqlite3.Connection, table: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def iso(ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def _ms(iso: str) -> int:
    from datetime import datetime, timezone
    d = datetime.fromisoformat(iso)
    return int((d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp() * 1000)
