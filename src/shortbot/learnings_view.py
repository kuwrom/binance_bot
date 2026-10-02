"""Learning audit (#28): what kind of claim each learning is, its sample (n, days, link) and when it is due a review.

Rows recorded before #28 have no kind or sample. Their values are inferred here for display, marked '?' (listed in
`inferred`) and never written back: the agent or the owner sets them with update_learning, and values set by hand
always win. Everything here is a flag for the brief, the journal tool and research_report, never a gate.

Inference for a row without a kind or n:
- kind: engineering when the evidence names a test (test_x, tests/...), otherwise statistical. process is never
  inferred.
- n / days: only when the text gives no sample of its own (no "n=", no count of events/episodes/trades/candidates...,
  no report/study/backtest) but cites trades ("ZRO #8" with the symbol matching the journal's trade 8, "trade #8",
  "trade:8"): n = distinct trades, days = their distinct open dates. A rule adopted from one trade shows as n=1.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .journal_research import LEARNING_KINDS
from .untrusted import flat

EPISODES_SINCE = "2026-10-01"   # research_report counts symbol-episodes from this date (#27); before it, scan rows
ANECDOTE_N = 30                 # docs/RESEARCH.md: n < 30 is an anecdote
REVIEW_DAYS = {"hypothesis": 14, "supported": 30}   # default review after the last update (supported: statistical)
MAX_IDS = 30                    # ids per brief line; research_report.learnings has them all
DUE_SHOWN = 5                   # due learnings shown with their statement; the rest by id
SECTION = "### learnings to re-check"
SHORT = {"engineering": "eng", "statistical": "stat", "process": "proc"}
TEST_RE = re.compile(r"\btest_\w+|\btests/\w+")
COUNT_RE = re.compile(r"\bn\s*=\s*\d|\b\d[\d,.]*k?\s+(?:[a-z-]+\s+)?(?:events|episodes|trades|candidates|coins|perps|"
                      r"listings|crossings|signals|trials|rows)\b|\b(?:report|study|backtest|bootstrap)\b", re.I)
TRADE_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,14}) #(\d+)\b|\b[Tt]rades? #(\d+)\b|\btrade:(\d+)\b")


def _today(today: date | None) -> date:
    return today or datetime.now(timezone.utc).date()


def _citations(row: dict[str, Any]) -> list[tuple[str | None, int]]:
    text = f"{row.get('statement') or ''} {row.get('evidence') or ''} {' '.join(row.get('provenance') or [])}"
    return [(sym or None, int(a or b or c)) for sym, a, b, c in TRADE_RE.findall(text)]


def _needs_n(row: dict[str, Any], kind: str) -> bool:
    return (kind == "statistical" and row.get("n") is None
            and not COUNT_RE.search(f"{row.get('statement') or ''} {row.get('evidence') or ''}"))


def _review_by(row: dict[str, Any], kind: str) -> tuple[str | None, bool]:
    """(date, is_default): the date set by hand, else last update + REVIEW_DAYS for hypotheses and supported
    statistical learnings; adopted and rejected ones only resurface on a date set by hand."""
    if row.get("review_by"):
        return row["review_by"], False
    st = row.get("status")
    if st == "hypothesis" or (st == "supported" and kind == "statistical"):
        last = date.fromisoformat(str(row["updated_at"])[:10])
        return (last + timedelta(days=REVIEW_DAYS[st])).isoformat(), True
    return None, False


def _audit(row: dict[str, Any], trades: dict[int, dict[str, Any]], today: date) -> dict[str, Any]:
    inferred: list[str] = []
    kind = row.get("kind")
    if kind not in LEARNING_KINDS:
        kind = "engineering" if TEST_RE.search(row.get("evidence") or "") else "statistical"
        inferred.append("kind")
    n, days, cited = row.get("n"), row.get("days"), []
    if _needs_n(row, kind):
        cited = sorted({tid for sym, tid in _citations(row) if tid in trades
                        and (sym is None or trades[tid]["symbol"].split("/")[0] == sym)})
        if cited:
            n = len(cited)
            inferred.append("n")
            if days is None:
                days = len({str(trades[t]["opened_at"] or "")[:10] for t in cited})
                inferred.append("days")
    review_by, default = _review_by(row, kind)
    flags = []
    if row.get("status") != "rejected":
        if review_by and review_by <= today.isoformat():
            flags.append("review_due")
        if kind == "statistical":
            if row.get("status") in ("supported", "adopted") and n is not None and n < ANECDOTE_N:
                flags.append("anecdote")
            if str(row.get("created_at") or "")[:10] < EPISODES_SINCE and row.get("n") is None:
                flags.append("pre_episodes")
            if n is None:
                flags.append("n_unknown")
    out = {"kind": kind, "n": n, "days": days, "sample": row.get("sample"), "review_by": review_by,
           "review_default": default, "inferred": inferred, "flags": flags}
    if cited:
        out["trades"] = cited
    return out


def annotate(j, rows: list[dict[str, Any]], today: date | None = None) -> list[dict[str, Any]]:
    """Each learning plus an `audit` dict (kind, n, days, sample, review_by, inferred fields, flags)."""
    want = sorted({tid for r in rows if _needs_n(r, r.get("kind") or "statistical") for _, tid in _citations(r)})
    trades = {t["id"]: t for t in j._rows(f"SELECT id, symbol, opened_at FROM trades WHERE id IN "
                                          f"({','.join('?' * len(want))})", tuple(want))} if want else {}
    return [{**r, "audit": _audit(r, trades, _today(today))} for r in rows]


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"


def tag(a: dict[str, Any]) -> str:
    """Compact kind/sample tag for a brief line: 'eng', 'stat n=120 40d', 'stat? n=1? 1d?', 'stat n=?'."""
    q = lambda f: "?" if f in a["inferred"] else ""  # noqa: E731
    out = SHORT[a["kind"]] + q("kind")
    if a["kind"] == "statistical":
        out += f" n={_num(a['n'])}{q('n')}" if a["n"] is not None else " n=?"
        if a["days"] is not None:
            out += f" {a['days']}d{q('days')}"
    return out


def _ids(rows: list[dict[str, Any]], fmt=lambda r: f"#{r['id']}", sep: str = ", ") -> str:
    more = f" +{len(rows) - MAX_IDS} more (research_report.learnings)" if len(rows) > MAX_IDS else ""
    return sep.join(fmt(r) for r in rows[:MAX_IDS]) + more


def _n(r: dict[str, Any]) -> str:
    a = r["audit"]
    return f"#{r['id']} (n={_num(a['n'])}{'?' * ('n' in a['inferred'])})"


def brief_lines(annotated: list[dict[str, Any]]) -> list[str]:
    """The brief's 'learnings to re-check' section: due learnings with their statement (stale ones are not among the
    10 most recent), then one line per flag. No line starts with '- #'. Empty when nothing is flagged."""
    def having(flag: str) -> list[dict[str, Any]]:
        return sorted((r for r in annotated if flag in r["audit"]["flags"]), key=lambda r: r["id"])
    lines = []
    due = sorted(having("review_due"), key=lambda r: (r["audit"]["review_by"], r["id"]))
    lines += [f"- review due since {r['audit']['review_by']}: #{r['id']} [{r['status']}/{r['confidence']} "
              f"{tag(r['audit'])}] {flat(r['statement'], 160)}" for r in due[:DUE_SHOWN]]
    if len(due) > DUE_SHOWN:
        lines.append(f"- review due, {len(due) - DUE_SHOWN} more: " + _ids(due[DUE_SHOWN:]))
    if small := having("anecdote"):
        lines.append(f"- supported/adopted on n < {ANECDOTE_N} (an anecdote, docs/RESEARCH.md): "
                     + _ids(small, _n)
                     + ". The status stands until the agent or the owner decides")
    if old := having("pre_episodes"):
        lines.append(f"- statistics from before episode counting ({EPISODES_SINCE}; row counts overstate n): "
                     + _ids(old, sep=" ")
                     + ". Re-state n/days/sample from research_report or the study with update_learning")
    if k := sum("kind" in r["audit"]["inferred"] for r in annotated):
        lines.append(f"- {k} learning{'s' * (k != 1)} with no kind ('?' = inferred from the text): set kind, n, days "
                     "and sample with update_learning")
    return [f"{SECTION} (record what you find with update_learning: evidence, status, n/days/sample or a new "
            "review_by)", *lines] if lines else []


def audit(j, today: date | None = None) -> dict[str, Any]:
    """research_report's learnings section: every active statistical learning with its sample (lowest known n
    first), counts by kind, and the ids behind each flag."""
    rows = annotate(j, j.learnings(), today)
    stat = sorted((r for r in rows if r["audit"]["kind"] == "statistical"),
                  key=lambda r: (r["audit"]["n"] is None, r["audit"]["n"] or 0, r["id"]))
    ids = lambda flag: sorted(r["id"] for r in rows if flag in r["audit"]["flags"])  # noqa: E731
    return {
        "by_kind": {k: sum(r["audit"]["kind"] == k for r in rows) for k in LEARNING_KINDS},
        "kind_inferred": sum("kind" in r["audit"]["inferred"] for r in rows),
        "statistical": [{"id": r["id"], "status": r["status"], "confidence": r["confidence"], "setup": r["setup"],
                         **{k: r["audit"][k] for k in ("n", "days", "sample", "review_by", "inferred", "flags")},
                         "created": str(r["created_at"])[:10], "statement": " ".join(r["statement"].split())[:100]}
                        for r in stat],
        "review_due": ids("review_due"), "anecdote": ids("anecdote"), "pre_episodes": ids("pre_episodes"),
        "n_unknown": ids("n_unknown"),
        "how_to_read": f"n = independent observations (episodes, trades or events, never scan rows), days = distinct "
                       f"days. Fields in `inferred` were read from the text of a learning recorded before #28: set "
                       f"them with update_learning (kind, n, days, sample, review_by); values set by hand win. "
                       f"anecdote = supported/adopted on n < {ANECDOTE_N}; pre_episodes = statistics recorded before "
                       f"{EPISODES_SINCE}, when research_report counted rows; review_due = past review_by (default: "
                       f"last update + {REVIEW_DAYS['hypothesis']} days for hypotheses, "
                       f"+ {REVIEW_DAYS['supported']} for supported statistical learnings). Flags, not gates.",
    }
