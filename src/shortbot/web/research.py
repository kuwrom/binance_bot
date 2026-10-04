"""What the desk already knows, read from the journal for the web console (docs/WEB.md): the latest scan, one coin's
standing, and the odds on the open book. Read-only: no exchange call, no write, no DDL. The guardian fills these
tables (candidates, predictions, ladder_shadow); a page that reads them costs no exchange weight and logs nothing."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .. import coin_memory
from ..entry_intents import ON_BOOK
from ..exchange import to_symbol

FEATURES = ("change_24h", "quote_volume", "funding_8h", "oi_value", "oi_change_24h", "rsi_1h", "vwap_z",
            "off_high_pct", "liq_tier", "lifecycle_stage", "listing_age_days")
HOUR_MS = 3_600_000
SEEN_DAYS = 7      # the search box suggests coins the scan scored in this many days
HISTORY = 12       # scan rows in a coin's history


def _json(text: Any, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def _lazy(j: Any, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    """Rows of a table its owner creates on first use (entries, ladder_shadow): none before that."""
    try:
        return j._rows(sql, args)
    except sqlite3.OperationalError:
        return []


def _candidate(row: dict[str, Any], pred: dict[str, Any] | None, now_ms: int) -> dict[str, Any]:
    """A scan row for the page: its reasons, a few features, and the scan's no-edge bracket odds."""
    f = _json(row.get("features"), {})
    ctx = _json(pred.get("context"), {}) if pred else {}
    return {
        "id": row["id"], "symbol": row["symbol"], "ts": row["ts"], "age_s": max(0, (now_ms - row["ts_ms"]) // 1000),
        "price": row["price"], "score": row["score"], "tradable": bool(row["tradable"]), "regime": row.get("regime"),
        "setups": _json(row.get("setups"), []), "vetoes": _json(row.get("vetoes"), []),
        "signals": _json(row.get("signals"), []), "tape": ctx.get("tape_state"),
        "p_target": pred.get("p_model") if pred else None, "stop": pred.get("stop") if pred else None,
        "target": pred.get("target") if pred else None,
        "outcome": {k: row.get(k) for k in ("short_ret_4h", "short_ret_24h", "short_ret_72h")}
        if row.get("labelled_at") else None,
        "features": {k: f.get(k) for k in FEATURES if f.get(k) is not None},
    }


def _scan_predictions(j: Any, since_ms: int, symbol: str | None = None) -> dict[str, dict[str, Any]]:
    """{symbol: its newest scan bracket} since `since_ms`."""
    where, args = ("AND symbol=?", (symbol,)) if symbol else ("", ())
    rows = j._rows("SELECT p.symbol, p.p_model, p.stop, p.target, p.context FROM predictions p JOIN (SELECT MAX(id) id "
                   f"FROM predictions WHERE source='scan' AND ts_ms >= ? {where} GROUP BY symbol) m ON p.id = m.id",
                   (since_ms, *args))
    return {r["symbol"]: r for r in rows}


def candidates(j: Any, now_ms: int, hours: float = 6.0, limit: int = 40) -> dict[str, Any]:
    """The newest scored row per coin within `hours`: tradable and vetoed, each best score first, plus the watchlist
    and every coin the scan scored in SEEN_DAYS (the search box's suggestions)."""
    since = now_ms - int(hours * HOUR_MS)
    rows = j._rows("SELECT c.* FROM candidates c JOIN (SELECT MAX(id) id FROM candidates WHERE ts_ms >= ? "
                   "GROUP BY symbol) m ON c.id = m.id ORDER BY c.score DESC, c.id DESC", (since,))
    preds = _scan_predictions(j, since)
    out = [_candidate(r, preds.get(r["symbol"]), now_ms) for r in rows]
    tradable, vetoed = [c for c in out if c["tradable"]], [c for c in out if not c["tradable"]]
    seen = j._rows("SELECT DISTINCT symbol FROM candidates WHERE ts_ms >= ? ORDER BY symbol",
                   (now_ms - SEEN_DAYS * 24 * HOUR_MS,))
    return {"hours": hours, "as_of": max((r["ts"] for r in rows), default=None), "scored": len(out),
            "n_tradable": len(tradable), "n_vetoed": len(vetoed), "tradable": tradable[:limit],
            "vetoed": vetoed[:limit], "watchlist": coin_memory.watching(j.db, now_ms),
            "symbols": [r["symbol"] for r in seen]}


def _standing(trade: Any, entries: list, watch: Any, latest: dict[str, Any] | None, hours: float) -> dict[str, str]:
    """Where the coin stands with the desk, most binding first, and why in one line."""
    if trade:
        return {"state": "position", "why": f"open short, trade #{trade['id']} ({trade.get('setup') or 'no setup'})"}
    if entries:
        return {"state": "resting", "why": f"{len(entries)} limit entr{'y' if len(entries) == 1 else 'ies'} on the book"}
    fresh = latest if latest is not None and latest["age_s"] <= hours * 3600 else None
    if fresh and fresh["tradable"]:
        return {"state": "candidate", "why": f"tradable at the last scan, score {fresh['score']}"
                + (f", setup {', '.join(fresh['setups'])}" if fresh["setups"] else ", no setup detected")}
    if fresh:
        return {"state": "vetoed", "why": "scored but vetoed: " + "; ".join(fresh["vetoes"][:2])}
    if watch:
        return {"state": "watchlist", "why": watch.get("reason") or "on the watchlist"}
    if latest is not None:
        return {"state": "stale", "why": f"last scored {latest['age_s'] // 3600} h ago: it has not reached the scan since"}
    return {"state": "unseen", "why": "never scored: the scan only scores coins that move or trip a trigger"}


def coin(j: Any, symbol: str, now_ms: int, hours: float = 6.0) -> dict[str, Any]:
    """One coin's standing with the desk: an open short, resting entries, the watchlist, its last scan (with why it
    was tradable or vetoed), its scan history and labelled outcomes, the session's current read and our past trades."""
    sym = to_symbol(symbol)
    trade = j.active_trade(sym)
    entries = _lazy(j, f"SELECT id, group_id, status, limit_price, stop_price, setup, expires_ms FROM entries WHERE "
                      f"symbol=? AND mode=? AND {ON_BOOK} ORDER BY id", (sym, j.mode))
    watch = next((w for w in coin_memory.watching(j.db, now_ms) if w.get("symbol") == sym), None)
    rows = j._rows("SELECT * FROM candidates WHERE symbol=? ORDER BY id DESC LIMIT ?", (sym, HISTORY))
    pred = _scan_predictions(j, rows[0]["ts_ms"] - HOUR_MS, sym).get(sym) if rows else None
    latest = _candidate(rows[0], pred, now_ms) if rows else None
    labelled = j._rows("SELECT COUNT(*) n, AVG(short_ret_24h) avg, AVG(short_ret_24h > 0) hit FROM candidates WHERE "
                       "symbol=? AND short_ret_24h IS NOT NULL", (sym,))[0]
    closed = j._rows("SELECT id, setup, pnl, closed_at, exit_reason FROM trades WHERE symbol=? AND mode=? AND "
                     "status='closed' ORDER BY id DESC LIMIT 5", (sym, j.mode))
    try:
        read = coin_memory.current_read(j.db, sym, now_ms)
    except sqlite3.OperationalError:   # no coin-memory tables yet
        read = None
    return {
        "symbol": sym, "standing": _standing(trade, entries, watch, latest, hours),
        "trade": {k: trade.get(k) for k in ("id", "status", "setup", "entry_price", "stop_price", "opened_at",
                                            "conviction", "thesis")} if trade else None,
        "entries": entries, "watch": watch, "latest": latest,
        "history": [{"ts": r["ts"], "score": r["score"], "tradable": bool(r["tradable"]), "price": r["price"],
                     "vetoes": len(_json(r.get("vetoes"), []))} for r in rows],
        # scan rows overlap (one move is scored many times): an anecdote about the coin, not a sample
        "outcomes": {"rows": labelled["n"], "avg_short_ret_24h": labelled["avg"],
                     "hit_rate": labelled["hit"]} if labelled["n"] else None,
        "read": {k: read.get(k) for k in ("ts", "read", "bias", "levels", "invalidation", "expires_ms")} if read else None,
        "trades": closed,
    }


def book(j: Any, now_ms: int) -> dict[str, Any]:
    """The odds on the open book, as the guardian last logged them: per open short its ladder (probability of each next
    level against the no-edge baseline, EV in R, the state score and what the rule would hold), per resting entry its
    fill and target odds at placement."""
    ladders: dict[str, dict[str, Any]] = {}
    for t in j.open_trades(include_opening=True):
        rows = _lazy(j, "SELECT * FROM ladder_shadow WHERE trade_id=? AND COALESCE(variant,'base')='base' "
                       "ORDER BY id DESC LIMIT 1", (t["id"],))
        if not rows:
            continue
        r = rows[0]
        ladders[t["symbol"]] = {
            "trade_id": t["id"], "ts": r["ts"], "price": r["price"], "score": r["score"],
            "parts": _json(r["parts"], {}), "levels": _json(r["levels"], []), "x_star": r["x_star"],
            "x_actual": r["x_actual"], "action": r["action"], "entry": r["entry"], "init_r": r["init_r"],
            "live": bool((t.get("plan") or {}).get("ladder_live")), "conviction": t.get("conviction"),
        }
    odds: dict[str, dict[str, Any]] = {}
    ids = [r["id"] for r in _lazy(j, f"SELECT id FROM entries WHERE mode=? AND {ON_BOOK}", (j.mode,))]
    for eid in ids:
        for p in j._rows("SELECT source, p_model, p_analyst, hours FROM predictions WHERE ref_id=? AND source IN "
                         "('entry', 'entry_fill') ORDER BY id", (eid,)):
            o = odds.setdefault(str(eid), {})
            if p["source"] == "entry_fill":
                o["p_fill"], o["fill_hours"] = p["p_model"], p["hours"]
            else:
                o["p_target"], o["p_analyst"], o["hours"] = p["p_model"], p["p_analyst"], p["hours"]
    return {"ladders": ladders, "entries": odds}
