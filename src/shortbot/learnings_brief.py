"""The brief's learnings (cli_brief.py): a small rulebook that is always loaded, the learnings relevant to what the desk
holds or watches right now, and what to re-check. Everything else is searched on demand (the `learnings` tool), so the
brief stays bounded however many learnings accumulate (learnings #82/#83: ACE, ExpeL, the Claude Code memory docs).

- rulebook: at most RULEBOOK_MAX settled learnings (learnings_view.settled: adopted, or supported on a real sample),
  best first (learnings_view.rank), each as its one-line rule.
- relevant now: at most RELEVANT_MAX current learnings the rulebook doesn't show, taken in this order from the ids the
  open trades' plans cite, then searches for the open trades (base symbol + setup), the resting entries and the top
  CANDIDATES_N tradable candidates of the last CANDIDATE_HOURS (one scan batch is only 2-7 coins).
- a footer with the number of current learnings neither part shows, then learnings_view.brief_lines (re-check).
Our own text only, one line each (flat); a learning resting on third-party text is marked, never quoted."""

from __future__ import annotations

import json
import time
from typing import Any

from . import learnings_view as lv
from .untrusted import flat

RULEBOOK_MAX = 20
RELEVANT_MAX = 5
CANDIDATES_N = 3
CANDIDATE_HOURS = 6
THIRD_PARTY = " ⚠️ evidence from third-party text"


def section(j, orders: list[dict[str, Any]]) -> list[str]:
    """The brief's learnings lines; [] when there are none. orders: the resting entries (EntryDesk.pending)."""
    rows = lv.annotate(j, j.learnings())   # not rejected: superseded and expired rows feed the re-check lines
    current = [r for r in rows if not r.get("superseded_by") and "expired" not in r["audit"]["flags"]]
    book = lv.rank([r for r in current if lv.settled(r)])[:RULEBOOK_MAX]
    now = _relevant(j, orders, {r["id"]: r for r in current}, {r["id"] for r in book})
    lines = []
    if current:
        lines.append("### rulebook (settled: adopted, or supported on a real sample; cite the ids an entry relies on "
                     "in plan.uses_learnings)")
        lines += [_line(r) for r in book] or ["- none settled yet: the rest is searched (`learnings` tool)"]
    if now:
        lines.append(f"### relevant now (searched from open trades, resting entries and the last {CANDIDATE_HOURS}h "
                     "of scans)")
        lines += [_line(r, "" if lv.settled(r) else " (not settled)") for r in now]
    if more := len(current) - len(book) - len(now):
        lines.append(f"- {more} more current learnings: `learnings` tool (a query, --setup or --id)")
    return lines + lv.brief_lines(rows)


def _line(r: dict[str, Any], mark: str = "") -> str:
    a = r["audit"]
    score = f" +{a['helpful']}/-{a['harmful']}" if a["helpful"] or a["harmful"] else ""
    return (f"- #{r['id']} [{r['status']} {lv.tag(a)}{score}] {flat(r.get('setup') or 'all', 40)}: {lv.text(r)}"
            + mark + (THIRD_PARTY if a["third_party"] else ""))


def _relevant(j, orders: list[dict[str, Any]], current: dict[int, dict[str, Any]],
              shown: set[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def take(ids: Any) -> None:
        for i in ids:
            if len(out) < RELEVANT_MAX and type(i) is int and i in current and i not in shown:
                shown.add(i)
                out.append(current[i])
    trades = j.open_trades(include_opening=True)
    take(i for t in trades for i in (t.get("plan") or {}).get("uses_learnings") or [])
    for q in _queries(j, trades, orders):
        if len(out) >= RELEVANT_MAX:
            break
        take(r["id"] for r in j.search_learnings(q, limit=RELEVANT_MAX + RULEBOOK_MAX))
    return out


def _queries(j, trades: list[dict[str, Any]], orders: list[dict[str, Any]]) -> list[str]:
    """One search per open trade, resting entry and top recent candidate: its base symbol and setup(s)."""
    base = lambda s: str(s or "").split("/")[0]  # noqa: E731
    qs = [f"{base(x['symbol'])} {x.get('setup') or ''}" for x in [*trades, *orders]]
    since = int(time.time() * 1000) - CANDIDATE_HOURS * 3_600_000
    for sym, setups, _ in j.db.execute(   # each symbol's best row of the window (SQLite: bare columns follow MAX)
            "SELECT symbol, setups, MAX(score) FROM candidates WHERE tradable=1 AND ts_ms >= ? GROUP BY symbol"
            " ORDER BY MAX(score) DESC, symbol LIMIT ?", (since, CANDIDATES_N)):
        try:
            names = " ".join(str(s) for s in json.loads(setups or "[]"))
        except ValueError:
            names = ""
        qs.append(f"{base(sym)} {names}")
    return list(dict.fromkeys(q.strip() for q in qs if q.strip()))
