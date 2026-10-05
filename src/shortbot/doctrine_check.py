"""Do the files every session loads still agree with the journal? docs/DOCTRINE.md states rules and cites the
learnings behind them as ids in brackets, "(24)" or "(15, 36)"; the skills cite "learning #29". Those files are written
by hand and loaded as settled, while a learning's status moves on in the journal (rejected, superseded, expired, or
never past a hypothesis). The brief names each cited id the rulebook gate (learnings_view.settled) would not show, so
the drift is seen at the next session start and fixed in `research-review`: re-state the learning's sample, or reword
or drop the rule. A missing file (the docker image ships no docs) checks nothing."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import learnings_view as lv

ROOT = Path(__file__).resolve().parents[2]
DOCTRINE = ROOT / "docs" / "DOCTRINE.md"
SKILLS = ROOT / ".claude" / "skills"
BRACKET_RE = re.compile(r"\((\d{1,4}(?:, \d{1,4})*)\)")             # DOCTRINE.md: "(44)", "(15, 36)"
LEARNING_RE = re.compile(r"\blearnings? ((?:#\d+(?:, | and )?)+)")   # skills: "learning #29", "learnings #79, #80"


def cited(doctrine: Path | None = None, skills: Path | None = None) -> dict[int, str]:
    """{learning id: the first file citing it} for the doctrine and every SKILL.md."""
    doctrine, skills = doctrine or DOCTRINE, skills or SKILLS
    out: dict[int, str] = {}
    files = [(doctrine, BRACKET_RE)] + [(p, LEARNING_RE) for p in sorted(skills.glob("*/SKILL.md"))]
    for path, pattern in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        name = path.name if path == doctrine else f"{path.parent.name} skill"
        for m in pattern.finditer(text):
            for i in re.findall(r"\d+", m.group(1)):
                out.setdefault(int(i), name)
    return out


def _why(row: dict[str, Any] | None) -> str | None:
    """Why the rulebook would not show this learning; None when it is settled."""
    if row is None:
        return "no such learning"
    if row.get("superseded_by"):
        return f"superseded by #{row['superseded_by']}"
    if row["status"] in ("rejected", "hypothesis"):
        return row["status"]
    flags = row["audit"]["flags"]
    if "expired" in flags:
        return "expired"
    if row["audit"]["kind"] == "engineering" or lv.settled(row):
        return None   # an engineering fact is checked by its test, not by a sample
    return next((w for f, w in (("harmful", "cited by losing trades"), ("anecdote", "n < 30"),
                                ("n_unknown", "sample not stated")) if f in flags), None)


def brief_line(rows: list[dict[str, Any]], cites: dict[int, str] | None = None) -> str | None:
    """One brief line naming the cited learnings that aren't settled; None when every citation holds.
    rows: every learning, rejected ones included, annotated (learnings_view.annotate)."""
    cites = cited() if cites is None else cites
    by_id = {r["id"]: r for r in rows}
    bad = [(i, src, why) for i, src in sorted(cites.items()) if (why := _why(by_id.get(i)))]
    if not bad:
        return None
    return ("- cited as settled by a file loaded every session, but not settled in the journal: "
            + ", ".join(f"#{i} ({why}; {src})" for i, src, why in bad)
            + ". Re-state its sample (update_learning), or reword or drop the rule there (research-review)")
