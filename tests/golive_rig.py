"""A journal that passes every go-live criterion (#46 §4, golive.py), with a knob for each one so a test can break
exactly one. Everything is stamped relative to NOW, so ages and windows are deterministic."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from shortbot import drill_log, gateway_health, probes, uptime
from shortbot import killswitch as ks
from shortbot import registry as R
from shortbot.backup_drill import KV_KEY as BACKUP_KV
from shortbot.journal import Journal

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)
SINCE = "2026-10-03T00:00:00+00:00"
DAY0 = datetime(2026, 10, 4, 10, tzinfo=UTC)
CYCLE = (1.4, -0.6, 0.9, -0.4, 1.2, -0.8, 0.7, 0.5, -0.3, 1.0)     # mean +0.36 R
FORWARD = (0.5, 0.3, 0.8, 0.2, 0.6)
N, COINS, VOL = 60, 25, 5e7
STOP, ENTRY, AMOUNT = 1.05, 1.0, 100.0        # 1R = 5 USDT, notional 100


def iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def fill(leg: str, kind: str, bps: float, mid: float, at: datetime) -> dict[str, Any]:
    """A fill `bps` worse for the short than the mainnet mid (negative: better)."""
    px = mid * (1 - bps / 1e4) if leg == "entry" else mid * (1 + bps / 1e4)
    return {"leg": leg, "type": kind, "order": f"{leg}-{kind}-{ms(at)}", "px": px, "qty": AMOUNT, "ms": ms(at),
            "src": "book", "ref_ms": ms(at) + 100, "bid": mid * 0.99995, "ask": mid * 1.00005, "mark": mid}


def trade(j: Journal, r: float, opened: datetime, closed: datetime, symbol: str = "C0/USDT:USDT",
          setup: str = "pump_fade", entry: tuple[str, float] = ("market", 3.0), exit_bps: float = 3.0,
          fees: float = -0.08, refs: bool = True, limits: dict[str, Any] | None = None) -> int:
    feats = {"quote_volume": VOL, **({"entry_type": "limit"} if entry[0] == "limit" else {}),
             **({"limits": limits} if limits else {})}
    tid = j.create_trade(symbol, setup, "t", STOP, 2, {}, feats)
    j.activate_trade(tid, ENTRY, AMOUNT, ENTRY * AMOUNT)
    risk = (STOP - ENTRY) * AMOUNT
    j.close_trade(tid, ENTRY - r * risk / AMOUNT, r * risk, "x", fees=fees)
    j.update_trade(tid, opened_at=iso(opened), closed_at=iso(closed))
    if refs:
        doc = {"v": 1, "vol24_usd": VOL, "fills": [fill("entry", entry[0], entry[1], ENTRY, opened),
                                                   fill("exit", "market", exit_bps, ENTRY - 0.01, closed)]}
        j.db.execute("UPDATE trades SET fill_refs=? WHERE id=?", (json.dumps(doc), tid))
        j.db.commit()
    return tid


def decision(j: Journal, action: str, at: datetime, snapshot: dict[str, Any], reasoning: str = "r") -> None:
    j.db.execute("INSERT INTO decisions (ts, action, reasoning, snapshot, mode) VALUES (?,?,?,?,?)",
                 (iso(at), action, reasoning, json.dumps(snapshot), j.mode))
    j.db.commit()


def alert(j: Journal, kind: str, at: datetime, symbol: str | None = None) -> None:
    j.db.execute("INSERT INTO alerts (ts, symbol, kind, message, mode) VALUES (?,?,?,?,?)",
                 (iso(at), symbol, kind, "m", j.mode))
    j.db.commit()


def gap(j: Journal, start: datetime, secs: float, tag: str | None = None) -> None:
    j.db.execute("INSERT INTO guardian_gaps (start_at, end_at, secs, mode, pid, tag) VALUES (?,?,?,?,?,?)",
                 (iso(start), iso(start + timedelta(seconds=secs)), secs, j.mode, 1, tag))
    j.db.commit()


def plumbing(j: Journal, age_days: float = 2.0, skip: tuple[str, ...] = ()) -> None:
    at = NOW - timedelta(days=age_days)
    for name in drill_log.NAMES:
        if name not in skip:
            decision(j, drill_log.RESULT, at, {"drill": name, "verdict": "pass", "ok": True, "mode": "demo",
                                               "checks": {"x": {"ok": True, "detail": None}}})
    for name in probes.ORDER:
        if name not in skip:
            decision(j, "probe_result", at, {
                "probe": name, "ok": True, "clean": True, "verdict": "as assumed",
                "matches_fake": None if name in ("user-stream",) else True})
    j.set(BACKUP_KV, json.dumps({"ts": iso(at), "copy": "c.db.gz", "decision_id": 1}))
    j.set(j.mode_key(gateway_health.KEY), json.dumps({"at": NOW.timestamp() - 5, "pid": 7, "code": "abc"}))


def forward(j: Journal, adj: tuple[float, ...] = FORWARD, monkeypatch: Any = None, pid: str = "strategy.pump_fade",
            n: int = 30) -> None:
    """The strategy node behind pump_fade, registered before the epoch, with n forward trade episodes over 20+ days."""
    if monkeypatch is not None:
        monkeypatch.setattr(R, "_now", lambda: "2026-10-01T00:00:00+00:00")
    R.register(j.db, {"id": pid, "level": "strategy", "definition": "d", "detector": "x", "metric": "trade_r",
                      "status": "shadow"})
    R.add_episodes(j.db, pid, [{"symbol": f"C{i}/USDT:USDT", "ts_ms": ms(DAY0 + timedelta(days=i * 0.75)),
                                "source": "live", "outcome": adj[i % len(adj)],
                                "features": {"adj": adj[i % len(adj)]}} for i in range(n)])


def desk(monkeypatch: Any = None, entry_div: int = 2, entry: tuple[str, float] = ("market", 3.0),
         exit_bps: float = 3.0, fees: float = -0.08, adj: tuple[float, ...] = FORWARD, n: int = N,
         path: Any = ":memory:") -> Journal:
    """Every criterion passes (NOW, full mode): 60 trades over 30 entry days on 25 coins, +0.36R a trade."""
    j = Journal(path)
    j.set(j.mode_key(uptime.SINCE_KEY), SINCE)
    j.set(ks.HEARTBEAT_KEY, str(NOW.timestamp() - 30))
    eq = 1000.0
    marks: dict[str, float] = {"2026-10-03": eq}
    for i in range(n):
        opened = DAY0 + timedelta(days=i // entry_div, hours=i % 2)
        closed = DAY0 + timedelta(days=i // 2, hours=6 + i % 2)
        r = CYCLE[i % len(CYCLE)]
        trade(j, r, opened, closed, f"C{i % COINS}/USDT:USDT", entry=entry, exit_bps=exit_bps, fees=fees)
        eq += r * 5
        marks.setdefault((closed + timedelta(days=1)).date().isoformat(), eq)
    for day, e in marks.items():
        j.set(j.mode_key(f"eq_mark:day:{day}"), json.dumps({"equity": e, "ts": 0}))
    forward(j, adj, monkeypatch)
    plumbing(j)
    alert(j, "stop_restored", DAY0 + timedelta(days=3), "C1/USDT:USDT")    # a near-miss: listed, not gated
    return j
