"""Between-scan watch for a session (docs/DOCTRINE.md "The loop"): every 45 s, one stdout line only when a watched coin
crosses one of its levels (entry, stop, targets, triggers), its funding changes sign, or a new alert lands for this
desk. Meant for Claude Code's Monitor tool, which wakes the session on each line. Read-only: public mark price and
funding, and the journal opened read-only. Its reads go through the weight guard (shortbot.http, desk class): while
the IP is near its limit or banned (a 429/418 any process on the host saw) it skips polls and says so once.

    .venv/bin/python -m shortbot.level_watch --level ZRO=1.85,1.909,2.005 --level GTC=0.17,0.21
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from . import http, weight
from .alerts_view import MINE
from .config import Settings

EVERY_S = 45
_sleep = time.sleep   # tests replace it
BUFFER = 0.003   # a crossing counts once price is 0.3% past the level: no line per tick while it sits on it


def zone(price: float, levels: list[float], prev: int | None = None, buffer: float = BUFFER) -> int:
    """How many levels price is at or above. A change from `prev` counts only once price is `buffer` past the level
    it crossed; until then the previous zone holds."""
    z = sum(price >= lv for lv in levels)
    if prev is None or z == prev:
        return z
    edge = levels[min(z, prev)]
    return z if abs(price / edge - 1) >= buffer else prev


def parse_levels(specs: list[str]) -> dict[str, list[float]]:
    """['ZRO=1.85,2.005'] -> {'ZROUSDT': [1.85, 2.005]}"""
    out: dict[str, list[float]] = {}
    for spec in specs:
        coin, _, raw = spec.partition("=")
        if not coin or not raw:
            raise ValueError(f"--level COIN=price,price,... ; got {spec!r}")
        sym = coin.upper().removesuffix("/USDT:USDT").removesuffix("USDT") + "USDT"
        out[sym] = sorted(float(x) for x in raw.split(","))
    return out


def _premium(sym: str) -> tuple[float, float]:
    """Mark price and funding (premiumIndex, weight 1). Never waits: a blocked poll is skipped (weight.BudgetWait)."""
    d = http.get("/fapi/v1/premiumIndex", {"symbol": sym}, cls=weight.DESK, wait_s=0)
    return float(d["markPrice"]), float(d["lastFundingRate"])


def _backoff(e: Exception) -> str | None:
    """Why polls pause, for a budget wait or a limit answer (429/418/-1003); None for any other error."""
    if isinstance(e, weight.BudgetWait):
        return str(e)
    if isinstance(e, http.BinanceHTTPError) and e.limited:
        return str(e)
    return None


def _alerts(db: Path, mode: str, after: int | None) -> tuple[int, list[str]]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        top = c.execute(f"SELECT MAX(id) FROM alerts WHERE {MINE}", (mode,)).fetchone()[0] or 0
        if after is None or top <= after:
            return top, []
        rows = c.execute(f"SELECT id, kind, symbol, substr(message, 1, 140) FROM alerts WHERE id > ? AND {MINE} "
                         "ORDER BY id", (after, mode)).fetchall()
        return top, [f"alert #{i} {k} {s or ''}: {m}" for i, k, s, m in rows]
    finally:
        c.close()


def run(levels: dict[str, list[float]], db: Path, mode: str, every_s: float = EVERY_S, out: Any = sys.stdout,
        polls: int | None = None) -> None:
    """Poll every `every_s` until killed (`polls`: stop after that many, for tests)."""
    state: dict[str, tuple[int, bool]] = {}
    last_alert: int | None = None
    paused = False
    while polls is None or polls > 0:
        polls = None if polls is None else polls - 1
        try:
            for sym, lv in levels.items():
                px, fr = _premium(sym)
                old = state.get(sym)
                z, neg = zone(px, lv, old[0] if old else None), fr < 0
                if old and (old[0] != z or old[1] != neg):
                    print(f"{sym} {px:g} zone {old[0]}->{z} of {lv}; funding {fr:+.5f}", file=out, flush=True)
                state[sym] = (z, neg)
            if paused:
                print("watch resumed: prices readable again", file=out, flush=True)
                paused = False
        except Exception as e:  # noqa: BLE001 - one failed poll never ends the watch
            why = _backoff(e)
            if why is None:
                print(f"watch error: {type(e).__name__}: {e}", file=out, flush=True)
            elif not paused:   # one line per pause: every line wakes the session
                print(f"watch paused (weight guard, prices not read): {why}", file=out, flush=True)
                paused = True
        try:
            last_alert, lines = _alerts(db, mode, last_alert)
            for ln in lines:
                print(ln, file=out, flush=True)
        except Exception as e:  # noqa: BLE001 - one failed poll never ends the watch
            print(f"watch error: {type(e).__name__}: {e}", file=out, flush=True)
        _sleep(every_s)


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m shortbot.level_watch", description=__doc__.split("\n\n")[0])
    p.add_argument("--level", action="append", default=[], help="COIN=price,price,... (repeatable)")
    p.add_argument("--every", type=float, default=EVERY_S)
    a = p.parse_args()
    s = Settings.from_env()
    run(parse_levels(a.level), Path(s.db_path), s.mode, a.every)


if __name__ == "__main__":
    main()
