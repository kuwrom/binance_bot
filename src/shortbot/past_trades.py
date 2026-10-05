"""The desk's own closed trades that bear on a coin being read (read.py): this coin's, then the latest of the same
setup(s), each with its R, exit reason and the lesson written at the exit. A lesson is stored per trade
(trades.lesson) and nothing else brings it back at decision time; here it returns where the next decision on the same
coin or setup is made. Each row is one trade (n = 1): an anecdote that says what to check, never a rule (rules are
learnings). All desks' trades (the claim is about the market, not the account), never a plumbing drill's."""

from __future__ import annotations

from typing import Any

from .untrusted import flat

PER_GROUP = 3      # trades shown per group, newest first
LESSON_MAX = 400   # a lesson's characters (our own text, one line)
NOTE = "the desk's own closed trades, newest first; each is one trade (n=1): what to check, not a rule"


def for_coin(journal, symbol: str, setups: list[str]) -> dict[str, Any]:
    """{this_coin, same_setup, note}; {} when no closed trade matches."""
    closed = {t["id"]: t for t in journal.closed_results(all_modes=True)}
    if not closed:
        return {}
    marks = ",".join("?" * len(closed))
    text = {i: (reason, lesson) for i, reason, lesson in journal.db.execute(
        f"SELECT id, exit_reason, lesson FROM trades WHERE id IN ({marks})", tuple(closed))}
    newest = sorted(closed.values(), key=lambda t: (t.get("closed_at") or "", t["id"]), reverse=True)
    base = symbol.split("/")[0]
    mine = lambda t: str(t["symbol"]).split("/")[0] == base  # noqa: E731
    coin = [t for t in newest if mine(t)][:PER_GROUP]
    setup = [t for t in newest if not mine(t) and t.get("setup") in set(setups)][:PER_GROUP]   # other coins'

    def row(t: dict[str, Any]) -> dict[str, Any]:
        reason, lesson = text.get(t["id"], (None, None))
        r = t.get("r_multiple")
        return {"trade": t["id"], "coin": str(t["symbol"]).split("/")[0], "setup": t.get("setup"),
                "closed": str(t.get("closed_at") or "")[:10], "r": None if r is None else round(r, 2),
                "exit": flat(reason, 80), "lesson": flat(lesson, LESSON_MAX) or None}
    if not coin and not setup:
        return {}
    return {"this_coin": [row(t) for t in coin], "same_setup": [row(t) for t in setup], "note": NOTE}
