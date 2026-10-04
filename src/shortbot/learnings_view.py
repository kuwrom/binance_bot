"""Learning audit (#28): what kind of claim each learning is, its sample (n, days, link) and when it is due a review;
its outcome score (trades whose plan cited it) and the rulebook gate (`settled`).

Rows recorded before #28 have no kind or sample. Their values are inferred here for display, marked '?' (listed in
`inferred`) and never written back: the agent or the owner sets them with update_learning, and values set by hand
always win. The flags are for the brief, the journal tool and research_report; only `settled` gates anything, and only
what the brief's rulebook shows.

Inference for a row without a kind or n:
- kind: engineering when the evidence names a test (test_x, tests/...), otherwise statistical. process is never
  inferred.
- n / days: only when the text gives no sample of its own (no "n=", no count of events/episodes/trades/candidates...,
  no report/study/backtest) but cites trades ("ZRO #8" with the symbol matching the journal's trade 8, "trade #8",
  "trade:8"): n = distinct trades, days = their distinct open dates. A rule adopted from one trade shows as n=1.

Scores come from outcomes, never from votes: each closed trade (any mode) whose plan.uses_learnings cites a learning
counts for it as helpful (R > SCORE_R), harmful (R < -SCORE_R) or neutral. They are computed when read, so a partial
close or a re-finalised trade can't count twice.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from . import alerts_view
from .journal_learnings import LEARNING_KINDS
from .untrusted import flat, third_party_alert, third_party_tag

EPISODES_SINCE = "2026-10-01"   # research_report counts symbol-episodes from this date (#27); before it, scan rows
ANECDOTE_N = 30                 # docs/RESEARCH.md: n < 30 is an anecdote
REVIEW_DAYS = {"hypothesis": 14, "supported": 30}   # default review after the last update (supported: statistical)
MAX_IDS = 30                    # ids per brief line; research_report.learnings has them all
DUE_SHOWN = 5                   # due learnings shown with their statement; the rest by id
SCORE_R = 0.05                  # |R| at or below this: a scratch, neither helpful nor harmful
HARMFUL_BY = 2                  # flagged harmful at harmful >= helpful + HARMFUL_BY
TEXT_MAX = 160                  # a rulebook line's text: the rule, else the statement cut to this
UNSETTLED = {"anecdote", "n_unknown", "harmful"}   # a supported learning with one of these stays out of the rulebook
SECTION = "### learnings to re-check"
SHORT = {"engineering": "eng", "statistical": "stat", "process": "proc"}
TEST_RE = re.compile(r"\btest_\w+|\btests/\w+")
COUNT_RE = re.compile(r"\bn\s*=\s*\d|\b\d[\d,.]*k?\s+(?:[a-z-]+\s+)?(?:events|episodes|trades|candidates|coins|perps|"
                      r"listings|crossings|signals|trials|rows)\b|\b(?:report|study|backtest|bootstrap)\b", re.I)
TRADE_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,14}) #(\d+)\b|\b[Tt]rades? #(\d+)\b|\btrade:(\d+)\b")


def _today(today: date | None) -> date:
    return today or datetime.now(UTC).date()


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


def scores(j) -> dict[int, dict[str, Any]]:
    """{learning id: helpful, harmful, cited_by (trade ids)} from the closed trades whose plan cited it (all modes:
    the claim is about the market, not the account). A trade citing an id twice counts once; R unknown = neutral."""
    out: dict[int, dict[str, Any]] = {}
    for t in j.closed_results(all_modes=True):
        r = t.get("r_multiple")
        for lid in dict.fromkeys(t.get("uses_learnings") or []):
            s = out.setdefault(lid, {"helpful": 0, "harmful": 0, "cited_by": []})
            s["cited_by"].append(t["id"])
            if r is not None and r > SCORE_R:
                s["helpful"] += 1
            elif r is not None and r < -SCORE_R:
                s["harmful"] += 1
    return out


def third_party(j, provenance: list[str] | None) -> bool:
    """Did a learning's evidence come from third-party text (a news tool, or a news/announcement alert)?"""
    for tag in provenance or []:
        if third_party_tag(tag):
            return True
        if tag.startswith("alert:") and tag[6:].isdigit():
            kind = alerts_view.kind_of(j, int(tag[6:]))
            if kind and third_party_alert(kind):
                return True
    return False


def _audit(row: dict[str, Any], trades: dict[int, dict[str, Any]], today: date,
           score: dict[str, Any]) -> dict[str, Any]:
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
    if row.get("status") != "rejected" and not row.get("superseded_by"):   # a superseded row is history: no flags
        if review_by and review_by <= today.isoformat():
            flags.append("review_due")
        if row.get("valid_until") and str(row["valid_until"]) < today.isoformat():
            flags.append("expired")
        if kind == "statistical":
            if row.get("status") in ("supported", "adopted") and n is not None and n < ANECDOTE_N:
                flags.append("anecdote")
            if str(row.get("created_at") or "")[:10] < EPISODES_SINCE and row.get("n") is None:
                flags.append("pre_episodes")
            if n is None:
                flags.append("n_unknown")
        if score["harmful"] and score["harmful"] >= score["helpful"] + HARMFUL_BY:
            flags.append("harmful")
    out = {"kind": kind, "n": n, "days": days, "sample": row.get("sample"), "review_by": review_by,
           "review_default": default, "inferred": inferred, "flags": flags, "helpful": score["helpful"],
           "harmful": score["harmful"], "cited_by": score["cited_by"]}
    if cited:
        out["trades"] = cited
    return out


def annotate(j, rows: list[dict[str, Any]], today: date | None = None) -> list[dict[str, Any]]:
    """Each learning plus an `audit` dict (kind, n, days, sample, review_by, inferred fields, flags, the outcome score:
    helpful, harmful, cited_by, and third_party: the evidence rests on third-party text)."""
    want = sorted({tid for r in rows if _needs_n(r, r.get("kind") or "statistical") for _, tid in _citations(r)})
    trades = {t["id"]: t for t in j._rows(f"SELECT id, symbol, opened_at FROM trades WHERE id IN "
                                          f"({','.join('?' * len(want))})", tuple(want))} if want else {}
    sc, day, out = scores(j), _today(today), []
    for r in rows:
        a = _audit(r, trades, day, sc.get(r["id"], {"helpful": 0, "harmful": 0, "cited_by": []}))
        a["third_party"] = third_party(j, r.get("provenance"))
        x = {**r, "audit": a}
        if settled(x) and not r.get("rule"):
            a["flags"].append("missing_rule")
        out.append(x)
    return out


def settled(r: dict[str, Any]) -> bool:
    """The rulebook gate: not superseded, not expired, not engineering, and adopted, or supported with a real sample
    (no anecdote, n_unknown or harmful flag). Everything else is searched on demand (the `learnings` tool)."""
    a = r["audit"]
    if r.get("superseded_by") or "expired" in a["flags"] or a["kind"] == "engineering":
        return False
    return r["status"] == "adopted" or (r["status"] == "supported" and not UNSETTLED & set(a["flags"]))


def rank(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adopted first, then helpful minus harmful, then n, then the latest update."""
    def key(r: dict[str, Any]) -> tuple[Any, ...]:
        a = r["audit"]
        return (r["status"] == "adopted", a["helpful"] - a["harmful"], a["n"] or 0, str(r.get("updated_at") or ""),
                r["id"])
    return sorted(rows, key=key, reverse=True)


def text(r: dict[str, Any]) -> str:
    """A learning in one line: its rule, else its statement cut to TEXT_MAX."""
    return flat(r.get("rule") or r["statement"], TEXT_MAX)


def index(rows: list[dict[str, Any]], limit: int = 60) -> list[dict[str, Any]]:
    """Annotated learnings as compact rows, best first (rank): the journal tool's view. The full rows (statement,
    evidence, provenance) come from the `learnings` tool."""
    return [{"id": r["id"], "status": r["status"], "tag": tag(r["audit"]), "setup": r.get("setup"), "text": text(r),
             "helpful": r["audit"]["helpful"], "harmful": r["audit"]["harmful"],
             "flags": r["audit"]["flags"] + (["third_party"] if r["audit"]["third_party"] else [])}
            for r in rank(rows)[:limit]]


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


def _score(r: dict[str, Any]) -> str:
    a = r["audit"]
    return f"#{r['id']} (+{a['helpful']}/-{a['harmful']})"


def brief_lines(annotated: list[dict[str, Any]]) -> list[str]:
    """The brief's 'learnings to re-check' section: due learnings with their statement (most of them are not in the
    rulebook), then one line per flag. No line starts with '- #'. Empty when nothing is flagged."""
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
    if bad := having("harmful"):
        lines.append(f"- cited by losing trades (harmful >= helpful + {HARMFUL_BY}): " + _ids(bad, _score)
                     + ". Re-check the claim; supersede or reject it with update_learning")
    if gone := having("expired"):
        lines.append("- past valid_until: " + _ids(gone, lambda r: f"#{r['id']} ({r['valid_until']})")
                     + ". Extend valid_until, supersede or reject it with update_learning")
    if k := sum("kind" in r["audit"]["inferred"] for r in annotated if not r.get("superseded_by")):
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
        "n_unknown": ids("n_unknown"), "harmful": ids("harmful"), "expired": ids("expired"),
        "missing_rule": ids("missing_rule"),
        "how_to_read": f"n = independent observations (episodes, trades or events, never scan rows), days = distinct "
                       f"days. Fields in `inferred` were read from the text of a learning recorded before #28: set "
                       f"them with update_learning (kind, n, days, sample, review_by); values set by hand win. "
                       f"anecdote = supported/adopted on n < {ANECDOTE_N}; pre_episodes = statistics recorded before "
                       f"{EPISODES_SINCE}, when research_report counted rows; review_due = past review_by (default: "
                       f"last update + {REVIEW_DAYS['hypothesis']} days for hypotheses, "
                       f"+ {REVIEW_DAYS['supported']} for supported statistical learnings); harmful = cited by closed "
                       f"trades with R < -{SCORE_R} at least {HARMFUL_BY} times more than with R > +{SCORE_R}; "
                       f"expired = past valid_until; missing_rule = in the brief's rulebook without a one-line rule "
                       f"(it shows the statement cut to {TEXT_MAX} chars). Flags, not gates: only the rulebook is "
                       f"gated (adopted, or supported without anecdote, n_unknown or harmful).",
    }
