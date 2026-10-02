"""Research storage (mixed into Journal):

- candidates: every coin the scanner scored (tradable or vetoed) with its full feature set, and later
  its forward outcome (what a short would have done). This turns every scan into data, so we can test
  whether a score, feature or veto actually predicts drops, without placing trades.
- learnings: hypotheses and conclusions with evidence and status. The analyst reads the active ones at
  the start of every session, so knowledge compounds instead of living in chat history.
"""

from __future__ import annotations

import json
from typing import Any

RESEARCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL, price REAL,
    score INTEGER, tradable INTEGER, setups TEXT, vetoes TEXT, signals TEXT, features TEXT, regime TEXT,
    short_ret_4h REAL, short_ret_24h REAL, short_ret_72h REAL, max_adverse_24h REAL, max_favorable_24h REAL,
    labelled_at TEXT, label_v INTEGER, label_tries INTEGER, label_error TEXT, label_retry_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cand_unlabelled ON candidates (labelled_at, ts_ms);
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, ts_ms INTEGER NOT NULL, source TEXT NOT NULL, ref_id INTEGER,
    symbol TEXT NOT NULL, entry REAL NOT NULL, stop REAL, target REAL, hours REAL NOT NULL, kind TEXT NOT NULL,
    p_model REAL, p_mc REAL, p_analyst REAL, sigma_h REAL, context TEXT,
    outcome TEXT, mfe_r REAL, mae_r REAL, labelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pred_unlabelled ON predictions (labelled_at, ts_ms);
CREATE TABLE IF NOT EXISTS learnings (
    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, setup TEXT,
    statement TEXT NOT NULL, evidence TEXT, status TEXT NOT NULL, confidence TEXT, provenance TEXT
);
"""
# Label method of a candidate's outcome. NULL = legacy (1h candles from the scan's floored hour, so the first bar
# included price action from before the scan); 2 = 5m bars opening at/after the scan time. Older versions are
# re-queued by unlabelled_candidates, so the whole dataset converges on one method.
# A failed fetch never overwrites a label (#62): label_tries counts failures since the last label written,
# label_retry_ms is the backoff, and a row with LABEL_MAX_TRIES failures leaves the queue (a legacy label stays as it
# is). Bumping LABEL_VERSION: reset label_tries too, or rows that gave up are never relabelled.
LABEL_VERSION = 2
LABEL_MAX_TRIES = 5
CANDIDATE_COLUMNS = {"label_v": "INTEGER", "label_tries": "INTEGER", "label_error": "TEXT",
                     "label_retry_ms": "INTEGER"}
# provenance: JSON list of where the evidence came from ("trade:12", "research_report", "coin_news:QNT", "alert:345"),
# so a learning planted by third-party text can be traced and flagged (#22). Learnings outlive sessions.
LEARNING_COLUMNS = {"provenance": "TEXT"}
LEARNING_STATUS = ("hypothesis", "supported", "rejected", "adopted")
CONFIDENCE = ("low", "medium", "high")


class ResearchStore:
    db: Any  # provided by Journal

    def _migrate_research(self) -> None:
        have = {r[1] for r in self.db.execute("PRAGMA table_info(candidates)")}
        for col, ddl in CANDIDATE_COLUMNS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE candidates ADD COLUMN {col} {ddl}")
        have = {r[1] for r in self.db.execute("PRAGMA table_info(learnings)")}
        for col, ddl in LEARNING_COLUMNS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE learnings ADD COLUMN {col} {ddl}")

    # ------------------------------------------------------------ candidates
    def log_candidates(self, ts: str, ts_ms: int, rows: list[dict[str, Any]], regime: str | None) -> int:
        self.db.executemany(
            "INSERT INTO candidates (ts, ts_ms, symbol, price, score, tradable, setups, vetoes, signals, features,"
            " regime) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(ts, ts_ms, r["symbol"], r.get("price"), r.get("score"), int(not r.get("vetoes")),
              json.dumps(r.get("setups", [])), json.dumps(r.get("vetoes", [])), json.dumps(r.get("signals", [])),
              json.dumps(r.get("features", {}), default=str), regime) for r in rows])
        self.db.commit()
        return len(rows)

    def unlabelled_candidates(self, older_than_ms: int, limit: int = 50, version: int = LABEL_VERSION,
                              now_ms: int | None = None) -> list[dict[str, Any]]:
        """Candidates due a label: never labelled, or labelled by an older method (relabelled oldest first).
        Rows that failed LABEL_MAX_TRIES times are out; with now_ms, so are rows still backing off."""
        backoff = " AND COALESCE(label_retry_ms, 0) <= ?" if now_ms is not None else ""
        return self._rows("SELECT id, symbol, ts_ms, price, short_ret_24h, label_tries FROM candidates WHERE ts_ms < ?"
                          " AND (labelled_at IS NULL OR label_v IS NULL OR label_v < ?) AND COALESCE(label_tries, 0) < ?"
                          f"{backoff} ORDER BY ts_ms LIMIT ?",
                          (older_than_ms, version, LABEL_MAX_TRIES, *([now_ms] if backoff else []), limit))

    def label_candidate(self, cid: int, outcome: dict[str, float | None], version: int = LABEL_VERSION) -> None:
        cols = ("short_ret_4h", "short_ret_24h", "short_ret_72h", "max_adverse_24h", "max_favorable_24h")
        self.db.execute(f"UPDATE candidates SET {', '.join(c + '=?' for c in cols)}, labelled_at=datetime('now'),"
                        " label_v=?, label_tries=NULL, label_error=NULL, label_retry_ms=NULL WHERE id=?",
                        (*(outcome.get(c) for c in cols), version, cid))
        self.db.commit()

    def label_failed(self, cid: int, error: str, tries: int, retry_ms: int | None) -> None:
        """A label fetch failed: the row keeps its outcome and label_v; retried from retry_ms (None: gave up)."""
        self.db.execute("UPDATE candidates SET label_tries=?, label_error=?, label_retry_ms=? WHERE id=?",
                        (tries, error[:200], retry_ms, cid))
        self.db.commit()

    def candidates(self, labelled_only: bool = False, limit: int = 5000) -> list[dict[str, Any]]:
        where = "WHERE labelled_at IS NOT NULL" if labelled_only else ""
        rows = self._rows(f"SELECT * FROM candidates {where} ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            for f in ("setups", "vetoes", "signals", "features"):
                r[f] = json.loads(r[f]) if r.get(f) else ([] if f != "features" else {})
        return rows

    # ------------------------------------------------------------ predictions (shadow mode + calibration)
    def log_prediction(self, source: str, symbol: str, entry: float, stop: float | None, target: float | None,
                       hours: float, kind: str, p_model: float | None, p_mc: float | None = None,
                       p_analyst: float | None = None, sigma_h: float | None = None, ref_id: int | None = None,
                       context: dict | None = None, ts_ms: int | None = None) -> int:
        """kind: 'bracket' (short: target before stop within hours) or 'touch' (price touches `target` level
        within hours, e.g. a limit fill)."""
        import time as _t
        from datetime import datetime, timezone
        ms = ts_ms or int(_t.time() * 1000)
        return self._insert(
            "INSERT INTO predictions (ts, ts_ms, source, ref_id, symbol, entry, stop, target, hours, kind, p_model,"
            " p_mc, p_analyst, sigma_h, context) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds"), ms, source, ref_id,
             symbol, entry, stop, target, hours, kind, p_model, p_mc, p_analyst, sigma_h,
             json.dumps(context or {}, default=str)))

    def due_predictions(self, now_ms: int, limit: int = 50) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM predictions WHERE labelled_at IS NULL AND ts_ms + hours*3600000 < ?"
                          " ORDER BY ts_ms LIMIT ?", (now_ms, limit))

    def label_prediction(self, pid: int, outcome: str, mfe_r: float | None = None, mae_r: float | None = None) -> None:
        self.db.execute("UPDATE predictions SET outcome=?, mfe_r=?, mae_r=?, labelled_at=datetime('now') WHERE id=?",
                        (outcome, mfe_r, mae_r, pid))
        self.db.commit()

    def predictions(self, labelled_only: bool = True, limit: int = 20000) -> list[dict[str, Any]]:
        where = "WHERE outcome IS NOT NULL AND outcome != 'no_data'" if labelled_only else ""
        rows = self._rows(f"SELECT * FROM predictions {where} ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["context"] = json.loads(r["context"]) if r.get("context") else {}
        return rows

    # ------------------------------------------------------------ learnings
    def add_learning(self, statement: str, evidence: str = "", setup: str | None = None,
                     status: str = "hypothesis", confidence: str = "low", provenance: list[str] | None = None) -> int:
        _check(status, confidence)
        prov = json.dumps(_tags(provenance)) if provenance is not None else None
        return self._insert(
            "INSERT INTO learnings (created_at, updated_at, setup, statement, evidence, status, confidence, provenance)"
            " VALUES (datetime('now'), datetime('now'), ?,?,?,?,?,?)",
            (setup, statement, evidence, status, confidence, prov))

    def update_learning(self, lid: int, status: str | None = None, evidence: str | None = None,
                        confidence: str | None = None, provenance: list[str] | None = None) -> None:
        cur = self._rows("SELECT * FROM learnings WHERE id=?", (lid,))
        if not cur:
            raise ValueError(f"no learning #{lid}")
        row = cur[0]
        status, confidence = status or row["status"], confidence or row["confidence"]
        _check(status, confidence)
        ev = row["evidence"] or ""
        if evidence:
            ev = f"{ev}\n- {evidence}".strip()
        prov = json.loads(row.get("provenance") or "[]")
        if provenance is not None:
            prov += [t for t in _tags(provenance) if t not in prov]
        self.db.execute("UPDATE learnings SET status=?, confidence=?, evidence=?, provenance=?, "
                        "updated_at=datetime('now') WHERE id=?",
                        (status, confidence, ev, json.dumps(prov) if prov else row.get("provenance"), lid))
        self.db.commit()

    def learnings(self, active_only: bool = True, setup: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM learnings WHERE 1=1", []
        if active_only:
            q += " AND status != 'rejected'"
        if setup:
            q += " AND (setup=? OR setup IS NULL)"
            args.append(setup)
        rows = self._rows(q + " ORDER BY updated_at DESC", tuple(args))
        for r in rows:
            r["provenance"] = json.loads(r["provenance"]) if r.get("provenance") else None
        return rows


def _tags(provenance: list[str]) -> list[str]:
    """Provenance tags: short one-line strings ("trade:12", "coin_news:QNT", "alert:345"), at most 20."""
    if isinstance(provenance, str) or not isinstance(provenance, list):
        raise ValueError("provenance must be a list of strings, e.g. ['trade:12', 'research_report']")
    tags = [" ".join(str(t).split())[:80] for t in provenance if str(t).strip()]
    if not tags:
        raise ValueError("provenance must name where the evidence came from, e.g. ['trade:12', 'coin_news:QNT']")
    return tags[:20]


def _check(status: str, confidence: str) -> None:
    if status not in LEARNING_STATUS:
        raise ValueError(f"status must be one of {LEARNING_STATUS}")
    if confidence not in CONFIDENCE:
        raise ValueError(f"confidence must be one of {CONFIDENCE}")
