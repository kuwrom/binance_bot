"""Learnings storage (journal_learnings.py): the new columns and the FTS5 index on old databases, a race-safe
migration, an index that follows every write, searches that never raise, current = not rejected / superseded /
expired, update_learning's checks, and closed trades carrying the learnings their plan cited."""

import sqlite3
from datetime import date

import pytest

from shortbot.journal import Journal
from shortbot.journal_learnings import LEARNING_COLUMNS, fts_query
from shortbot.journal_research import add_columns

TODAY = date(2026, 10, 3)


def integrity(j):
    """FTS5's own check: the index matches the content table (PRAGMA integrity_check misses drift)."""
    j.db.execute("INSERT INTO learnings_fts(learnings_fts, rank) VALUES('integrity-check', 1)")


def ids(rows):
    return [r["id"] for r in rows]


def test_an_old_database_gains_the_columns_and_a_built_index(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE learnings (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
                " setup TEXT, statement TEXT NOT NULL, evidence TEXT, status TEXT NOT NULL, confidence TEXT,"
                " provenance TEXT)")
    con.execute("INSERT INTO learnings VALUES (7, '2026-09-28', '2026-09-28', 'pump_fade', 'pumps fade after the"
                " failed sweep', 'ZRO #8', 'adopted', 'high', NULL)")
    con.commit()
    con.close()
    j = Journal(db)
    have = {r[1] for r in j.db.execute("PRAGMA table_info(learnings)")}
    assert set(LEARNING_COLUMNS) <= have
    integrity(j)
    assert ids(j.search_learnings("sweeps")) == [7]          # the old row is indexed, stemmed
    assert j.rebuild_learnings_index() == 1
    integrity(j)


def test_the_migration_is_idempotent_and_race_safe(tmp_path):
    p = tmp_path / "j.db"
    a = Journal(p)
    lid = a.add_learning("funding flips precede squeezes", "e", kind="statistical")
    b = Journal(p)                                            # a second process on a migrated file
    add_columns(b.db, "learnings", LEARNING_COLUMNS, have=set())   # it raced: each ALTER finds the column there
    b._create_index()                                         # it raced: the index exists once it holds the lock
    triggers = [r[0] for r in b.db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND"
                                           " name LIKE 'learnings_fts%'")]
    assert sorted(triggers) == ["learnings_fts_ad", "learnings_fts_ai", "learnings_fts_au"]
    integrity(b)
    assert ids(b.search_learnings("squeeze")) == [lid] and ids(a.search_learnings("squeeze")) == [lid]


def test_the_index_follows_inserts_updates_and_appended_evidence():
    j = Journal(":memory:")
    lid = j.add_learning("breakdowns rarely bounce", "3 trades", kind="statistical")
    assert ids(j.search_learnings("bounce")) == [lid] and j.search_learnings("liquidation") == []
    j.update_learning(lid, evidence="liquidation cascades extend them", rule="short the retest, never a bounce")
    integrity(j)
    assert ids(j.search_learnings("liquidation cascade")) == [lid] and ids(j.search_learnings("retest")) == [lid]
    j.update_learning(lid, rule="")                           # cleared: the old rule leaves the index
    integrity(j)
    assert j.search_learnings("retest") == []
    j.db.execute("UPDATE learnings SET updated_at='2026-01-01' WHERE id=?", (lid,))   # no indexed column: no trigger
    j.db.execute("DELETE FROM learnings WHERE id=?", (lid,))
    integrity(j)
    assert j.search_learnings("bounce") == []


@pytest.mark.parametrize("odd", ["trade:12", "a-b", "AND", '"(((', "", "NOT near OR", "x" * 5000, None])
def test_search_never_raises_on_odd_input(odd):
    j = Journal(":memory:")
    j.add_learning("trade 12 showed the squeeze", "AND then some", kind="statistical")
    assert isinstance(j.search_learnings(odd), list)
    q = fts_query(odd)
    assert q == "" or all(t.startswith('"') and t.endswith('"') for t in q.split(" OR "))
    assert ids(j.search_learnings("trade:12")) == [1]         # a column filter in raw FTS syntax: quoted here


def test_a_rule_match_outranks_an_evidence_match_and_words_are_stemmed():
    j = Journal(":memory:")
    for i in range(6):                                        # a corpus where the terms are rare (bm25's idf)
        j.add_learning(f"unrelated claim {i}", "other evidence", kind="process")
    ev = j.add_learning("one thing", "the funding flipped before the squeeze", kind="statistical")
    rule = j.add_learning("another thing", "e", kind="statistical", rule="funding flips warn of squeezes")
    assert ids(j.search_learnings("squeeze funding")) == [rule, ev]
    assert ids(j.search_learnings("flipping")) == [rule, ev]  # porter: flipping / flipped / flips
    assert j.search_learnings("squeeze")[0]["relevance"] > j.search_learnings("squeeze")[1]["relevance"]


def test_current_excludes_rejected_superseded_and_expired():
    j = Journal(":memory:")
    keep = j.add_learning("pumps fade", "e", kind="statistical", valid_until="2026-10-03")   # through today
    old = j.add_learning("pumps fade fast", "e", kind="statistical")
    gone = j.add_learning("pumps fade on Sundays", "e", kind="statistical", valid_until="2026-10-02")
    no = j.add_learning("pumps fade always", "e", status="rejected", kind="statistical")
    j.update_learning(old, superseded_by=keep)
    assert set(ids(j.learnings(current=True, today=TODAY))) == {keep}
    assert set(ids(j.search_learnings("pumps", today=TODAY))) == {keep}
    assert set(ids(j.learnings())) == {keep, old, gone}       # active_only keeps its meaning: the audit sees them
    assert set(ids(j.search_learnings("pumps", current=False))) == {keep, old, gone, no}
    assert set(ids(j.learnings(active_only=False, ids=[no, old]))) == {no, old}
    assert ids(j.learnings(status="rejected", active_only=False)) == [no]
    assert j.unknown_learnings([keep, 99, 99]) == [99]


def test_update_learning_checks_its_fields_and_clears_them():
    j = Journal(":memory:")
    a = j.add_learning("a", "e", kind="statistical", rule="short the retest", valid_until="2026-12-01",
                       setup="pump_fade")
    b = j.add_learning("b", "e", kind="statistical")
    c = j.add_learning("c", "e", kind="statistical")
    for bad, why in (({"rule": "x" * 161}, "160"), ({"superseded_by": a}, "itself"), ({"superseded_by": 99}, "no "),
                     ({"valid_until": "soon"}, "date"), ({"superseded_by": -1}, "learning id")):
        with pytest.raises(ValueError, match=why):
            j.update_learning(a, **bad)
    with pytest.raises(ValueError, match="160"):
        j.add_learning("d", "e", kind="statistical", rule="y" * 200)
    j.update_learning(b, superseded_by=c)
    j.update_learning(c, superseded_by=a)
    with pytest.raises(ValueError, match="cycle"):
        j.update_learning(a, superseded_by=b)                 # a -> b -> c -> a
    j.update_learning(a, rule="  one\nline  ")
    assert j.learnings(ids=[a])[0]["rule"] == "one line"      # one line: it can't open a section of the brief
    j.update_learning(a, rule="", setup="", valid_until="")
    j.update_learning(b, superseded_by=0)
    row, rb = j.learnings(ids=[a])[0], j.learnings(ids=[b])[0]
    assert (row["rule"], row["setup"], row["valid_until"], rb["superseded_by"]) == (None, None, None, None)
    assert row["statement"] == "a"                            # never changes: supersede instead


def test_closed_results_carry_the_learnings_a_plan_cited():
    j = Journal(":memory:")
    for plan in ({"uses_learnings": [3, 5], "r1_usdt": 10.0}, {}, {"uses_learnings": ["x", 4]}):
        tid = j.create_trade("ZRO/USDT:USDT", "pump_fade", "t", 1.1, 2, plan=plan)
        j.activate_trade(tid, 1.0, 100.0, 100.0)
        j.close_trade(tid, 0.95, 5.0, "target")
    assert [r["uses_learnings"] for r in j.closed_results()] == [[3, 5], [], [4]]
    assert j.closed_results()[0]["r_multiple"] == 0.5         # the pinned 1R still read from the same plan
