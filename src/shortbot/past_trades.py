"""What the desk already did and thought about a coin being read (read.py): its own closed trades on this coin, then
the latest of the same setup(s), each with its R, exit reason and the lesson written at the exit; and the latest
decisions logged on the coin (skip, watch, hold, note: why we passed or what we were waiting for). A lesson is stored
per trade (trades.lesson) and a decision per coin (decisions), and nothing else brings them back at decision time;
here they return where the next decision on the same coin or setup is made. Each row is one trade or one moment
(n = 1): it says what to check, never a rule (rules are learnings). Both desks' rows (the claim is about the market,
not the account), never a plumbing drill's."""

from __future__ import annotations

from typing import Any

from .untrusted import flat

PER_GROUP = 3      # trades shown per group, newest first
LESSON_MAX = 400   # a lesson's characters (our own text, one line)
NOTE = "the desk's own closed trades, newest first; each is one trade (n=1): what to check, not a rule"
DECISIONS = ("skip", "watch", "hold", "note", "rejected")   # a session's judgement on the coin, never an order row
DECISIONS_SHOWN = 4
DECISION_DAYS = 7     # older reasons describe another week's tape
DECISION_MAX = 240


def for_coin(journal, symbol: str, setups: list[str]) -> dict[str, Any]:
    """{this_coin, same_setup, note} when a closed trade matches, plus {recent_decisions} when the coin has any;
    {} when there is nothing."""
    out = _trades(journal, symbol, setups)
    if said := _decisions(journal, symbol):
        out["recent_decisions"] = said
    return out


def _decisions(journal, symbol: str) -> list[dict[str, Any]]:
    """The last DECISIONS_SHOWN judgement rows on this coin from the last DECISION_DAYS days, newest first."""
    marks = ",".join("?" * len(DECISIONS))
    rows = journal.db.execute(
        f"SELECT ts, COALESCE(mode, 'demo'), action, reasoning FROM decisions WHERE symbol=? AND action IN ({marks})"
        " AND ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?) ORDER BY id DESC LIMIT ?",
        (symbol, *DECISIONS, f"-{DECISION_DAYS} days", DECISIONS_SHOWN)).fetchall()
    return [{"at": str(ts)[:16].replace("T", " "), "desk": mode, "action": action, "why": flat(why, DECISION_MAX)}
            for ts, mode, action, why in rows]


def _trades(journal, symbol: str, setups: list[str]) -> dict[str, Any]:
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
