"""Learnings (mixed into Journal): hypotheses and conclusions with evidence and status. Sessions start from them, so
knowledge compounds instead of living in chat history.

A learning changes item by item, never by a rewrite: evidence and provenance are appended, and a changed claim is a NEW
learning that supersedes the old one (superseded_by), so the history stays. `rule` is the one-line form the brief's
rulebook shows (learnings_view.settled decides which rows get there); `valid_until` is the date a regime- or
coin-bound claim stops being current.

learnings_fts indexes rule, statement, evidence and setup (FTS5, external content, porter stemming: "fades" finds
"fade"). Triggers keep it current, so a search never misses a learning added since the last registry seed. Never write
a learning with INSERT OR REPLACE: its implicit delete fires no trigger and the index drifts. A changed trigger or
column list needs a versioned drop and recreate (here, in _migrate_learnings): the index is created only when missing.
rebuild_learnings_index is the repair path (registry_seed.seed_all runs it).
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

from .journal_research import add_columns

LEARNINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS learnings (
    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, setup TEXT,
    statement TEXT NOT NULL, evidence TEXT, status TEXT NOT NULL, confidence TEXT, provenance TEXT
);
"""
# provenance: JSON list of where the evidence came from ("trade:12", "research_report", "coin_news:QNT", "alert:345"),
# so a learning planted by third-party text can be traced and flagged (#22). Learnings outlive sessions.
# kind / n / days / sample / review_by (#28): what sort of claim it is, and for statistical ones the effective sample
# (independent observations: episodes, trades or events, never raw scan rows), its days and where it lives
# ("research_report:2026-10-02", "registry:lib.x", "data/research/<dir>"). NULL on rows recorded before #28:
# learnings_view infers them for display and never writes them back.
# rule / superseded_by / valid_until: the rulebook line, the learning that replaced this one, the last current date.
LEARNING_COLUMNS = {"provenance": "TEXT", "kind": "TEXT", "n": "REAL", "days": "INTEGER", "sample": "TEXT",
                    "review_by": "TEXT", "rule": "TEXT", "superseded_by": "INTEGER", "valid_until": "TEXT"}
LEARNING_STATUS = ("hypothesis", "supported", "rejected", "adopted")
CONFIDENCE = ("low", "medium", "high")
LEARNING_KINDS = ("engineering", "statistical", "process")
SAMPLE_MAX = 120
RULE_MAX = 160
FTS_COLUMNS = "rule, statement, evidence, setup"
FTS_WEIGHTS = "3.0, 2.0, 0.5, 1.0"   # bm25, in FTS_COLUMNS order: a rule match outranks an evidence match
_NEW = "new.id, new.rule, new.statement, new.evidence, new.setup"
_OLD = "'delete', old.id, old.rule, old.statement, old.evidence, old.setup"
FTS_DDL = (
    f"CREATE VIRTUAL TABLE learnings_fts USING fts5({FTS_COLUMNS}, content='learnings', content_rowid='id',"
    " tokenize='porter unicode61')",
    f"CREATE TRIGGER learnings_fts_ai AFTER INSERT ON learnings BEGIN"
    f" INSERT INTO learnings_fts(rowid, {FTS_COLUMNS}) VALUES ({_NEW}); END",
    f"CREATE TRIGGER learnings_fts_ad AFTER DELETE ON learnings BEGIN"
    f" INSERT INTO learnings_fts(learnings_fts, rowid, {FTS_COLUMNS}) VALUES ({_OLD}); END",
    f"CREATE TRIGGER learnings_fts_au AFTER UPDATE OF {FTS_COLUMNS} ON learnings BEGIN"
    f" INSERT INTO learnings_fts(learnings_fts, rowid, {FTS_COLUMNS}) VALUES ({_OLD});"
    f" INSERT INTO learnings_fts(rowid, {FTS_COLUMNS}) VALUES ({_NEW}); END",
)
STOP_WORDS = frozenset(("the", "and", "for", "with", "not", "but", "are", "was", "were", "this", "that", "from", "into",
                        "has", "have", "its", "all", "any", "can", "our", "you", "when", "than", "then", "near"))
MAX_TERMS = 24


def fts_query(text: str | None) -> str:
    """A MATCH expression that can't fail on user text: each word of 3+ characters once, stop words dropped, every word
    quoted ("w1" OR "w2"). Raw input breaks MATCH: trade:12 (a column filter), a-b, AND, '"((('. '' when nothing is left."""
    words = [w for w in re.findall(r"[^\W_]+", str(text or "").lower()) if len(w) >= 3 and w not in STOP_WORDS]
    return " OR ".join(f'"{w}"' for w in list(dict.fromkeys(words))[:MAX_TERMS])


def _day(today: date | None) -> str:
    return (today or datetime.now(timezone.utc).date()).isoformat()


def _current(t: str = "") -> str:
    """Current: not rejected, not superseded, not past valid_until (one ? = today). t: the table alias + '.'."""
    return f"{t}status != 'rejected' AND {t}superseded_by IS NULL AND ({t}valid_until IS NULL OR {t}valid_until >= ?)"


class LearningStore:
    db: sqlite3.Connection   # provided by Journal

    if TYPE_CHECKING:   # Journal's
        def _rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]: ...

        def _insert(self, sql: str, args: tuple[Any, ...]) -> int: ...

    def _migrate_learnings(self) -> None:
        add_columns(self.db, "learnings", LEARNING_COLUMNS)   # before the index: it reads `rule`
        if not self._has_index():
            self._create_index()

    def _has_index(self) -> bool:
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE name='learnings_fts'").fetchone() is not None

    def _create_index(self) -> None:
        """The index, its triggers, then a rebuild from the table, as one IMMEDIATE transaction: a process migrating at
        the same time waits for it, then finds the index there; no learning is written between the two."""
        if self.db.in_transaction:
            self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if not self._has_index():
                for sql in FTS_DDL:
                    self.db.execute(sql)
                self.db.execute("INSERT INTO learnings_fts(learnings_fts) VALUES('rebuild')")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def rebuild_learnings_index(self) -> int:
        """Re-read every learning into learnings_fts (the repair path for a drifted index); the number indexed."""
        if self._has_index():
            self.db.execute("INSERT INTO learnings_fts(learnings_fts) VALUES('rebuild')")
            self.db.commit()
        else:
            self._create_index()
        return int(self.db.execute("SELECT COUNT(*) FROM learnings").fetchone()[0])

    # ------------------------------------------------------------ writes
    def add_learning(self, statement: str, evidence: str = "", setup: str | None = None,
                     status: str = "hypothesis", confidence: str = "low", provenance: list[str] | None = None,
                     kind: str | None = None, n: float | None = None, days: int | None = None,
                     sample: str | None = None, review_by: str | None = None, rule: str | None = None,
                     valid_until: str | None = None) -> int:
        """kind is required by the add_learning tool; it stays optional here for older callers (CLI, imports)."""
        _check(status, confidence)
        prov = json.dumps(_tags(provenance)) if provenance is not None else None
        f = _fields(kind=kind, n=n, days=days, sample=sample, review_by=review_by)
        f.update({k: v for k, v in _clearable(rule=rule, valid_until=valid_until).items() if v is not None})
        return self._insert(
            "INSERT INTO learnings (created_at, updated_at, setup, statement, evidence, status, confidence, provenance"
            f"{''.join(', ' + k for k in f)}) VALUES (datetime('now'), datetime('now'), ?,?,?,?,?,?{',?' * len(f)})",
            (setup, statement, evidence, status, confidence, prov, *f.values()))

    def update_learning(self, lid: int, status: str | None = None, evidence: str | None = None,
                        confidence: str | None = None, provenance: list[str] | None = None,
                        kind: str | None = None, n: float | None = None, days: int | None = None,
                        sample: str | None = None, review_by: str | None = None, rule: str | None = None,
                        setup: str | None = None, superseded_by: int | None = None,
                        valid_until: str | None = None) -> None:
        """Evidence and provenance are appended; kind, n, days, sample, review_by, rule, setup and valid_until replace
        what was there ('' clears rule, setup and valid_until); superseded_by=0 clears the link. The statement never
        changes: a changed claim is a new learning that supersedes this one."""
        cur = self._rows("SELECT * FROM learnings WHERE id=?", (lid,))
        if not cur:
            raise ValueError(f"no learning #{lid}")
        row = cur[0]
        status, confidence = status or row["status"], confidence or row["confidence"]
        _check(status, confidence)
        f = {**_fields(kind=kind, n=n, days=days, sample=sample, review_by=review_by),
             **_clearable(rule=rule, setup=setup, valid_until=valid_until)}
        if superseded_by is not None:
            f["superseded_by"] = self._successor(lid, superseded_by)
        ev = row["evidence"] or ""
        if evidence:
            ev = f"{ev}\n- {evidence}".strip()
        prov = json.loads(row.get("provenance") or "[]")
        if provenance is not None:
            prov += [t for t in _tags(provenance) if t not in prov]
        sets = "".join(f", {k}=?" for k in f)
        self.db.execute(f"UPDATE learnings SET status=?, confidence=?, evidence=?, provenance=?{sets}, "
                        "updated_at=datetime('now') WHERE id=?",
                        (status, confidence, ev, json.dumps(prov) if prov else row.get("provenance"), *f.values(), lid))
        self.db.commit()

    def _successor(self, lid: int, sid: int) -> int | None:
        """superseded_by, checked: an existing learning, not this one, and no chain that leads back here. 0 clears it."""
        if isinstance(sid, bool) or not isinstance(sid, int) or sid < 0:
            raise ValueError("superseded_by must be a learning id (0 clears it)")
        if sid == 0:
            return None
        if sid == lid:
            raise ValueError(f"learning #{lid} can't supersede itself")
        if self.unknown_learnings([sid]):
            raise ValueError(f"superseded_by: no learning #{sid}")
        seen, nxt = {lid}, int(sid)   # walk sid's successors: none may be this learning
        while nxt:
            if nxt in seen:
                raise ValueError(f"superseded_by={sid} makes a cycle: #{sid}'s successors lead back to #{lid}")
            seen.add(nxt)
            r = self.db.execute("SELECT superseded_by FROM learnings WHERE id=?", (nxt,)).fetchone()
            nxt = int(r[0]) if r and r[0] is not None else 0
        return sid

    # ------------------------------------------------------------ reads
    def learnings(self, active_only: bool = True, setup: str | None = None, *, current: bool = False,
                  ids: list[int] | None = None, status: str | None = None,
                  today: date | None = None) -> list[dict[str, Any]]:
        """Newest update first. active_only: not rejected (superseded and expired rows stay: the audit flags them).
        current: not rejected, not superseded, not past valid_until. ids / status narrow it further."""
        q = "SELECT * FROM learnings WHERE 1=1"
        args: list[Any] = []
        if active_only and not current:
            q += " AND status != 'rejected'"
        if current:
            q += f" AND {_current()}"
            args.append(_day(today))
        if setup:
            q += " AND (setup=? OR setup IS NULL)"
            args.append(setup)
        if status:
            q += " AND status=?"
            args.append(status)
        if ids is not None:
            q += f" AND id IN ({','.join('?' * len(ids))})"
            args += list(ids)
        return [_row(r) for r in self._rows(q + " ORDER BY updated_at DESC", tuple(args))]

    def search_learnings(self, query: str, setup: str | None = None, status: str | None = None, limit: int = 10,
                         current: bool = True, today: date | None = None) -> list[dict[str, Any]]:
        """Best match first (bm25 over FTS_COLUMNS, weighted FTS_WEIGHTS), each row with its `relevance`. Words are
        stemmed and OR-ed (fts_query); input it can't use returns [] instead of raising."""
        q = fts_query(query)
        if not q:
            return []
        sql = (f"SELECT l.*, -bm25(learnings_fts, {FTS_WEIGHTS}) AS relevance FROM learnings_fts"
               " JOIN learnings l ON l.id = learnings_fts.rowid WHERE learnings_fts MATCH ?")
        args: list[Any] = [q]
        if current:
            sql += f" AND {_current('l.')}"
            args.append(_day(today))
        if setup:
            sql += " AND (l.setup=? OR l.setup IS NULL)"
            args.append(setup)
        if status:
            sql += " AND l.status=?"
            args.append(status)
        try:
            rows = self._rows(sql + " ORDER BY relevance DESC, l.id DESC LIMIT ?", (*args, limit))
        except sqlite3.DatabaseError:   # a missing or broken index: no results, never a crash
            return []
        for r in rows:
            r["relevance"] = round(r["relevance"], 2)
        return [_row(r) for r in rows]

    def unknown_learnings(self, ids: list[int]) -> list[int]:
        """The ids no learning has (a plan citing them is refused)."""
        want = list(dict.fromkeys(ids))
        if not want:
            return []
        have = {r[0] for r in self.db.execute(f"SELECT id FROM learnings WHERE id IN ({','.join('?' * len(want))})",
                                              want)}
        return [i for i in want if i not in have]


def _row(r: dict[str, Any]) -> dict[str, Any]:
    r["provenance"] = json.loads(r["provenance"]) if r.get("provenance") else None
    if isinstance(r.get("n"), float) and r["n"].is_integer():
        r["n"] = int(r["n"])
    return r


def _tags(provenance: list[str]) -> list[str]:
    """Provenance tags: short one-line strings ("trade:12", "coin_news:QNT", "alert:345"), at most 20."""
    if isinstance(provenance, str) or not isinstance(provenance, list):
        raise ValueError("provenance must be a list of strings, e.g. ['trade:12', 'research_report']")
    tags = [" ".join(str(t).split())[:80] for t in provenance if str(t).strip()]
    if not tags:
        raise ValueError("provenance must name where the evidence came from, e.g. ['trade:12', 'coin_news:QNT']")
    return tags[:20]


def _date(name: str, v: Any) -> str:
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        raise ValueError(f"{name} must be a date, YYYY-MM-DD") from None


def _fields(**given: Any) -> dict[str, Any]:
    """The #28 fields that were given (None = leave as is), validated and normalised."""
    out = {k: v for k, v in given.items() if v is not None}
    if "kind" in out and out["kind"] not in LEARNING_KINDS:
        raise ValueError(f"kind must be one of {LEARNING_KINDS}")
    if "n" in out and (isinstance(out["n"], bool) or not isinstance(out["n"], (int, float)) or out["n"] < 0):
        raise ValueError("n must be a number >= 0: independent observations (episodes, trades, events), not rows")
    if "days" in out and (isinstance(out["days"], bool) or not isinstance(out["days"], int) or out["days"] < 0):
        raise ValueError("days must be a whole number >= 0")
    if "sample" in out:
        out["sample"] = " ".join(str(out["sample"]).split())
        if len(out["sample"]) > SAMPLE_MAX:
            raise ValueError(f"sample is a short link (<= {SAMPLE_MAX} chars), e.g. 'research_report:2026-10-02', "
                             "'registry:lib.x' or 'data/research/<dir>'")
        if not out["sample"]:
            del out["sample"]
    if "review_by" in out:
        out["review_by"] = _date("review_by", out["review_by"])
    return out


def _clearable(**given: Any) -> dict[str, Any]:
    """rule / setup / valid_until that were given (None = leave as is), normalised; '' clears one (NULL)."""
    out: dict[str, Any] = {}
    for k, v in given.items():
        if v is None:
            continue
        v = " ".join(str(v).split())   # one line: a rule can't open a section of the brief
        if not v:
            out[k] = None
        elif k == "rule" and len(v) > RULE_MAX:
            raise ValueError(f"rule is one line of <= {RULE_MAX} chars (got {len(v)}): the claim, its condition and "
                             "the action; the detail stays in the statement and evidence")
        else:
            out[k] = _date(k, v) if k == "valid_until" else v
    return out


def _check(status: str, confidence: str) -> None:
    if status not in LEARNING_STATUS:
        raise ValueError(f"status must be one of {LEARNING_STATUS}")
    if confidence not in CONFIDENCE:
        raise ValueError(f"confidence must be one of {CONFIDENCE}")
