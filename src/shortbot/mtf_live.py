"""Every coin's 5m / 15m / 1h / 4h / 1d state at once, live, from the stream store (task #41). Zero exchange calls.

- view(store, symbol): mtf.latest over each timeframe's CLOSED bars (the forming bar is dropped), taker imbalance
  where the store has taker volume. The same mtf.series code as the chain study (backtest/chain_study.py).
- A guardian scan-lane job logs every streamed coin's view at each 15m close to data/research/mtf.db. That is the
  forward record the chain rules are judged on, and the input tier-1 chain triggers read once a chain earns it.

    .venv/bin/python -m shortbot.mtf_live BTC [ETH ...]     # print the views
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import mtf
from .market import stream_read as R
from .market import streams as S

M15 = S.TF_MS["15m"]
BARS = (150, 110, 40)        # tried in order: enough for move z (LOOK + 96 + 1) when the store has it
KEEP_DAYS = 30
SCHEMA = """
CREATE TABLE IF NOT EXISTS views (ts INTEGER NOT NULL, symbol TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (ts, symbol)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _closed(store: Path | str, symbol: str, tf: str, now_ms: int) -> list[list[float]] | None:
    step = S.TF_MS[tf]
    for n in BARS:
        rows = R.flow_candles(symbol, tf, n + 1, store, now_ms) or R.candles(symbol, tf, n + 1, store, now_ms)
        if rows:
            return [r for r in rows if r[0] + step <= now_ms]
    return None


def view(store: Path | str, symbol: str, now_ms: int) -> dict[str, dict[str, Any]]:
    return mtf.latest({tf: _closed(store, symbol, tf, now_ms) for tf in mtf.TFS})


def db_path() -> Path:
    from . import provenance as P
    return P.data_dir() / "research" / "mtf.db"


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path or db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(p), timeout=15)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def run(store: Path | str, now_ms: int, path: Path | str | None = None) -> dict[str, Any]:
    """At most once per 15m close: every streamed coin's view, logged; old rows pruned."""
    close = now_ms // M15 * M15
    db = connect(path)
    try:
        r = db.execute("SELECT value FROM meta WHERE key='last_close'").fetchone()
        if r and int(r[0]) >= close:
            return {"logged": 0}
        coins = R.fresh_symbols("kline_15m", 90, store, now_ms)
        rows = [(close, s, json.dumps(view(store, s, now_ms), separators=(",", ":"))) for s in coins]
        with db:
            db.executemany("INSERT OR REPLACE INTO views VALUES (?,?,?)", rows)
            db.execute("INSERT OR REPLACE INTO meta VALUES ('last_close', ?)", (str(close),))
            db.execute("DELETE FROM views WHERE ts < ?", (close - KEEP_DAYS * 86_400_000,))
        return {"logged": len(rows), "close": close}
    finally:
        db.close()


def main(argv: list[str] | None = None) -> None:
    import time
    from .exchange import to_raw
    names = sys.argv[1:] if argv is None else argv
    store = S.path_for(str(db_path().parent.parent / "shortbot.db"))
    now = int(time.time() * 1000)   # wall-clock: a live read
    for n in names:
        v = view(store, to_raw(n), now)
        print(f"== {to_raw(n)}")
        for tf in mtf.TFS:
            if tf in v:
                print(f"  {tf:>3} " + " ".join(f"{k}={v[tf][k]}" for k in mtf.COLUMNS))


if __name__ == "__main__":
    main()
